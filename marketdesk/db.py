"""SQLite storage layer: prices, fundamentals, scores, holdings, alerts."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime

import pandas as pd

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS universe(
  symbol TEXT PRIMARY KEY,
  name   TEXT,
  industry TEXT,
  sector TEXT
);
CREATE TABLE IF NOT EXISTS prices(
  symbol TEXT NOT NULL,
  date   TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  PRIMARY KEY(symbol, date)
);
CREATE INDEX IF NOT EXISTS idx_prices_date ON prices(date);
CREATE TABLE IF NOT EXISTS fundamentals(
  symbol TEXT PRIMARY KEY,
  fetched_at TEXT,
  data TEXT
);
CREATE TABLE IF NOT EXISTS scores(
  date TEXT, symbol TEXT,
  dvs REAL, value REAL, quality REAL, momentum REAL, growth REAL, dividend REAL,
  rank INTEGER,
  PRIMARY KEY(date, symbol)
);
CREATE TABLE IF NOT EXISTS holdings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT UNIQUE NOT NULL,
  qty REAL NOT NULL,
  avg_cost REAL NOT NULL,
  note TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS alert_rules(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  symbol TEXT DEFAULT '',
  params TEXT DEFAULT '{}',
  enabled INTEGER DEFAULT 1,
  last_triggered TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT, rule TEXT, symbol TEXT, message TEXT, channels TEXT
);
CREATE TABLE IF NOT EXISTS screeners(
  name TEXT PRIMARY KEY,
  query TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);
"""


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db() -> None:
    with conn() as c:
        c.executescript(SCHEMA)


def rows(sql: str, params=()) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def frame(sql: str, params=()) -> pd.DataFrame:
    with conn() as c:
        return pd.read_sql_query(sql, c, params=params)


def run(sql: str, params=()) -> None:
    with conn() as c:
        c.execute(sql, params)


def executemany(sql: str, seq) -> None:
    with conn() as c:
        c.executemany(sql, seq)


# ---------------------------------------------------------------- universe
def save_universe(df: pd.DataFrame) -> None:
    recs = [
        (r.symbol, getattr(r, "name", "") or "", getattr(r, "industry", "") or "",
         getattr(r, "sector", "") or "")
        for r in df.itertuples()
    ]
    with conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO universe(symbol,name,industry,sector) VALUES (?,?,?,?)",
            recs,
        )


def load_universe() -> pd.DataFrame:
    return frame("SELECT * FROM universe ORDER BY symbol")


# ---------------------------------------------------------------- prices
def upsert_prices(df: pd.DataFrame) -> int:
    """df must have: symbol, date, open, high, low, close, volume."""
    if df.empty:
        return 0
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"]).dt.strftime("%Y-%m-%d")
    d = d.dropna(subset=["close"])
    seq = [
        (r.symbol, r.date, _f(r.open), _f(r.high), _f(r.low), _f(r.close), _f(r.volume))
        for r in d.itertuples()
    ]
    with conn() as c:
        c.executemany(
            "INSERT OR REPLACE INTO prices(symbol,date,open,high,low,close,volume) "
            "VALUES (?,?,?,?,?,?,?)",
            seq,
        )
    return len(seq)


def _f(v) -> float:
    try:
        f = float(v)
        return f if f == f else None  # NaN -> None
    except (TypeError, ValueError):
        return None


def last_dates() -> pd.DataFrame:
    """Latest date present in the DB per symbol."""
    return frame("SELECT symbol, MAX(date) AS last_date FROM prices GROUP BY symbol")


def load_closes(start: str | None = None, symbols: list[str] | None = None) -> pd.DataFrame:
    """Wide DataFrame: index=date (str), columns=symbol, values=close."""
    sql = "SELECT symbol, date, close FROM prices"
    params: list = []
    if start:
        sql += " WHERE date >= ?"
        params.append(start)
    if symbols:
        sql += (" AND " if start else " WHERE ") + (
            "symbol IN (%s)" % ",".join("?" * len(symbols))
        )
        params.extend(symbols)
    df = frame(sql, params)
    if df.empty:
        return pd.DataFrame()
    return df.pivot(index="date", columns="symbol", values="close").sort_index()


