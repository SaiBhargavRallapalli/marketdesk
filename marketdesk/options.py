"""Sensibull-style options: Black-Scholes chains, strategy payoffs, backtester.

Data model: NSE's option-chain API blocks server-side requests, so chains are
SYNTHESIZED with Black-Scholes using India VIX (^INDIAVIX) as the base IV plus
a log-moneyness skew. Greeks and premiums are model values, not exchange
quotes — always verify against your broker before trading.

Components:
  * bs / greeks          — European pricing, no scipy (math.erf)
  * current_iv / vix_series — IV from the stored ^INDIAVIX history
  * chain                — synthesized option chain around ATM
  * TEMPLATES / build_legs — Sensibull-style strategy templates
  * payoff / metrics     — P/L at expiry and after N days, breakevens, margin
  * backtest             — historical strategy backtester (entry rules,
                           mark-to-market exits, costs, equity curve)
"""
from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from . import db
from .config import CFG

RF = float(CFG["options"].get("rf", 0.065))
Q = float(CFG["options"].get("q", 0.012))
CAPITAL = float(CFG["options"].get("capital", 1_000_000))
LOTS = dict(CFG["options"].get("lots", {}))
STEPS = dict(CFG["options"].get("steps", {}))

UNDERLYINGS = {"^NSEI": "NIFTY", "^NSEBANK": "BANKNIFTY", "^BSESN": "SENSEX"}


# ================================================================ Black-Scholes
def _ncdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _npdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def bs(S: float, K: float, T: float, sigma: float, r: float = RF, q: float = Q,
       kind: str = "CE") -> float:
    """European option premium (Black-Scholes-Merton, continuous dividend q)."""
    if T <= 0:
        return max(0.0, S - K) if kind == "CE" else max(0.0, K - S)
    sigma = max(sigma, 1e-6)
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    if kind == "CE":
        return S * math.exp(-q * T) * _ncdf(d1) - K * math.exp(-r * T) * _ncdf(d2)
    return K * math.exp(-r * T) * _ncdf(-d2) - S * math.exp(-q * T) * _ncdf(-d1)


