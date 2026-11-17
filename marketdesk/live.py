"""Live quote engine — near-real-time data during market hours.

Yahoo's batch quote endpoint (`/v7/finance/quote`) returns hundreds of symbols
in a SINGLE HTTP request (measured: all 501 Nifty 500 symbols in ~0.6 s), so
the whole universe can refresh every `live.index_seconds` seconds without
tripping rate limits. The Streamlit UI re-renders every `live.ui_seconds`
seconds via st.fragment and simply reads the SQLite DB this engine keeps fresh.

Cadence (market hours = Mon-Fri 09:15-15:30 IST):
  quotes   every live.index_seconds   (one batch HTTP request)
  alerts   every 60 s
  UI       every live.ui_seconds      (st.fragment in app.py)

Yahoo's NSE feed is delayed ~15 min (`exchangeDataDelayedBy`) — a free-data
limitation that is disclosed in the UI.

If the batch endpoint ever fails we fall back to per-ticker yfinance downloads
at the slower `live.universe_seconds` cadence.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

import pandas as pd

from . import db
from .config import CFG

IST = ZoneInfo("Asia/Kolkata")
QUOTE_URL = "https://query1.finance.yahoo.com/v7/finance/quote"
_CHUNK = 400          # symbols per HTTP request (500 measured OK, stay under)

_YF = None            # shared yfinance cookie/crumb session
_thread: threading.Thread | None = None
_thread_lock = threading.Lock()
_stop = threading.Event()

_status: dict = {
    "running": False,
    "market_open": False,
    "market_state": "",
    "last_quotes": None,     # iso ts of last successful quote refresh
    "last_alerts": None,     # iso ts of last alert evaluation
    "symbols": 0,
    "rows": 0,
    "runs": 0,
    "error": "",
}


# ---------------------------------------------------------------- time helpers
def now() -> datetime:
    return datetime.now(IST)


def market_open(at: datetime | None = None) -> bool:
    """True Mon-Fri 09:15-15:30 IST (holidays are not modelled)."""
    at = at or now()
    if at.weekday() >= 5:
        return False
    t = at.time()
    return dtime(9, 15) <= t <= dtime(15, 30)


def ui_interval() -> int:
    return max(1, int(CFG["live"]["ui_seconds"]))


def status() -> dict:
    """Snapshot for the UI (computed ages, safe to read from any thread)."""
    s = dict(_status)
    s["market_open"] = market_open()
    s["ui_seconds"] = ui_interval()
    s["index_seconds"] = int(CFG["live"]["index_seconds"])
    s["now"] = now().strftime("%H:%M:%S")
    s["quote_age"] = _age(s.get("last_quotes"))
    s["alert_age"] = _age(s.get("last_alerts"))
    return s


def _age(iso: str | None) -> int | None:
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return max(0, int((now() - dt).total_seconds()))
    except ValueError:
        return None


def _set(**kw) -> None:
    _status.update(kw)


# ---------------------------------------------------------------- fetching
def _yf():
    global _YF
    if _YF is None:
        from yfinance.data import YfData
        _YF = YfData()
    return _YF


def all_symbols() -> list[str]:
    """Indices (+India VIX) plus every universe symbol, de-duplicated."""
    from .market import INDICES
    syms = list(dict.fromkeys(INDICES.values()))
    try:
        univ = db.load_universe()
        if not univ.empty:
            syms += univ["symbol"].tolist()
    except Exception:  # noqa: BLE001 - universe may not exist yet
        pass
    return list(dict.fromkeys(s.upper() for s in syms))


def _quote_row(q: dict) -> dict | None:
    sym = q.get("symbol")
    px = q.get("regularMarketPrice")
    if not sym or px is None or px <= 0:
        return None
    ts = q.get("regularMarketTime")
    dt = datetime.fromtimestamp(ts, IST) if ts else now()
    o = q.get("regularMarketOpen") or 0
    hi = q.get("regularMarketDayHigh") or 0
    lo = q.get("regularMarketDayLow") or 0
    # pre-market / missing OHLC: fall back to last price so we never write 0s
    return {
        "symbol": str(sym).upper(),
        "date": dt.strftime("%Y-%m-%d"),
        "open": float(o or px),
        "high": float(hi or px),
        "low": float(lo or px),
        "close": float(px),
        "volume": float(q.get("regularMarketVolume") or 0),
        "market_state": str(q.get("marketState") or ""),
    }


def fetch_quotes(symbols: list[str]) -> pd.DataFrame:
    """Batch-quote up to `_CHUNK` symbols per HTTP request."""
    yd = _yf()
    rows: list[dict] = []
    for i in range(0, len(symbols), _CHUNK):
        part = symbols[i:i + _CHUNK]
        last_err: Exception | None = None
        for attempt in range(2):
            try:
                resp = yd.get(QUOTE_URL, params={"symbols": ",".join(part)})
                data = resp.json().get("quoteResponse", {}).get("result", [])
                rows += [r for r in (_quote_row(q) for q in data) if r]
                last_err = None
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                _time.sleep(2)
        if last_err is not None:
            raise last_err
    return pd.DataFrame(rows)


def refresh(symbols: list[str] | None = None) -> dict:
    """Fetch quotes and upsert them into the prices table."""
    syms = symbols or all_symbols()
    if not syms:
        return {"rows": 0, "symbols": 0}
    t0 = _time.time()
    df = fetch_quotes(syms)
    if df.empty:
        raise RuntimeError("Yahoo returned no quotes")
    df = df[df["close"].notna() & (df["close"] > 0)]
    n = db.upsert_prices(
        df[["symbol", "date", "open", "high", "low", "close", "volume"]]
    )
    state = next((s for s in df["market_state"].tolist() if s), "")
    _set(
        last_quotes=now().isoformat(timespec="seconds"),
        symbols=int(df["symbol"].nunique()),
        rows=n,
        runs=int(_status.get("runs", 0)) + 1,
        market_state=state,
        error="",
    )
    return {"rows": n, "symbols": int(df["symbol"].nunique()),
            "secs": round(_time.time() - t0, 1)}


def refresh_fallback() -> dict:
    """Per-ticker yfinance download (used when the batch endpoint fails)."""
    from . import market as M
    syms = all_symbols()
    df = M.download(syms, period="5d")
    if df.empty:
        raise RuntimeError("fallback download returned nothing")
    df["symbol"] = df["symbol"].str.upper()
    n = db.upsert_prices(df)
    _set(last_quotes=now().isoformat(timespec="seconds"),
         symbols=int(df["symbol"].nunique()), rows=n,
         runs=int(_status.get("runs", 0)) + 1, error="")
    return {"rows": n, "symbols": int(df["symbol"].nunique())}


# ---------------------------------------------------------------- background
def _due(key: str, seconds: float) -> bool:
    """True if `status[key]` is older than `seconds` (or never set)."""
    age = _age(_status.get(key))
    return age is None or age >= seconds


def _loop() -> None:
    idx_sec = max(5, int(CFG["live"]["index_seconds"]))
    uni_sec = max(30, int(CFG["live"]["universe_seconds"]))
    fallback_at = 0.0            # last fallback attempt (throttled)
    while not _stop.is_set():
        started = _time.time()
        try:
            if not market_open():
                _set(market_open=False, error="")
                _stop.wait(10)
                continue
            _set(market_open=True)
            # one batch request covers indices + full universe -> fast cadence
            refresh()
            if _due("last_alerts", 60):
                _evaluate_alerts()
        except Exception as e:  # noqa: BLE001
            _set(error=f"{type(e).__name__}: {e}")
            # batch endpoint down -> slow per-ticker fallback, throttled
            if _time.time() - fallback_at > uni_sec:
                fallback_at = _time.time()
                try:
                    refresh_fallback()
                except Exception as e2:  # noqa: BLE001
                    _set(error=f"{e}; fallback: {type(e2).__name__}: {e2}")
        elapsed = _time.time() - started
        _stop.wait(max(2.0, idx_sec - elapsed))


def _evaluate_alerts() -> None:
    try:
        from . import alerts as A
        A.evaluate()
        _set(last_alerts=now().isoformat(timespec="seconds"), error="")
    except Exception as e:  # noqa: BLE001
        _set(error=f"alerts: {type(e).__name__}: {e}")


def start() -> bool:
    """Start the background refresher once (idempotent, safe on Streamlit reruns)."""
    global _thread
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return False
        _stop.clear()
        _thread = threading.Thread(target=_loop, name="live-quotes", daemon=True)
        _thread.start()
        _set(running=True)
        return True


def stop() -> None:
    _stop.set()
    _set(running=False)
