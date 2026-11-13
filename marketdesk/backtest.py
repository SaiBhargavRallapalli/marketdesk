"""Vectorised backtesting: SMA cross, RSI reversion, momentum rotation, buy&hold."""
from __future__ import annotations

import numpy as np
import pandas as pd

from .config import CFG
from .market import rsi, sma

RF = 0.065  # risk-free rate (India, annual)


def _metrics(equity: pd.Series, n_exposure_days: int = 0) -> dict:
    equity = equity.dropna()
    if len(equity) < 2:
        return {}
    ret = equity.pct_change().dropna()
    total = equity.iloc[-1] / equity.iloc[0] - 1
    years = len(equity) / 252
    cagr = (equity.iloc[-1] / equity.iloc[0]) ** (1 / max(years, 1e-9)) - 1
    vol = ret.std() * np.sqrt(252)
    sharpe = ((ret.mean() * 252) - RF) / vol if vol > 0 else float("nan")
    downside = ret[ret < 0].std() * np.sqrt(252)
    sortino = ((ret.mean() * 252) - RF) / downside if downside and downside > 0 else float("nan")
    dd = equity / equity.cummax() - 1
    maxdd = dd.min()
    calmar = cagr / abs(maxdd) if maxdd < 0 else float("nan")
    in_pos = ret[n_exposure_days:] if n_exposure_days else ret
    win = (in_pos > 0).mean() * 100
    return {
        "Total return": f"{total * 100:.1f}%",
        "CAGR": f"{cagr * 100:.1f}%",
        "Volatility (ann.)": f"{vol * 100:.1f}%",
        "Sharpe": round(float(sharpe), 2),
        "Sortino": round(float(sortino), 2),
        "Max drawdown": f"{maxdd * 100:.1f}%",
        "Calmar": round(float(calmar), 2),
        "Win rate (% days in profit)": f"{win:.0f}%",
        "Years": round(years, 1),
    }


def _apply_costs(ret: pd.Series, pos: pd.Series, cost_bps: float) -> pd.Series:
    turns = pos.diff().abs().fillna(0)
    return ret * pos - turns * (cost_bps / 10_000)


def _buy_hold(close: pd.DataFrame, cost_bps: float) -> tuple[pd.Series, pd.Series, int]:
    ret = close.pct_change().mean(axis=1)  # equal-weight universe
    pos = pd.Series(1.0, index=ret.index)
    net = ret - 0  # one-time entry cost handled below
    equity = (1 + net.fillna(0)).cumprod()
    equity.iloc[0] = 1.0
    return equity, ret, 0


def _sma_cross(close: pd.DataFrame, fast: int, slow: int, cost_bps: float):
    s = close.iloc[:, 0] if close.shape[1] == 1 else close.mean(axis=1)
    signal = (sma(s, fast) > sma(s, slow)).astype(float)
    pos = signal.shift(1).fillna(0.0)
    ret = s.pct_change().fillna(0)
    net = _apply_costs(ret, pos, cost_bps)
    equity = (1 + net).cumprod()
    trades = int((pos.diff() == 1).sum())
    return equity, ret, trades, pos


def _rsi_reversion(close: pd.DataFrame, n: int, buy: float, sell: float, cost_bps: float):
    s = close.iloc[:, 0] if close.shape[1] == 1 else close.mean(axis=1)
    r = rsi(s, n)
    enter, exit_ = r < buy, r > sell
    state = pd.Series(np.where(enter, 1.0, np.where(exit_, 0.0, np.nan)), index=s.index)
    pos = state.ffill().fillna(0.0)
    ret = s.pct_change().fillna(0)
    net = _apply_costs(ret, pos, cost_bps)
    equity = (1 + net).cumprod()
    trades = int((pos.diff() == 1).sum())
    return equity, ret, trades, pos


