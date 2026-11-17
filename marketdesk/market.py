"""Price downloads (yfinance), technical indicators, market breadth."""
from __future__ import annotations

import time

import numpy as np
import pandas as pd
import yfinance as yf

from . import db

INDICES = {"NIFTY 50": "^NSEI", "SENSEX": "^BSESN", "NIFTY BANK": "^NSEBANK",
           "INDIA VIX": "^INDIAVIX"}


# ---------------------------------------------------------------- downloads
def _flatten(raw: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    """yf.download output -> long df with symbol column."""
    if raw.empty:
        return pd.DataFrame()
    if not isinstance(raw.columns, pd.MultiIndex):
        raw = raw.copy()
        raw["symbol"] = symbols[0] if len(symbols) == 1 else ""
    else:
        # pick the column level that holds ticker symbols
        lv0 = set(map(str, raw.columns.get_level_values(0)))
        lvl = 0 if str(symbols[0]) in lv0 else 1
        try:
            stacked = raw.stack(level=lvl, future_stack=True)
        except (TypeError, NotImplementedError, ValueError):
            stacked = raw.stack(level=lvl)
        stacked = stacked.rename_axis(["date", "symbol"]).reset_index()
        raw = stacked
    raw = raw.rename(columns=str.lower)
    if "symbol" not in raw.columns:
        raw["symbol"] = symbols[0] if len(symbols) == 1 else ""
    keep = ["symbol", "date", "open", "high", "low", "close", "volume"]
    out = raw[[c for c in keep if c in raw.columns]]
    if "date" in out.columns and "symbol" in out.columns:
        out = out.dropna(subset=["close"])
    return out


def download(symbols: list[str], start: str | None = None,
             period: str = "2y", retries: int = 2) -> pd.DataFrame:
    if not symbols:
        return pd.DataFrame()
    for attempt in range(retries + 1):
        try:
            raw = yf.download(
                symbols, start=start, period=period, group_by="ticker",
                auto_adjust=True, threads=True, progress=False,
            )
            return _flatten(raw, symbols)
        except Exception:  # noqa: BLE001
            if attempt == retries:
                raise
            time.sleep(3)
    return pd.DataFrame()


def update_prices(symbols: list[str], force_full: bool = False,
                  progress=None) -> int:
    """Incrementally refresh closes for symbols; always refreshes indices."""
    last = db.last_dates().set_index("symbol")["last_date"].to_dict()
    today = pd.Timestamp.today().strftime("%Y-%m-%d")

    stale, fresh = [], []
    for s in symbols:
        d = last.get(s)
        if force_full or d is None or d < today:
            stale.append(s)
        else:
            fresh.append(s)

    total = 0
    if stale:
        starts = [last[s] for s in stale if last.get(s)]
        start = (min(starts) if starts else None)
        # de-overlap: refetch from earliest needed date (1 day cushion)
        if start:
            start = (pd.Timestamp(start) - pd.Timedelta(days=3)).strftime("%Y-%m-%d")
        if progress:
            progress(f"Downloading {len(stale)} symbol(s)...")
        df = download(stale, start=start)
        if not df.empty:
            df["symbol"] = df["symbol"].str.upper()
            total += db.upsert_prices(df)

    # indices always refreshed
    idx = list(INDICES.values())
    if progress:
        progress("Refreshing indices...")
    df = download(idx, period="2y")
    if not df.empty:
        df["symbol"] = df["symbol"].str.upper()
        total += db.upsert_prices(df)
    return total


# ---------------------------------------------------------------- indicators
def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n, min_periods=n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    line = ema(close, fast) - ema(close, slow)
    signal = ema(line, sig)
    return line, signal, line - signal


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0):
    mid = sma(close, n)
    sd = close.rolling(n, min_periods=n).std()
    return mid + k * sd, mid, mid - k * sd


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    hl = df["high"] - df["low"]
    hc = (df["high"] - df["close"].shift()).abs()
    lc = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def technicals(close: pd.DataFrame) -> pd.DataFrame:
    """Vectorised technical metrics for a wide close matrix (date x symbol)."""
    out = pd.DataFrame(index=close.columns)
    out["close"] = close.iloc[-1]
    out["sma50"] = sma(close, 50).iloc[-1]
    out["sma200"] = sma(close, 200).iloc[-1]
    out["rsi14"] = rsi(close, 14).iloc[-1]
    out["dist_sma50"] = _pct(out["close"], out["sma50"])
    out["dist_sma200"] = _pct(out["close"], out["sma200"])
    for label, n in (("mom1m", 21), ("mom3m", 63), ("mom6m", 126), ("mom12m", 252)):
        out[label] = (close.pct_change(n).iloc[-1]) * 100
    hi = close.rolling(252, min_periods=60).max().iloc[-1]
    out["dist_52w_high"] = _pct(out["close"], hi)
    lo = close.rolling(252, min_periods=60).min().iloc[-1]
    out["dist_52w_low"] = _pct(out["close"], lo)
    return out


def _pct(a: pd.Series, b: pd.Series) -> pd.Series:
    return (a / b - 1) * 100


# ---------------------------------------------------------------- breadth
def breadth() -> dict:
    """Advance/decline across the stored universe using the latest session."""
    lc = db.latest_closes()
    lc = lc[lc["prev_close"].notna() & lc["chg_pct"].notna()]
    lc = lc[~lc["symbol"].str.startswith("^")]
    if lc.empty:
        return {"advances": 0, "declines": 0, "unchanged": 0, "date": "", "as_of": ""}
    adv = int((lc["chg_pct"] > 0).sum())
    dec = int((lc["chg_pct"] < 0).sum())
    unc = int((lc["chg_pct"] == 0).sum())
    return {
        "advances": adv, "declines": dec, "unchanged": unc,
        "date": lc["date"].max(),
        "as_of": lc["date"].max(),
    }


def index_snapshot() -> pd.DataFrame:
    """Latest value + % change for the major indices."""
    syms = [v.upper() for v in INDICES.values()]
    out = []
    for name, sym in INDICES.items():
        h = db.load_history(sym.upper())
        if len(h) < 2:
            continue
        last, prev = h["close"].iloc[-1], h["close"].iloc[-2]
        out.append({
            "index": name, "date": str(h.index[-1].date()),
            "value": round(last, 2),
            "change_pct": round((last / prev - 1) * 100, 2),
        })
    return pd.DataFrame(out)


def movers(top_n: int = 10) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Top gainers and losers from the latest session."""
    lc = db.latest_closes()
    lc = lc[lc["prev_close"].notna() & ~lc["symbol"].str.startswith("^")]
    univ = db.load_universe()
    if not univ.empty:
        lc = lc.merge(univ[["symbol", "name"]], on="symbol", how="left")
    else:
        lc["name"] = lc["symbol"]
    cols = ["symbol", "name", "close", "chg_pct"]
    gainers = lc.nlargest(top_n, "chg_pct")[cols]
    losers = lc.nsmallest(top_n, "chg_pct")[cols]
    return gainers, losers