def load_history(symbol: str, start: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM prices WHERE symbol=?"
    params: list = [symbol]
    if start:
        sql += " AND date>=?"
        params.append(start)
    sql += " ORDER BY date"
    df = frame(sql, params)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
    return df


def latest_closes() -> pd.DataFrame:
    """Per-symbol latest close with previous close and % change."""
    df = frame(
        """
        SELECT p.symbol, p.date, p.close,
               (SELECT close FROM prices p2
                 WHERE p2.symbol = p.symbol AND p2.date < p.date
                 ORDER BY p2.date DESC LIMIT 1) AS prev_close
          FROM prices p
          JOIN (SELECT symbol, MAX(date) AS maxd FROM prices GROUP BY symbol) m
            ON m.symbol = p.symbol AND m.maxd = p.date
        """
    )
    if not df.empty:
        df["chg_pct"] = (df["close"] / df["prev_close"] - 1) * 100
    return df


# ---------------------------------------------------------------- fundamentals
def save_fundamentals(symbol: str, data: dict) -> None:
    import json

    run(
        "INSERT OR REPLACE INTO fundamentals(symbol,fetched_at,data) VALUES (?,?,?)",
        (symbol, datetime.now().isoformat(timespec="seconds"), json.dumps(data)),
    )


def load_fundamentals() -> pd.DataFrame:
    import json

    df = frame("SELECT symbol, fetched_at, data FROM fundamentals")
    if df.empty:
        return pd.DataFrame()
    parsed = df.apply(lambda r: {"symbol": r.symbol, **json.loads(r.data)}, axis=1)
    return pd.DataFrame(list(parsed))


# ---------------------------------------------------------------- scores
def save_scores(date: str, df: pd.DataFrame) -> None:
    cols = ["symbol", "dvs", "value", "quality", "momentum", "growth", "dividend", "rank"]
    seq = [
        (date, r.symbol, _f(r.dvs), _f(r.value), _f(r.quality), _f(r.momentum),
         _f(r.growth), _f(r.dividend), int(r.rank) if r.rank == r.rank else None)
        for r in df[cols].itertuples()
    ]
    with conn() as c:
        c.execute("DELETE FROM scores WHERE date=?", (date,))  # full-day replace
        c.executemany(
            "INSERT OR REPLACE INTO scores(date,symbol,dvs,value,quality,momentum,"
            "growth,dividend,rank) VALUES (?,?,?,?,?,?,?,?,?)",
            seq,
        )


def latest_scores() -> pd.DataFrame:
    return frame(
        """
        SELECT * FROM scores
         WHERE date = (SELECT MAX(date) FROM scores)
         ORDER BY rank
        """
    )


# ---------------------------------------------------------------- holdings
def add_holding(symbol: str, qty: float, avg_cost: float, note: str = "") -> None:
    run(
        "INSERT OR REPLACE INTO holdings(symbol,qty,avg_cost,note) VALUES (?,?,?,?)",
        (symbol.upper().strip(), qty, avg_cost, note),
    )


def delete_holding(symbol: str) -> None:
    run("DELETE FROM holdings WHERE symbol=?", (symbol.upper().strip(),))


def load_holdings() -> pd.DataFrame:
    return frame("SELECT * FROM holdings ORDER BY symbol")


# ---------------------------------------------------------------- misc
def set_setting(key: str, value: str) -> None:
    run("INSERT OR REPLACE INTO settings(key,value) VALUES (?,?)", (key, value))


def get_setting(key: str, default: str = "") -> str:
    r = rows("SELECT value FROM settings WHERE key=?", (key,))
    return r[0]["value"] if r else default
