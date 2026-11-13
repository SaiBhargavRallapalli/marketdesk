"""Index constituent lists (Nifty 50 / Nifty 500) with multi-source fallback."""
from __future__ import annotations

import io
import time

import pandas as pd
import requests

from . import db
from .config import DATA

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/csv,*/*",
    "Referer": "https://www.nseindia.com/",
}

SOURCES = {
    "nifty50": [
        "https://archives.nseindia.com/content/indices/ind_nifty50list.csv",
        "https://www.niftyindices.com/IndexConstituent/ind_nifty50list.csv",
    ],
    "nifty500": [
        "https://archives.nseindia.com/content/indices/ind_nifty500list.csv",
        "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv",
    ],
}


def _to_ns(symbol: str) -> str:
    s = symbol.upper().strip()
    return s if s.endswith((".NS", ".BO")) else s + ".NS"


def _fetch_csv(url: str) -> pd.DataFrame:
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    return pd.read_csv(io.StringIO(r.text))


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    cols = {c.lower().strip(): c for c in df.columns}
    sym_col = cols.get("symbol")
    if sym_col is None:
        raise ValueError(f"no Symbol column in {list(df.columns)}")
    name_col = cols.get("company name") or cols.get("name")
    ind_col = cols.get("industry") or cols.get("sector")
    out = pd.DataFrame()
    out["symbol"] = df[sym_col].astype(str).str.strip().str.upper()
    out["symbol_ns"] = out["symbol"].map(_to_ns)
    out["name"] = df[name_col].astype(str).str.strip() if name_col else out["symbol"]
    out["industry"] = df[ind_col].astype(str).str.strip() if ind_col else ""
    out["sector"] = ""
    return out.drop_duplicates("symbol_ns").reset_index(drop=True)


def refresh(kind: str = "nifty500", max_age_days: int = 6) -> pd.DataFrame:
    """Fetch (or reuse cached) constituent list. Raises on total failure."""
    cache = DATA / f"universe_{kind}.csv"
    if cache.exists():
        age = time.time() - cache.stat().st_mtime
        if age < max_age_days * 86400:
            return pd.read_csv(cache)

    last_err: Exception | None = None
    for url in SOURCES[kind]:
        try:
            df = _normalize(_fetch_csv(url))
            df.to_csv(cache, index=False)
            return df
        except Exception as e:  # noqa: BLE001 - try next source
            last_err = e
            time.sleep(1)

    if cache.exists():  # stale cache beats nothing
        return pd.read_csv(cache)
    raise RuntimeError(
        "Could not download index constituents from NSE/niftyindices. "
        f"Drop your own CSV at {cache} (columns: Symbol,Industry). Last error: {last_err}"
    )


def load(kind: str = "nifty500", use_db: bool = False) -> pd.DataFrame:
    """Constituent list, seeded into the DB for reporting.

    Stored `symbol` is the Yahoo-style '.NS' ticker so joins with
    prices/scores/fundamentals all match.
    """
    if use_db:
        stored = db.load_universe()
        if not stored.empty:
            return stored
    df = refresh(kind)
    db.run("DELETE FROM universe")  # drop stale rows from older schemas
    db.save_universe(df.assign(symbol=df["symbol_ns"]))
    return df


def ns_symbols(kind: str = "nifty500") -> list[str]:
    return load(kind)["symbol_ns"].tolist()
