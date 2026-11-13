"""Stock screener: filter schema + query engine over scores/fundamentals/technicals."""
from __future__ import annotations

import json
from datetime import datetime

import pandas as pd

from . import db

# Range filters shown in the UI. All values are display units.
SCHEMA: list[dict] = [
    {"key": "dvs",           "label": "DVS score (0-100)",     "lo": 0,   "hi": 100, "step": 1},
    {"key": "value",         "label": "Value sub-score",       "lo": 0,   "hi": 100, "step": 1},
    {"key": "quality",       "label": "Quality sub-score",     "lo": 0,   "hi": 100, "step": 1},
    {"key": "momentum",      "label": "Momentum sub-score",    "lo": 0,   "hi": 100, "step": 1},
    {"key": "pe",            "label": "P/E (TTM)",             "lo": 0,   "hi": 150, "step": 1},
    {"key": "pb",            "label": "P/B",                   "lo": 0,   "hi": 30,  "step": 0.5},
    {"key": "roe",           "label": "ROE (%)",               "lo": -50, "hi": 80,  "step": 1},
    {"key": "debt_equity",   "label": "Debt/Equity (%)",       "lo": 0,   "hi": 300, "step": 5},
    {"key": "op_margin",     "label": "Operating margin (%)",  "lo": -50, "hi": 60,  "step": 1},
    {"key": "div_yield",     "label": "Dividend yield (%)",    "lo": 0,   "hi": 10,  "step": 0.1},
    {"key": "fcf_yield",     "label": "FCF yield (%)",         "lo": -5,  "hi": 20,  "step": 0.5},
    {"key": "ev_ebitda",     "label": "EV/EBITDA",             "lo": 0,   "hi": 60,  "step": 1},
    {"key": "mom1m",         "label": "1M momentum (%)",       "lo": -60, "hi": 150, "step": 1},
    {"key": "mom6m",         "label": "6M momentum (%)",       "lo": -80, "hi": 300, "step": 5},
    {"key": "mom12m",        "label": "12M momentum (%)",      "lo": -80, "hi": 400, "step": 5},
    {"key": "rsi14",         "label": "RSI (14)",              "lo": 0,   "hi": 100, "step": 1},
    {"key": "dist_sma200",   "label": "Distance from 200-DMA (%)", "lo": -60, "hi": 150, "step": 5},
    {"key": "dist_52w_high", "label": "Distance from 52w high (%)", "lo": -70, "hi": 10, "step": 1},
    {"key": "target_upside", "label": "Analyst target upside (%)", "lo": -50, "hi": 200, "step": 5},
    {"key": "mcap_cr",       "label": "Market cap (₹ crore, min)", "lo": 100, "hi": 500000, "step": 100},
]


def _mcap_cr(row) -> float:
    mc = row.get("market_cap") if hasattr(row, "get") else None
    try:
        return float(mc) / 1e7 if mc else float("nan")
    except (TypeError, ValueError):
        return float("nan")


def screener_frame() -> pd.DataFrame:
    """One row per symbol with scores + fundamentals + technicals."""
    from . import market as M
    from . import scores as S

    df = S.latest()
    if df.empty:
        raise RuntimeError(
            "No scores yet. Run `python -m marketdesk.jobs update` first."
        )
    closes = db.load_closes()
    if not closes.empty and len(closes) > 21:
        tech = M.technicals(closes)
        df = df.merge(tech, left_on="symbol", right_index=True, how="left")
    df["mcap_cr"] = df.apply(_mcap_cr, axis=1)
    return df


def run(df: pd.DataFrame, filters: dict) -> pd.DataFrame:
    """filters: {key: (lo, hi)} or {key: scalar(=lo)}."""
    out = df
    for key, rng in filters.items():
        if rng is None:
            continue
        if not isinstance(rng, (tuple, list)):
            rng = (rng, None)
        lo, hi = rng
        if key not in out.columns:
            continue
        s = pd.to_numeric(out[key], errors="coerce")
        if lo is not None:
            out = out[s >= float(lo)]
            s = pd.to_numeric(out[key], errors="coerce")
        if hi is not None:
            out = out[s <= float(hi)]
    return out


def save_screener(name: str, filters: dict) -> None:
    db.run(
        "INSERT OR REPLACE INTO screeners(name,query,created_at) VALUES (?,?,?)",
        (name.strip(), json.dumps(filters), datetime.now().isoformat(timespec="seconds")),
    )


def list_screeners() -> dict[str, dict]:
    return {r["name"]: json.loads(r["query"]) for r in db.rows("SELECT * FROM screeners")}


def delete_screener(name: str) -> None:
    db.run("DELETE FROM screeners WHERE name=?", (name,))


# Sensible preset filters -> nice starting points in the UI
PRESETS: dict[str, dict] = {
    "Quality compounders": {
        "dvs": (70, 100), "roe": (15, None), "debt_equity": (None, 100),
    },
    "Value picks": {
        "dvs": (60, 100), "pe": (0, 25), "pb": (0, 4),
    },
    "Momentum breakout": {
        "momentum": (70, 100), "rsi14": (45, 75), "dist_sma200": (0, None),
    },
    "High dividend": {
        "div_yield": (2, None), "debt_equity": (None, 150), "dvs": (50, 100),
    },
    "Oversold with strength": {
        "dvs": (60, 100), "rsi14": (0, 35),
    },
}
