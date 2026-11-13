"""DVS - a free, transparent composite score (our DVM alternative).

DVS = weighted percentile rank across the universe:
  Value     (25%)  cheap on PE, PB, EV/EBITDA, high FCF yield
  Quality   (25%)  high ROE/ROA/margins, low debt
  Momentum  (30%)  1/3/6/12-month price momentum
  Growth    (10%)  earnings & revenue growth
  Dividend  (10%)  yield + payout consistency
All sub-scores are 0-100. Higher = better.
"""
from __future__ import annotations

from datetime import date as date_cls

import numpy as np
import pandas as pd

from . import db
from .config import CFG


def _pct_rank(series: pd.Series, invert: bool = False) -> pd.Series:
    """Percentile rank 0..100, NaN stays NaN."""
    r = series.rank(pct=True) * 100
    if invert:
        r = 100 - r
    return r


def _composite(df: pd.DataFrame, cols: list[str], weights: list[float]) -> pd.Series:
    """Weighted mean of available sub-metrics; NaN-metrics get neutral 50."""
    parts, wts = [], []
    for col, w in zip(cols, weights):
        if col not in df.columns:
            continue
        parts.append(df[col].fillna(50.0))
        wts.append(w)
    if not parts:
        return pd.Series(np.nan, index=df.index)
    mat = pd.concat(parts, axis=1)
    wt = pd.Series(wts, index=mat.columns)
    # renormalize weights over available columns
    return mat.mul(wt, axis=1).sum(axis=1) / wt.sum()


def compute_scores() -> pd.DataFrame:
    """Compute DVS scores for everything with prices + fundamentals."""
    closes = db.load_closes()
    if closes.empty:
        raise RuntimeError("No price data. Run `python -m marketdesk.jobs update` first.")
    # drop index tickers — score stocks only
    closes = closes[[c for c in closes.columns if not str(c).startswith("^")]]

    from . import fundamentals as F  # avoid circular import at module load
    metrics = F.metrics_frame()

    # ---- technical / momentum metrics from prices
    tech = pd.DataFrame(index=closes.columns)
    ret = lambda n: closes.pct_change(n).iloc[-1] * 100  # noqa: E731
    tech["mom1m"], tech["mom3m"] = ret(21), ret(63)
    tech["mom6m"], tech["mom12m"] = ret(126), ret(252)
    # 12-1 momentum: skip the most recent month (classic dual-momentum)
    mom12 = closes.pct_change(252)
    mom1 = closes.pct_change(21)
    tech["mom12_1"] = (mom12 - mom1).iloc[-1] * 100

    df = tech
    if not metrics.empty:
        df = df.join(metrics.set_index("symbol"), how="left")

    # ---- value
    df["v_pe"] = _pct_rank(df.get("pe"), invert=True)
    df["v_pb"] = _pct_rank(df.get("pb"), invert=True)
    df["v_ev"] = _pct_rank(df.get("ev_ebitda"), invert=True)
    df["v_fcf"] = _pct_rank(df.get("fcf_yield"))
    df["value"] = _composite(df, ["v_pe", "v_pb", "v_ev", "v_fcf"], [1, 1, 1, 1])

    # ---- quality
    df["q_roe"] = _pct_rank(df.get("roe"))
    df["q_roa"] = _pct_rank(df.get("roa"))
    df["q_op"] = _pct_rank(df.get("op_margin"))
    df["q_net"] = _pct_rank(df.get("net_margin"))
    df["q_debt"] = _pct_rank(df.get("debt_equity"), invert=True)
    df["q_cur"] = _pct_rank(df.get("current_ratio"))
    df["quality"] = _composite(
        df, ["q_roe", "q_roa", "q_op", "q_net", "q_debt", "q_cur"],
        [2, 1, 1, 1, 2, 1],
    )

    # ---- momentum (percentile-rank the raw returns first!)
    df["m_mom1"] = _pct_rank(df["mom1m"])
    df["m_mom3"] = _pct_rank(df["mom3m"])
    df["m_mom6"] = _pct_rank(df["mom6m"])
    df["m_mom121"] = _pct_rank(df["mom12_1"])
    df["m_score"] = _composite(
        df, ["m_mom1", "m_mom3", "m_mom6", "m_mom121"], [1, 1, 2, 2],
    )
    df["momentum"] = df["m_score"]

    # ---- growth
    df["g_earn"] = _pct_rank(df.get("earnings_growth"))
    df["g_rev"] = _pct_rank(df.get("revenue_growth"))
    df["growth"] = _composite(df, ["g_earn", "g_rev"], [1, 1])

    # ---- dividend
    df["d_yield"] = _pct_rank(df.get("div_yield"))
    df["d_payout"] = _pct_rank(df.get("payout"))
    df["dividend"] = _composite(df, ["d_yield", "d_payout"], [2, 1])

    # ---- composite
    w = CFG["scoring"]["weights"]
    df["dvs"] = _composite(
        df,
        ["value", "quality", "momentum", "growth", "dividend"],
        [w["value"], w["quality"], w["momentum"], w["growth"], w["dividend"]],
    )
    df = df.dropna(subset=["dvs"])
    df["rank"] = df["dvs"].rank(ascending=False).astype(int)

    out = df[["dvs", "value", "quality", "momentum", "growth", "dividend", "rank"]].copy()
    out = out.round(1).reset_index().rename(columns={"index": "symbol"})
    db.save_scores(date_cls.today().isoformat(), out)
    return out.sort_values("rank")


def latest() -> pd.DataFrame:
    scores = db.latest_scores()
    if scores.empty:
        return scores
    from . import fundamentals as F
    metrics = F.metrics_frame()
    universe = db.load_universe()
    merged = scores.merge(metrics, on="symbol", how="left")
    if not universe.empty:
        merged = merged.merge(
            universe[["symbol", "name"]], on="symbol", how="left"
        )
    return merged