def greeks(S: float, K: float, T: float, sigma: float, kind: str = "CE",
           r: float = RF, q: float = Q) -> dict:
    """delta, gamma, theta (per day), vega (per 1 vol point)."""
    if T <= 0 or sigma <= 0:
        if kind == "CE":
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return {"delta": delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    d1 = (math.log(S / K) + (r - q + 0.5 * sigma * sigma) * T) / (sigma * math.sqrt(T))
    d2 = d1 - sigma * math.sqrt(T)
    sq = math.sqrt(T)
    disc_q, disc_r = math.exp(-q * T), math.exp(-r * T)
    gamma = disc_q * _npdf(d1) / (S * sigma * sq)
    vega = S * disc_q * _npdf(d1) * sq / 100.0          # per 1% vol move
    if kind == "CE":
        delta = disc_q * _ncdf(d1)
        theta = (-S * disc_q * _npdf(d1) * sigma / (2 * sq)
                 - r * K * disc_r * _ncdf(d2) + q * S * disc_q * _ncdf(d1)) / 365.0
    else:
        delta = -disc_q * _ncdf(-d1)
        theta = (-S * disc_q * _npdf(d1) * sigma / (2 * sq)
                 + r * K * disc_r * _ncdf(-d2) - q * S * disc_q * _ncdf(-d1)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


def smile_iv(base: float, S: float, K: float, skew: float = 0.45) -> float:
    """Simple equity-index skew: OTM puts richer, OTM calls cheaper."""
    iv = base * (1.0 - skew * math.log(max(K, 1e-9) / max(S, 1e-9)))
    return float(min(max(iv, 0.04), 3.0))


# ================================================================ IV source
def vix_series() -> pd.Series:
    """Stored ^INDIAVIX history (index=date, values in %). Fetches once if empty."""
    h = db.load_history("^INDIAVIX")
    if len(h) >= 20:
        return h["close"].astype(float)
    try:
        from .market import download
        df = download(["^INDIAVIX"], period="2y")
        if not df.empty:
            db.upsert_prices(df)
            h = db.load_history("^INDIAVIX")
            if not h.empty:
                return h["close"].astype(float)
    except Exception:  # noqa: BLE001
        pass
    return pd.Series(dtype=float)


def current_iv(default: float = 0.15) -> float:
    """Latest India VIX as a decimal (0.15 = 15%)."""
    s = vix_series()
    if s.empty:
        return default
    return float(min(max(s.iloc[-1] / 100.0, 0.05), 3.0))


# ================================================================ expiries
def next_expiries(n: int = 6, weekday: int = 3, after: date | None = None) -> list[date]:
    """Next `n` dates falling on `weekday` (0=Mon ... 4=Fri default Thu).

    NSE expiry days have changed over time — pick the weekday your broker
    shows (UI selector), verify on nseindia.com.
    """
    d = after or date.today()
    out: list[date] = []
    while len(out) < n:
        d += timedelta(days=1)
        if d.weekday() == weekday:
            out.append(d)
    return out


def lot_size(symbol: str) -> int:
    return int(LOTS.get(symbol, 50))


def strike_step(symbol: str) -> int:
    return int(STEPS.get(symbol, 50))


# ================================================================ chain
def build_chain(symbol: str, spot: float, expiry: date, iv: float | None = None,
                width: int = 10) -> pd.DataFrame:
    """Synthesized chain: strikes ±width around ATM with IV, premium, greeks."""
    iv = current_iv() if iv is None else iv
    step = strike_step(symbol)
    atm = round(spot / step) * step
    t_days = max((expiry - date.today()).days, 0)
    T = t_days / 365.0
    rows = []
    for i in range(-width, width + 1):
        K = atm + i * step
        s = smile_iv(iv, spot, K)
        cg, pg = greeks(spot, K, T, s, "CE"), greeks(spot, K, T, s, "PE")
        rows.append({
            "strike": K,
            "iv": round(s * 100, 2),
            "ce_ltp": round(bs(spot, K, T, s, kind="CE"), 2),
            "ce_delta": round(cg["delta"], 3),
            "ce_gamma": round(cg["gamma"], 5),
            "ce_theta": round(cg["theta"], 2),
            "ce_vega": round(cg["vega"], 2),
            "pe_ltp": round(bs(spot, K, T, s, kind="PE"), 2),
            "pe_delta": round(pg["delta"], 3),
            "pe_gamma": round(pg["gamma"], 5),
            "pe_theta": round(pg["theta"], 2),
            "pe_vega": round(pg["vega"], 2),
            "dist_pct": round((K / spot - 1) * 100, 2),
        })
    df = pd.DataFrame(rows)
    df.insert(0, "pos", np.where(df["strike"] == atm, "ATM",
                                 np.where(df["strike"] < atm, "ITM(CE)", "ITM(PE)")))
    return df


# ================================================================ strategies
# leg = (side +1/-1, kind CE/PE/EQ, strike offset in strikes from ATM)
TEMPLATES: dict[str, dict] = {
    "long_straddle": {"label": "Long straddle", "legs": [(1, "CE", 0), (1, "PE", 0)],
                      "help": "Buy ATM CE + PE. Bullish on volatility, direction-neutral."},
    "short_straddle": {"label": "Short straddle", "legs": [(-1, "CE", 0), (-1, "PE", 0)],
                       "help": "Sell ATM CE + PE. Earn theta, capped profit, high risk."},
    "long_strangle": {"label": "Long strangle", "legs": [(1, "CE", 2), (1, "PE", -2)],
                      "help": "Buy OTM CE + PE. Cheaper than a straddle, needs a big move."},
    "short_strangle": {"label": "Short strangle", "legs": [(-1, "CE", 2), (-1, "PE", -2)],
                       "help": "Sell OTM CE + PE. Range-bound view, undefined risk."},
    "bull_call_spread": {"label": "Bull call spread", "legs": [(1, "CE", 0), (-1, "CE", 2)],
                         "help": "Buy ATM CE, sell 2 strikes higher. Defined-risk bullish."},
    "bear_put_spread": {"label": "Bear put spread", "legs": [(1, "PE", 0), (-1, "PE", -2)],
                        "help": "Buy ATM PE, sell 2 strikes lower. Defined-risk bearish."},
    "bull_put_spread": {"label": "Bull put spread (credit)", "legs": [(-1, "PE", -1), (1, "PE", -3)],
                        "help": "Sell 1-strike OTM PE, buy further OTM. Collects credit."},
    "bear_call_spread": {"label": "Bear call spread (credit)", "legs": [(-1, "CE", 1), (1, "CE", 3)],
                         "help": "Sell 1-strike OTM CE, buy further OTM. Collects credit."},
    "iron_condor": {"label": "Iron condor", "legs": [(-1, "PE", -1), (1, "PE", -3),
                                                     (-1, "CE", 1), (1, "CE", 3)],
                    "help": "Sell OTM strangle, buy wings. Defined-risk range play."},
    "iron_butterfly": {"label": "Iron butterfly", "legs": [(-1, "PE", 0), (-1, "CE", 0),
                                                            (1, "PE", -3), (1, "CE", 3)],
                       "help": "Sell ATM strangle, buy wings. Max profit at spot at expiry."},
    "long_call": {"label": "Long call", "legs": [(1, "CE", 0)],
                  "help": "Buy ATM call. Bullish, defined risk (premium)."},
    "long_put": {"label": "Long put", "legs": [(1, "PE", 0)],
                 "help": "Buy ATM put. Bearish, defined risk (premium)."},
    "covered_call": {"label": "Covered call (futures + short CE)",
                     "legs": [(1, "EQ", 0), (-1, "CE", 1)],
                     "help": "Long spot/futures + sell 1-strike OTM CE. Yield on holdings."},
}


def build_legs(template: str, spot: float, symbol: str, lots: int = 1) -> list[dict]:
    """Resolve a template into concrete legs at the current spot."""
    if template not in TEMPLATES:
        raise ValueError(f"unknown strategy: {template}")
    step = strike_step(symbol)
    atm = round(spot / step) * step
    lot = lot_size(symbol)
    legs = []
    for side, kind, off in TEMPLATES[template]["legs"]:
        K = spot if kind == "EQ" else atm + off * step
        legs.append({"side": side, "kind": kind, "strike": float(K),
                     "qty": int(lots), "lot": lot, "ref": float(spot)})
    return legs


def entry_prices(legs: list[dict], iv: float, days_to_expiry: int) -> list[dict]:
    """Attach entry premium + greeks to each leg (T from days_to_expiry)."""
    T = max(days_to_expiry, 0) / 365.0
    out = []
    for leg in legs:
        leg = dict(leg)
        S0, K = leg["ref"], leg["strike"]
        if leg["kind"] == "EQ":
            leg.update(entry=S0, delta=1.0, gamma=0.0, theta=0.0, vega=0.0, iv=0.0)
        else:
            s = smile_iv(iv, S0, K)
            g = greeks(S0, K, T, s, leg["kind"])
            leg.update(entry=round(bs(S0, K, T, s, kind=leg["kind"]), 2),
                       iv=round(s * 100, 2), **g)
        out.append(leg)
    return out


def leg_value(leg: dict, S: float, T: float, iv: float) -> float:
    """Current value of one leg at spot S, T years to expiry, base IV iv."""
    if leg["kind"] == "EQ":
        return S
    if T <= 0:
        return max(0.0, S - leg["strike"]) if leg["kind"] == "CE" else max(0.0, leg["strike"] - S)
    s = smile_iv(iv, leg["ref"], leg["strike"])
    return bs(S, leg["strike"], T, s, kind=leg["kind"])


def position_value(legs: list[dict], S: float, T: float, iv: float) -> float:
    """Mark-to-market value of the position (signed by side)."""
    return sum(l["side"] * l["qty"] * l["lot"] * leg_value(l, S, T, iv) for l in legs)


def net_debit(legs: list[dict]) -> float:
    """Cash paid at entry (>0 debit, <0 credit), ₹ for the whole position."""
    return sum(l["side"] * l["qty"] * l["lot"] * float(l.get("entry", 0.0)) for l in legs)


def payoff_expiry(legs: list[dict], spots: np.ndarray) -> np.ndarray:
    """P/L at expiry across a spot grid (₹)."""
    debit = net_debit(legs)
    vals = []
    for S in spots:
        v = 0.0
        for l in legs:
            if l["kind"] == "EQ":
                intr = S
            elif l["kind"] == "CE":
                intr = max(0.0, S - l["strike"])
            else:
                intr = max(0.0, l["strike"] - S)
            v += l["side"] * l["qty"] * l["lot"] * intr
        vals.append(v - debit)
    return np.array(vals)


def payoff_mtm(legs: list[dict], spots: np.ndarray, days_left: int, iv: float) -> np.ndarray:
    """P/L after `days_left` days (mark-to-market, ₹)."""
    T = max(days_left, 0) / 365.0
    debit = net_debit(legs)
    return np.array([position_value(legs, float(S), T, iv) - debit for S in spots])


def breakevens(legs: list[dict], spot: float) -> list[float]:
    """Spot levels where expiry P/L crosses zero (piecewise-linear interp)."""
    grid = np.linspace(spot * 0.6, spot * 1.4, 801)
    pl = payoff_expiry(legs, grid)
    out = []
    for i in range(1, len(grid)):
        if pl[i - 1] == 0:
            out.append(float(grid[i - 1]))
        elif pl[i - 1] * pl[i] < 0:
            frac = abs(pl[i - 1]) / (abs(pl[i - 1]) + abs(pl[i]))
            out.append(float(grid[i - 1] + frac * (grid[i] - grid[i - 1])))
    # collapse near-duplicates
    uniq = []
    for b in out:
        if not uniq or abs(b - uniq[-1]) > spot * 0.001:
            uniq.append(b)
    return [round(b, 2) for b in uniq]


def _naked_qty(legs: list[dict]) -> tuple[float, float, float]:
    """(naked CE qty, naked PE qty, underlying qty) after offsetting hedges.

    Long CE covers short CE (and a long underlying hedges short CE);
    long PE covers short PE (and a short underlying hedges short PE).
    """
    ce_s = sum(l["qty"] for l in legs if l["side"] < 0 and l["kind"] == "CE")
    ce_l = sum(l["qty"] for l in legs if l["side"] > 0 and l["kind"] == "CE")
    pe_s = sum(l["qty"] for l in legs if l["side"] < 0 and l["kind"] == "PE")
    pe_l = sum(l["qty"] for l in legs if l["side"] > 0 and l["kind"] == "PE")
    eq_l = sum(l["qty"] for l in legs if l["kind"] == "EQ" and l["side"] > 0)
    eq_s = sum(l["qty"] for l in legs if l["kind"] == "EQ" and l["side"] < 0)
    naked_ce = max(ce_s - ce_l - eq_l, 0.0)
    naked_pe = max(pe_s - pe_l - eq_s, 0.0)
    eq = eq_l + eq_s
    return naked_ce, naked_pe, eq


def margin_approx(legs: list[dict], spot: float) -> float:
    """Rough margin (₹), Sensibull-style. Not official SPAN — labelled 'approx'.

    Fully covered structures (spreads, condors, butterflies, long-only):
        margin = max loss at expiry (== broker treatment of defined risk).
    Naked shorts and/or underlying legs:
        margin ≈ 12% × notional × (underlying + naked option qty)
                 + naked short premiums − long option premiums.
    """
    naked_ce, naked_pe, eq = _naked_qty(legs)
    shorts = [l for l in legs if l["side"] < 0 and l["kind"] in ("CE", "PE")]
    long_prem = sum(l["qty"] * l["lot"] * float(l.get("entry", 0.0))
                    for l in legs if l["side"] > 0 and l["kind"] != "EQ")

    def _max_loss() -> float:
        ks = sorted({l["strike"] for l in legs if l["kind"] != "EQ"})
        grid = np.unique(np.concatenate([
            np.linspace(spot * 0.3, spot * 3.0, 1201),
            np.array(ks, dtype=float), np.array([0.0, spot]),
        ]))
        return abs(min(float(payoff_expiry(legs, grid).min()), 0.0))

    # defined-risk / long-only: every short is covered and there is no
    # underlying leg -> the expiry payoff is bounded on both tails
    if eq == 0 and naked_ce == 0 and naked_pe == 0:
        return _max_loss()

    naked_prem = sum(l["qty"] * l["lot"] * float(l.get("entry", 0.0))
                     for l in shorts
                     if (l["kind"] == "CE" and naked_ce > 0)
                     or (l["kind"] == "PE" and naked_pe > 0))
    lot = int(legs[0].get("lot", 50)) if legs else 50
    m = 0.12 * spot * lot * (eq + naked_ce + naked_pe)
    return max(m + naked_prem - long_prem, 0.0)


def metrics(legs: list[dict], spot: float) -> dict:
    """Max profit/loss, breakevens, net premium, margin, ROI (at expiry)."""
    # grid must include the strikes: the payoff kinks (max loss) sit exactly there
    ks = sorted({l["strike"] for l in legs if l["kind"] != "EQ"})
    grid = np.unique(np.concatenate([
        np.linspace(spot * 0.5, spot * 1.6, 1051),
        np.array(ks, dtype=float),
        np.array([0.0, spot]),
    ]))
    pl = payoff_expiry(legs, grid)
    debit = net_debit(legs)
    margin = margin_approx(legs, spot)
    mp, ml = float(pl.max()), float(pl.min())
    bes = breakevens(legs, spot)
    return {
        "max_profit": mp,
        "max_loss": ml,
        "breakevens": bes,
        "net_premium": debit,
        "margin": margin,
        "roi_max": (mp / margin * 100) if margin > 0 else float("nan"),
        "unlimited_profit": bool(pl[-1] > pl[-2] + 1 and any(
            l["side"] > 0 and l["kind"] == "CE" and l["qty"] for l in legs)),
    }


def position_greeks(legs: list[dict], iv: float, days_to_expiry: int) -> dict:
    """Aggregated portfolio greeks (delta/gamma in index points × lots)."""
    T = max(days_to_expiry, 0) / 365.0
    tot = {"delta": 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    for l in legs:
        if l["kind"] == "EQ":
            tot["delta"] += l["side"] * l["qty"] * l["lot"]
            continue
        s = smile_iv(iv, l["ref"], l["strike"])
        g = greeks(l["ref"], l["strike"], T, s, l["kind"])
        for k in tot:
            tot[k] += l["side"] * l["qty"] * l["lot"] * g[k]
    return {k: round(v, 2) for k, v in tot.items()}


# ================================================================ backtester
def _entry_signal(rule: str, close: pd.Series, vix: pd.Series, params: dict,
                  i: int) -> bool:
    from .market import rsi, sma

    if rule == "calendar":
        every = int(params.get("every", 21))
        return i % every == 0
    if rule == "rsi":
        n = int(params.get("n", 14))
        thr = float(params.get("value", 30))
        r = rsi(close, n)
        v = r.iloc[i]
        return bool(v == v and (v < thr if params.get("op", "below") == "above" else v > thr))
    if rule == "trend":
        n = int(params.get("n", 50))
        sm = sma(close, n)
        c, s = close.iloc[i], sm.iloc[i]
        if s != s:
            return False
        return bool(c < s) if params.get("op", "below") == "below" else bool(c > s)
    if rule == "vix":
        thr = float(params.get("value", 20))
        if vix.empty:
            return False
        v = vix.reindex(close.index).ffill().iloc[i]
        if v != v:
            return False
        return bool(v > thr) if params.get("op", "above") == "above" else bool(v < thr)
    return False


def run_backtest(template: str, symbol: str, years: int = 2,
                 rule: str = "calendar", params: dict | None = None,
                 hold_days: int = 5, lots: int = 1,
                 weekday: int = 3, costs: dict | None = None) -> dict:
    """Backtest an option strategy on historical index closes + India VIX.

    Entry: signal from `rule` (calendar/RSI/trend/VIX).
    Exit : `hold_days` sessions later (MTM via B-S) or at expiry (intrinsic),
           whichever comes first. One position at a time.
    Costs: ₹20/order per leg per side + slippage on premium.
    """
    params = dict(params or {})
    costs = dict({"per_order": 20.0, "slip_pct": 0.01}, **(costs or {}))
    hist = db.load_history(symbol, start=(pd.Timestamp.today()
                                          - pd.Timedelta(days=365 * years + 60)
                                          ).strftime("%Y-%m-%d"))
    if hist.empty or len(hist) < 60:
        raise ValueError(f"No price history for {symbol} — run the daily update first.")
    close = hist["close"].astype(float)
    vix = vix_series()
    step, lot = strike_step(symbol), lot_size(symbol)

    # pre-compute expiries for the whole window
    all_exp = next_expiries(n=400, weekday=weekday,
                            after=(close.index[0] - timedelta(days=7)).date())
    exp_by_idx = {}
    for j, d in enumerate(close.index):
        dd = d.date() if hasattr(d, "date") else d
        future = [e for e in all_exp if e >= dd]
        exp_by_idx[j] = future[0] if future else all_exp[-1]

    trades: list[dict] = []
    i, n = 0, len(close)
    while i < n:
        if not _entry_signal(rule, close, vix, params, i):
            i += 1
            continue
        spot = float(close.iloc[i])
        expiry = exp_by_idx[i]
        exit_i = min(i + hold_days, n - 1)
        exit_d = close.index[exit_i].date() if hasattr(close.index[exit_i], "date") \
            else close.index[exit_i]
        # don't exit before entry; if expiry lands inside the window, settle at expiry
        at_expiry = exit_d >= expiry
        if at_expiry:
            # exit on the last session <= expiry
            while exit_i > i and (close.index[exit_i].date()
                                  if hasattr(close.index[exit_i], "date")
                                  else close.index[exit_i]) > expiry:
                exit_i -= 1
            exit_d = close.index[exit_i].date() if hasattr(close.index[exit_i], "date") \
                else close.index[exit_i]
            at_expiry = exit_d >= expiry

        dte = max((expiry - (close.index[i].date() if hasattr(close.index[i], "date")
                             else close.index[i])).days, 0)
        if dte == 0:
            i += 1
            continue

        iv_in = _iv_at(vix, close.index[i])
        legs = entry_prices(build_legs(template, spot, symbol, lots), iv_in, dte)
        # entry cash flow = premiums paid/received at entry (NOT intrinsic:
        # position_value with T=0 would return intrinsic and drop the premium)
        debit = net_debit(legs)
        entry_val = debit

        exit_spot = float(close.iloc[exit_i])
        iv_out = _iv_at(vix, close.index[exit_i])
        if at_expiry:
            t_out = 0.0
        else:
            ed = close.index[exit_i]
            ed = ed.date() if hasattr(ed, "date") else ed
            t_out = max((expiry - ed).days, 0) / 365.0
        exit_vals = [leg_value(l, exit_spot, t_out, iv_out) for l in legs]
        exit_val = sum(l["side"] * l["qty"] * l["lot"] * v
                       for l, v in zip(legs, exit_vals))

        n_legs = len(legs)
        # costs: 1% of OPTION premium turnover (both sides), 0.05% of the
        # underlying notional per side, financing carry on held underlying,
        # ₹20/order per leg per side
        opt_in = sum(abs(l["side"] * l["qty"] * l["lot"] * float(l["entry"]))
                     for l in legs if l["kind"] != "EQ")
        opt_out = sum(abs(l["side"] * l["qty"] * l["lot"] * v)
                      for l, v in zip(legs, exit_vals) if l["kind"] != "EQ")
        eq_not = sum(l["qty"] * l["lot"] * spot for l in legs if l["kind"] == "EQ")
        days_held = exit_i - i
        slip = (costs["slip_pct"] * (opt_in + opt_out)
                + costs.get("eq_slip_pct", 0.0005) * eq_not * 2)
        fin = RF * eq_not * days_held / 365.0        # carry on underlying legs
        fee = costs["per_order"] * 2 * n_legs
        gross = exit_val - entry_val
        pnl = gross - slip - fin - fee

        trades.append({
            "entry": str(close.index[i].date()),
            "exit": str(exit_d),
            "days": exit_i - i,
            "expiry": str(expiry),
            "spot_in": round(spot, 2),
            "spot_out": round(exit_spot, 2),
            "iv_in": round(iv_in * 100, 2),
            "iv_out": round(iv_out * 100, 2),
            "debit": round(debit, 0),
            "pnl": round(pnl, 0),
            "fees": round(fee + slip + fin, 0),
        })
        i = exit_i + 1                      # one position at a time

    if not trades:
        raise ValueError("No entries for these rules — try a different entry rule "
                         "or a longer period.")

    t = pd.DataFrame(trades)
    t["equity"] = CAPITAL + t["pnl"].cumsum()
    wins = t[t["pnl"] > 0]
    losses = t[t["pnl"] <= 0]
    gross_win = float(wins["pnl"].sum()) if len(wins) else 0.0
    gross_loss = float(abs(losses["pnl"].sum())) if len(losses) else 0.0
    eq = t["equity"]
    dd = float((eq - eq.cummax()).min())
    rets = eq.pct_change().dropna()
    sharpe = float(rets.mean() / rets.std() * math.sqrt(252)) if len(rets) > 2 and rets.std() > 0 else float("nan")

    metrics_out = {
        "Total P&L": f"₹{t['pnl'].sum():,.0f}",
        "Return": f"{(eq.iloc[-1] / CAPITAL - 1) * 100:.2f}%",
        "Trades": int(len(t)),
        "Win rate": f"{len(wins) / len(t) * 100:.0f}%",
        "Avg win": f"₹{wins['pnl'].mean():,.0f}" if len(wins) else "-",
        "Avg loss": f"₹{losses['pnl'].mean():,.0f}" if len(losses) else "-",
        "Profit factor": round(gross_win / gross_loss, 2) if gross_loss else "∞",
        "Max drawdown": f"₹{dd:,.0f}",
        "Sharpe (per trade)": round(sharpe, 2) if sharpe == sharpe else "-",
        "Expectancy": f"₹{t['pnl'].mean():,.0f}",
        "Avg hold (days)": round(float(t["days"].mean()), 1),
        "Fees paid": f"₹{t['fees'].sum():,.0f}",
        "Ending capital": f"₹{eq.iloc[-1]:,.0f}",
    }
    return {"trades": t, "equity": eq, "metrics": metrics_out,
            "template": template, "symbol": symbol, "rule": rule, "params": params}


def _iv_at(vix: pd.Series, ts) -> float:
    if vix.empty:
        return 0.15
    s = (vix.reindex(vix.index.union(pd.DatetimeIndex([ts])))
         .sort_index().ffill().bfill())       # bfill: ts may precede VIX history
    try:
        v = float(s.loc[ts])
    except KeyError:
        v = float(s.iloc[-1])
    if v != v:                                # still NaN (empty series slice)
        v = 15.0
    return min(max(v / 100.0, 0.05), 3.0)
