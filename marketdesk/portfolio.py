"""Portfolio valuation, allocation and health vs benchmark."""
from __future__ import annotations

import pandas as pd

from . import db


def valuation() -> pd.DataFrame:
    h = db.load_holdings()
    if h.empty:
        return pd.DataFrame()
    lc = db.latest_closes()[["symbol", "close", "prev_close", "chg_pct"]]
    univ = db.load_universe()
    df = h.merge(lc, on="symbol", how="left")
    if not univ.empty:
        df = df.merge(univ[["symbol", "name"]], on="symbol", how="left")
    else:
        df["name"] = df["symbol"]
    df = df.dropna(subset=["close"])
    if df.empty:
        return df
    df["value"] = df["qty"] * df["close"]
    df["invested"] = df["qty"] * df["avg_cost"]
    df["pnl"] = df["value"] - df["invested"]
    df["pnl_pct"] = (df["close"] / df["avg_cost"] - 1) * 100
    df["day_chg"] = df["value"] - df["qty"] * df["prev_close"].fillna(df["close"])
    total = df["value"].sum()
    df["weight"] = df["value"] / total * 100 if total else 0
    return df.sort_values("value", ascending=False)


def summary() -> dict:
    df = valuation()
    if df.empty:
        return {"holdings": 0}
    total = df["value"].sum()
    invested = df["invested"].sum()
    winners = int((df["pnl"] > 0).sum())
    losers = int((df["pnl"] < 0).sum())
    top = df.iloc[0]
    return {
        "holdings": len(df),
        "value": float(total),
        "invested": float(invested),
        "pnl": float(total - invested),
        "pnl_pct": float((total / invested - 1) * 100) if invested else 0.0,
        "day_chg": float(df["day_chg"].sum()),
        "winners": winners,
        "losers": losers,
        "top_symbol": top["symbol"],
        "top_weight": float(top["weight"]),
        "best": df.loc[df["pnl_pct"].idxmax()][["symbol", "pnl_pct"]].to_dict(),
        "worst": df.loc[df["pnl_pct"].idxmin()][["symbol", "pnl_pct"]].to_dict(),
    }


def sector_allocation() -> pd.DataFrame:
    df = valuation()
    if df.empty:
        return pd.DataFrame()
    metrics = None
    from . import fundamentals as F
    metrics = F.metrics_frame()
    if not metrics.empty and "sector" in metrics.columns:
        df = df.merge(metrics[["symbol", "sector"]], on="symbol", how="left")
    else:
        df["sector"] = "Unknown"
    df["sector"] = df["sector"].fillna("Unknown")
    out = df.groupby("sector", as_index=False).agg(
        value=("value", "sum"), holdings=("symbol", "count")
    )
    out["weight"] = out["value"] / out["value"].sum() * 100
    return out.sort_values("value", ascending=False)


def series() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Historical portfolio value vs NIFTY 50 (same window)."""
    df = valuation()
    if df.empty:
        return pd.DataFrame(), pd.DataFrame()
    frames = []
    for _, r in df.iterrows():
        h = db.load_history(r["symbol"])
        if h.empty:
            continue
        s = h["close"] * r["qty"]
        s.name = r["symbol"]
        frames.append(s)
    if not frames:
        return pd.DataFrame(), pd.DataFrame()
    port = pd.concat(frames, axis=1).ffill().dropna(how="all")
    port_total = port.sum(axis=1).to_frame("portfolio")
    nifty = db.load_history("^NSEI")
    if not nifty.empty:
        nifty = nifty["close"].reindex(port_total.index).ffill()
        # scale nifty to portfolio starting value
        bench = (nifty / nifty.iloc[0]) * port_total["portfolio"].iloc[0]
        port_total["nifty"] = bench
    return port_total, port


def health() -> dict:
    df = valuation()
    if df.empty:
        return {}
    total = df["value"].sum()
    weights = df["value"] / total
    hhi = float((weights ** 2).sum())  # herfindahl: 1 = single stock
    return {
        "concentration_hhi": round(hhi, 3),
        "top3_weight": float(weights.nlargest(3).sum() * 100),
        "diversification": (
            "Very concentrated" if hhi > 0.25 else
            "Concentrated" if hhi > 0.12 else
            "Well diversified" if hhi > 0.06 else "Very diversified"
        ),
        "cash_like": 0.0,
    }
