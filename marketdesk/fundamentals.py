"""Fundamentals + analyst estimates from Yahoo Finance (free)."""
from __future__ import annotations

import math
import time
from datetime import datetime, timedelta

import pandas as pd
import yfinance as yf

from . import db
from .config import CFG

# yfinance info key -> our metric name
KEY_MAP = {
    "trailingPE": "pe",
    "forwardPE": "fwd_pe",
    "priceToBook": "pb",
    "returnOnEquity": "roe",
    "returnOnAssets": "roa",
    "debtToEquity": "debt_equity",
    "currentRatio": "current_ratio",
    "grossMargins": "gross_margin",
    "operatingMargins": "op_margin",
    "profitMargins": "net_margin",
    "earningsGrowth": "earnings_growth",
    "revenueGrowth": "revenue_growth",
    "dividendYield": "div_yield",
    "payoutRatio": "payout",
    "marketCap": "market_cap",
    "enterpriseValue": "ev",
    "ebitda": "ebitda",
    "freeCashflow": "fcf",
    "beta": "beta",
    "fiftyTwoWeekHigh": "high_52w",
    "fiftyTwoWeekLow": "low_52w",
    "targetMeanPrice": "analyst_target",
    "targetHighPrice": "analyst_target_high",
    "targetLowPrice": "analyst_target_low",
    "numberOfAnalystOpinions": "analyst_count",
    "recommendationKey": "recommendation",
    "shortName": "short_name",
    "sector": "sector",
    "industry": "industry",
}


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return None
    if isinstance(v, (int, str)):
        return v
    return float(v) if isinstance(v, (float,)) else str(v)


def fetch_one(symbol: str) -> dict | None:
    try:
        info = yf.Ticker(symbol).info or {}
    except Exception:  # noqa: BLE001
        return None
    if not info:
        return None
    out: dict = {}
    for src, dst in KEY_MAP.items():
        out[dst] = _clean(info.get(src))

    # Normalise to PERCENT units: Yahoo returns these as fractions (0.15 = 15%)
    for k in ("roe", "roa", "gross_margin", "op_margin", "net_margin",
              "earnings_growth", "revenue_growth", "payout"):
        if isinstance(out.get(k), (int, float)):
            out[k] = round(float(out[k]) * 100, 2)

    # derived
    ev, ebitda = out.get("ev"), out.get("ebitda")
    out["ev_ebitda"] = round(ev / ebitda, 2) if ev and ebitda else None
    mc, fcf = out.get("market_cap"), out.get("fcf")
    out["fcf_yield"] = round(fcf / mc * 100, 2) if mc and fcf else None
    # consensus upside
    tgt, price = out.get("analyst_target"), info.get("currentPrice") or info.get("regularMarketPrice")
    out["target_upside"] = (
        round((tgt / price - 1) * 100, 2) if tgt and price else None
    )
    # earnings date (next)
    try:
        cal = yf.Ticker(symbol).get_earnings_dates(limit=4)
        if cal is not None and len(cal):
            future = cal[cal.index >= pd.Timestamp.now(tz=cal.index.tz)]
            if len(future):
                out["next_earnings"] = str(future.index[0].date())
            else:
                out["next_earnings"] = None
    except Exception:  # noqa: BLE001
        out["next_earnings"] = None
    return out


def refresh(symbols: list[str], force: bool = False, progress=None) -> int:
    ttl = timedelta(hours=CFG["cache"]["fundamentals_ttl_hours"])
    cutoff = datetime.now() - ttl
    todo = []
    existing = db.rows("SELECT symbol, fetched_at FROM fundamentals")
    fresh = {
        r["symbol"]: datetime.fromisoformat(r["fetched_at"]) for r in existing
    }
    for s in symbols:
        ts = fresh.get(s)
        if force or ts is None or ts < cutoff:
            todo.append(s)

    if progress:
        progress(f"Fetching fundamentals for {len(todo)} symbol(s)...")

    done = 0
    for i, sym in enumerate(todo, 1):
        data = fetch_one(sym)
        if data:
            db.save_fundamentals(sym, data)
            done += 1
        if progress and i % 10 == 0:
            progress(f"  {i}/{len(todo)}")
        time.sleep(0.15)  # be polite to Yahoo
    return done


def metrics_frame() -> pd.DataFrame:
    """Merged fundamental metrics for the whole universe."""
    f = db.load_fundamentals()
    if f.empty:
        return f
    keep = [c for c in [
        "symbol", "pe", "fwd_pe", "pb", "roe", "roa", "debt_equity",
        "current_ratio", "gross_margin", "op_margin", "net_margin",
        "earnings_growth", "revenue_growth", "div_yield", "payout",
        "market_cap", "ev_ebitda", "fcf_yield", "beta", "high_52w", "low_52w",
        "analyst_target", "target_upside", "analyst_count", "recommendation",
        "next_earnings", "sector", "industry", "short_name",
    ] if c in f.columns]
    return f[keep].copy()