def _rotation(close: pd.DataFrame, lookback: int, top_n: int, cost_bps: float,
              positive_only: bool = True):
    """Monthly equal-weight rotation into top-N by momentum (data up to rebalance only)."""
    ret = close.pct_change().fillna(0)
    mom = close.pct_change(lookback)
    try:
        rebal_dates = close.resample("ME").last().index
    except ValueError:  # older pandas
        rebal_dates = close.resample("M").last().index
    rebal_dates = [d for d in rebal_dates if d in close.index]
    if len(rebal_dates) < 2:
        return _buy_hold(close, cost_bps)

    port = pd.Series(0.0, index=close.index)
    weights = pd.DataFrame(0.0, index=close.index, columns=close.columns)
    exposure_days = 0
    for i, d in enumerate(rebal_dates[:-1]):
        nxt = rebal_dates[i + 1]
        m = mom.loc[:d].iloc[-1].dropna().sort_values(ascending=False)
        if positive_only:
            m = m[m > 0]
        picks = m.head(top_n).index.tolist()
        seg = close.loc[(close.index > d) & (close.index <= nxt)]
        if seg.empty or not picks:
            continue
        w = 1.0 / len(picks)
        weights.loc[seg.index, picks] = w
        exposure_days += len(seg)

    port_ret = (weights.shift(1).fillna(0) * ret).sum(axis=1)
    # proportional transaction cost on weight change
    turnover = weights.diff().abs().sum(axis=1).fillna(0)
    net = port_ret - turnover * (cost_bps / 10_000)
    equity = (1 + net).cumprod()
    trades = int((weights.diff().sum(axis=1) > 0).sum())
    return equity, ret.mean(axis=1), trades, weights.sum(axis=1), exposure_days


def run_backtest(strategy: str, prices: pd.DataFrame, params: dict | None = None,
                 cost_bps: float | None = None, capital: float | None = None) -> dict:
    """prices: wide close matrix (date x symbol).

    Returns {equity, benchmark, metrics, trades, strategy, params}.
    """
    params = params or {}
    cost_bps = CFG["backtest"]["cost_bps"] if cost_bps is None else cost_bps
    capital = CFG["backtest"]["initial_capital"] if capital is None else capital
    if prices.empty or len(prices) < 30:
        raise ValueError("Not enough price data to backtest (need ~30+ sessions).")
    prices = prices.copy()
    prices.index = pd.to_datetime(prices.index)

    exposure_days = 0
    if strategy == "buy_hold":
        equity, bench_ret, trades = _buy_hold(prices, cost_bps)
        exposure_days = len(prices)
    elif strategy == "sma_cross":
        fast = int(params.get("fast", 20))
        slow = int(params.get("slow", 100))
        equity, bench_ret, trades, pos = _sma_cross(prices, fast, slow, cost_bps)
        exposure_days = int(pos.sum())
    elif strategy == "rsi_reversion":
        n = int(params.get("n", 14))
        buy = float(params.get("buy", 30))
        sell = float(params.get("sell", 60))
        equity, bench_ret, trades, pos = _rsi_reversion(prices, n, buy, sell, cost_bps)
        exposure_days = int(pos.sum())
    elif strategy == "momentum_rotation":
        lookback = int(params.get("lookback", 126))
        top_n = int(params.get("top_n", 5))
        positive_only = bool(params.get("positive_only", True))
        res = _rotation(prices, lookback, top_n, cost_bps, positive_only)
        equity, bench_ret, trades, _pos, exposure_days = res
    else:
        raise ValueError(f"unknown strategy: {strategy}")

    equity = equity * capital
    bench = (1 + bench_ret.fillna(0)).cumprod() * capital

    metrics = _metrics(equity, n_exposure_days=0)
    bench_metrics = _metrics(bench)
    metrics["Benchmark CAGR"] = bench_metrics.get("CAGR", "-")
    metrics["Benchmark Max DD"] = bench_metrics.get("Max drawdown", "-")
    metrics["Trades"] = trades
    metrics["Exposure"] = f"{100 * exposure_days / len(prices):.0f}%"
    metrics["Cost/model"] = f"{cost_bps} bps/side"

    return {
        "equity": equity,
        "benchmark": bench,
        "metrics": metrics,
        "trades": trades,
        "strategy": strategy,
        "params": params,
    }


STRATEGIES = {
    "sma_cross": {
        "label": "SMA crossover (fast > slow = long)",
        "params": {"fast": 20, "slow": 100},
        "help": "Golden/death cross on a single stock or equal-weight basket.",
    },
    "rsi_reversion": {
        "label": "RSI mean reversion",
        "params": {"n": 14, "buy": 30, "sell": 60},
        "help": "Buy when RSI < buy, exit when RSI > sell.",
    },
    "momentum_rotation": {
        "label": "Momentum rotation (monthly, top-N)",
        "params": {"lookback": 126, "top_n": 5, "positive_only": True},
        "help": "Monthly rebalance into the top N momentum names (6m lookback).",
    },
    "buy_hold": {
        "label": "Buy & hold (benchmark)",
        "params": {},
        "help": "Equal-weight buy and hold of the selected basket.",
    },
}
