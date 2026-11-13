"""MarketDesk — Streamlit dashboard.

Run:  streamlit run app.py
"""
from __future__ import annotations

import json
from datetime import date

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from marketdesk import alerts as A
from marketdesk import db, market as M, portfolio as P, reports as R
from marketdesk import scores as S, screener as SC
from marketdesk import backtest as BT
from marketdesk.config import CFG, ENV_FILE, ROOT, env, email_ready, telegram_ready

st.set_page_config(page_title="MarketDesk", page_icon="📊", layout="wide")
db.init_db()

DISCLAIMER = ("⚠️ Model scores and backtests are research aids, **not investment "
              "advice**. Data from free sources may be delayed or incomplete.")


# ================================================================ helpers
@st.cache_data(ttl=1800, show_spinner=False)
def _scores_latest() -> pd.DataFrame:
    return S.latest()


@st.cache_data(ttl=1800, show_spinner=False)
def _screener_frame() -> pd.DataFrame:
    return SC.screener_frame()


@st.cache_data(ttl=600, show_spinner=False)
def _closes() -> pd.DataFrame:
    return db.load_closes()


@st.cache_data(ttl=600, show_spinner=False)
def _movers() -> tuple[pd.DataFrame, pd.DataFrame]:
    return M.movers()


def _fmt_cr(v) -> str:
    try:
        v = float(v)
        if v >= 1e7:
            return f"₹{v / 1e7:,.2f} Cr"
        if v >= 1e5:
            return f"₹{v / 1e5:,.2f} L"
        return f"₹{v:,.0f}"
    except (TypeError, ValueError):
        return "-"


def _score_chip(v: float) -> str:
    color = "#0a8a3e" if v >= 70 else "#f5a623" if v >= 40 else "#d0342c"
    return f"<span style='color:{color};font-weight:700'>{v:.0f}</span>"


def _write_env(updates: dict) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    out, seen = [], set()
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else None
        if key and key in updates:
            out.append(f"{key}={updates[key]}")
            seen.add(key)
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    ENV_FILE.write_text("\n".join(out) + "\n")
    # reload into current process
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE, override=True)


# ================================================================ sidebar
with st.sidebar:
    st.markdown("## 📊 MarketDesk")
    st.caption("Self-hosted Trendlyne alternative · free data")
    n_rows = db.rows("SELECT COUNT(*) c FROM prices")[0]["c"] if db.DB_PATH.exists() else 0
    last = db.rows("SELECT MAX(date) d FROM prices")[0]["d"]
    st.caption(f"prices: {n_rows:,} rows · latest: {last or '—'}")
    st.caption(f"DVS universe: {db.rows('SELECT COUNT(*) c FROM universe')[0]['c']} stocks")
    st.divider()
    if st.button("🔄 Refresh prices", width="stretch"):
        with st.spinner("Downloading..."):
            try:
                univ = db.load_universe()
                syms = univ["symbol"].tolist() if not univ.empty else []
                n = M.update_prices(syms, progress=st.write)
                st.cache_data.clear()
                st.success(f"Updated {n} rows")
            except Exception as e:  # noqa: BLE001
                st.error(f"Update failed: {e}")
    if st.button("🧮 Recompute DVS scores", width="stretch"):
        try:
            with st.spinner("Scoring..."):
                df = S.compute_scores()
                st.cache_data.clear()
                st.success(f"Scored {len(df)} stocks")
        except Exception as e:  # noqa: BLE001
            st.error(str(e))
    st.divider()
    tg_ok, mail_ok = telegram_ready(), email_ready()
    st.caption(f"Telegram {'✅' if tg_ok else '⬜'} · Email {'✅' if mail_ok else '⬜'}")
    st.caption(DISCLAIMER)

tab_dash, tab_screener, tab_backtest, tab_pf, tab_stock, tab_reports, tab_alerts, tab_settings = st.tabs(
    ["🏠 Dashboard", "🔍 Screener", "🧪 Backtest", "💼 Portfolio",
     "📈 Stock", "📰 Reports", "🔔 Alerts", "⚙️ Settings"]
)

# ================================================================ dashboard
with tab_dash:
    try:
        idx = M.index_snapshot()
        br = M.breadth()
        if idx.empty:
            st.info("No index data yet — run `python -m marketdesk.jobs update`.")
        else:
            cols = st.columns(len(idx) + 1)
            for i, (_, r) in enumerate(idx.iterrows()):
                cols[i].metric(r["index"], f"{r['value']:,.0f}", f"{r['change_pct']:+.2f}%")
            tot = br["advances"] + br["declines"] + br["unchanged"]
            cols[-1].metric(
                "Breadth (A/D/U)",
                f"{br['advances']}/{br['declines']}/{br['unchanged']}",
                f"{br.get('date','')}",
            )
            if tot:
                fig = go.Figure(go.Indicator(
                    mode="gauge+number", value=br["advances"],
                    title={"text": "Advances"},
                    gauge={
                        "axis": {"range": [0, tot]},
                        "bar": {"color": "#0a8a3e"},
                        "steps": [
                            {"range": [0, tot // 2], "color": "#ffe3e1"},
                            {"range": [tot // 2, tot], "color": "#e3f6e9"},
                        ],
                    },
                ))
                fig.update_layout(height=220, margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig, width="stretch")

        gainers, losers = _movers()
        c1, c2 = st.columns(2)
        with c1:
            st.subheader("🔺 Top gainers")
            if not gainers.empty:
                g = gainers.copy()
                g["chg_pct"] = g["chg_pct"].map(lambda v: f"{v:+.2f}%")
                st.dataframe(g, width="stretch", hide_index=True, height=300)
        with c2:
            st.subheader("🔻 Top losers")
            if not losers.empty:
                lo = losers.copy()
                lo["chg_pct"] = lo["chg_pct"].map(lambda v: f"{v:+.2f}%")
                st.dataframe(lo, width="stretch", hide_index=True, height=300)

        st.subheader("⭐ Top DVS scores")
        sc = _scores_latest()
        if sc.empty:
            st.info("No scores yet — hit **Recompute DVS scores** in the sidebar.")
        else:
            show_cols = [c for c in ["rank", "symbol", "name", "dvs", "value", "quality",
                                     "momentum", "growth", "dividend", "pe", "roe",
                                     "mom6m", "target_upside"]
                         if c in sc.columns]
            top = sc.head(15)[show_cols].copy()
            top = top.round(1)
            for c in ("dvs", "value", "quality", "momentum", "growth", "dividend"):
                if c in top.columns:
                    top[c] = top[c].map(_score_chip)
            st.markdown(top.to_html(escape=False, index=False), unsafe_allow_html=True)
            csv = sc.to_csv(index=False).encode()
            st.download_button("⬇️ Full DVS table (CSV)", csv, "dvs_scores.csv",
                               mime="text/csv")

        pf = P.summary()
        if pf.get("holdings"):
            st.subheader("💼 Portfolio snapshot")
            k = st.columns(5)
            k[0].metric("Value", _fmt_cr(pf["value"]))
            k[1].metric("Overall P&L", _fmt_cr(pf["pnl"]), f"{pf['pnl_pct']:+.2f}%")
            k[2].metric("Today", _fmt_cr(pf["day_chg"]))
            k[3].metric("Winners / Losers", f"{pf['winners']} / {pf['losers']}")
            k[4].metric("Top holding", f"{pf['top_symbol']} ({pf['top_weight']:.0f}%)")
    except Exception as e:  # noqa: BLE001
        st.error(f"Dashboard error: {e}")

# ================================================================ screener
def _apply_slider_filters(flt: dict) -> None:
    """Set screener slider widget states (used by preset/load callbacks)."""
    for spec in SC.SCHEMA:
        k = spec["key"]
        rng = flt.get(k, (spec["lo"], spec["hi"]))
        if not isinstance(rng, (tuple, list)):
            rng = (rng, spec["hi"])
        lo = spec["lo"] if rng[0] is None else float(rng[0])
        hi = spec["hi"] if rng[1] is None else float(rng[1])
        st.session_state[f"sl_{k}"] = (lo, hi)


with tab_screener:
    st.subheader("🔍 Stock screener")
    st.caption("Filter on DVS scores, fundamentals and technicals. "
               "Leave sliders at full range to ignore them.")

    top1, top2 = st.columns(2)
    preset = top1.selectbox(
        "Preset", ["(custom)"] + list(SC.PRESETS.keys()), key="preset_sel",
        on_change=lambda: _apply_slider_filters(
            SC.PRESETS.get(st.session_state["preset_sel"], {})),
    )
    saved = SC.list_screeners()
    if saved:
        top2.selectbox(
            "Load saved screener", ["—"] + list(saved), key="load_sel",
            on_change=lambda: _apply_slider_filters(
                SC.list_screeners().get(st.session_state["load_sel"], {})
                if st.session_state["load_sel"] != "—" else {}),
        )

    filters: dict = {}
    with st.expander("Filters", expanded=True):
        cols = st.columns(4)
        for i, spec in enumerate(SC.SCHEMA):
            key, lab = spec["key"], spec["label"]
            with cols[i % 4]:
                val = st.slider(lab, float(spec["lo"]), float(spec["hi"]),
                                (float(spec["lo"]), float(spec["hi"])),
                                step=float(spec["step"]), key=f"sl_{key}")
                if val != (spec["lo"], spec["hi"]):
                    filters[key] = val

    try:
        frame = _screener_frame()
        result = SC.run(frame, filters)
        st.markdown(f"**{len(result)} / {len(frame)} stocks match**")

        if not result.empty:
            view_cols = [c for c in ["rank", "symbol", "name", "dvs", "pe", "pb", "roe",
                                     "debt_equity", "div_yield", "mom6m", "rsi14",
                                     "target_upside", "mcap_cr", "sector"]
                         if c in result.columns]
            show = result.sort_values("dvs", ascending=False)[view_cols]
            st.dataframe(show, width="stretch", hide_index=True, height=430)
            st.download_button("⬇️ Export CSV", show.to_csv(index=False).encode(),
                               "screener_result.csv", mime="text/csv")

            c1, c2, c3 = st.columns([2, 2, 1])
            with c1:
                name = st.text_input("Save as", key="save_name",
                                     placeholder="my quality screen")
            with c2:
                st.caption("Saved screeners load from the dropdown above.")
            with c3:
                if st.button("💾 Save", disabled=not name.strip()):
                    SC.save_screener(name.strip(), filters)
                    st.success("Saved")
                    st.rerun()
        else:
            st.warning("No stocks match. Widen some sliders.")
    except Exception as e:  # noqa: BLE001
        st.info(f"{e}")

# ================================================================ backtest
with tab_backtest:
    st.subheader("🧪 Backtest")
    st.caption("Vectorised, long-only, costs applied. Benchmark = equal-weight "
               "of the selected basket. Past ≠ future.")

    c1, c2, c3 = st.columns(3)
    with c1:
        strat_key = st.selectbox(
            "Strategy",
            list(BT.STRATEGIES),
            format_func=lambda k: BT.STRATEGIES[k]["label"],
        )
    spec = BT.STRATEGIES[strat_key]
    st.caption(spec["help"])

    # instrument
    all_syms = db.load_universe()
    with c2:
        universe_pick = st.selectbox("Basket", ["Nifty 50", "Nifty 500", "My portfolio"])
    with c3:
        years = st.selectbox("Period", [1, 2, 3], index=1)
        symbol_pick = st.selectbox(
            "Instrument (single-stock strategies)",
            ["Equal-weight basket"] + (all_syms["symbol"].tolist() if not all_syms.empty else []),
        )

    params = dict(spec["params"])
    pc1, pc2, pc3 = st.columns(3)
    if strat_key == "sma_cross":
        params["fast"] = pc1.number_input("Fast SMA", 5, 100, int(params["fast"]))
        params["slow"] = pc2.number_input("Slow SMA", 20, 400, int(params["slow"]))
    elif strat_key == "rsi_reversion":
        params["n"] = pc1.number_input("RSI period", 5, 30, int(params["n"]))
        params["buy"] = pc2.number_input("Buy below", 5, 50, int(params["buy"]))
        params["sell"] = pc3.number_input("Sell above", 30, 95, int(params["sell"]))
    elif strat_key == "momentum_rotation":
        params["lookback"] = pc1.selectbox("Lookback (days)", [63, 126, 252], index=1)
        params["top_n"] = pc2.number_input("Top N", 2, 15, int(params["top_n"]))
        params["positive_only"] = pc3.checkbox("Only positive momentum",
                                               value=bool(params["positive_only"]))

    if st.button("▶️ Run backtest", type="primary"):
        try:
            closes = _closes()
            if closes.empty:
                st.info("No data yet — run the daily update first.")
            else:
                start = (pd.Timestamp.today() - pd.Timedelta(days=365 * years))
                closes = closes[closes.index >= start.strftime("%Y-%m-%d")]
                if universe_pick == "Nifty 50":
                    from marketdesk import universe as U
                    syms = set(U.load("nifty50")["symbol_ns"])
                elif universe_pick == "My portfolio":
                    syms = set(db.load_holdings()["symbol"])
                else:
                    syms = set(closes.columns)
                basket = closes[[c for c in closes.columns if c in syms]].copy()
                basket = basket.dropna(axis=1, how="all")
                # keep symbols with >=80% data, forward-fill gaps
                basket = basket.loc[:, basket.notna().mean() >= 0.8].ffill()
                if basket.empty:
                    st.warning("No symbols with enough data in this basket.")
                else:
                    if strat_key in ("sma_cross", "rsi_reversion") and symbol_pick != "Equal-weight basket":
                        if symbol_pick not in basket.columns:
                            st.warning(f"{symbol_pick} has no cached data for this period.")
                        else:
                            basket = basket[[symbol_pick]]
                    res = BT.run_backtest(strat_key, basket, params)

                    eq = res["equity"]
                    fig = go.Figure()
                    fig.add_trace(go.Scatter(x=eq.index, y=eq, name="Strategy",
                                             line=dict(color="#3a49b8", width=2)))
                    fig.add_trace(go.Scatter(x=res["benchmark"].index,
                                             y=res["benchmark"], name="Benchmark",
                                             line=dict(color="#999", width=1.5, dash="dot")))
                    fig.update_layout(height=380, template="plotly_white",
                                      margin=dict(l=10, r=10, t=30, b=10),
                                      yaxis_title="Value (₹)")
                    st.plotly_chart(fig, width="stretch")

                    m = pd.DataFrame(list(res["metrics"].items()),
                                     columns=["Metric", "Value"])
                    st.dataframe(m, hide_index=True, width="stretch", height=330)
                    st.caption(f"Started ₹{eq.iloc[0]:,.0f} → ended ₹{eq.iloc[-1]:,.0f}")
        except Exception as e:  # noqa: BLE001
            st.error(f"Backtest failed: {e}")

# ================================================================ portfolio
with tab_pf:
    st.subheader("💼 Portfolio")

    imp1, imp2 = st.columns([3, 1])
    with imp1:
        from marketdesk import brokers as BR
        avail = BR.available()
        br_pick = st.selectbox(
            "Import from broker",
            [k for k, v in avail.items() if v] or ["manual"],
            format_func=lambda k: f"{k} {'(configured)' if avail.get(k) else ''}",
        )
    with imp2:
        if st.button("⤓ Import holdings", disabled=not avail.get(br_pick, False)):
            try:
                n = BR.import_holdings(br_pick)
                st.success(f"Imported {n} holdings from {br_pick}")
            except Exception as e:  # noqa: BLE001
                st.error(str(e))

    with st.expander("➕ Add / update holding", expanded=False):
        univ = db.load_universe()
        f1, f2, f3, f4 = st.columns([2, 1, 1, 1])
        sym_opts = univ["symbol"].tolist() if not univ.empty else []
        sym = f1.text_input("Symbol (NSE, e.g. RELIANCE.NS)",
                            value=sym_opts[0] if sym_opts else "RELIANCE.NS")
        qty = f2.number_input("Quantity", 1.0, 1e6, 1.0, step=1.0)
        cost = f3.number_input("Avg cost (₹)", 0.0, 1e6, 100.0, step=1.0)
        if f4.button("Add"):
            try:
                db.add_holding(sym.strip(), qty, cost)
                st.success(f"Added {sym}")
            except Exception as e:  # noqa: BLE001
                st.error(str(e))

    val = P.valuation()
    if val.empty:
        st.info("No holdings yet — add a stock above (or run the update so prices exist).")
    else:
        s = P.summary()
        k = st.columns(5)
        k[0].metric("Invested", _fmt_cr(s["invested"]))
        k[1].metric("Current value", _fmt_cr(s["value"]))
        k[2].metric("Overall P&L", _fmt_cr(s["pnl"]), f"{s['pnl_pct']:+.2f}%")
        k[3].metric("Today's change", _fmt_cr(s["day_chg"]))
        k[4].metric("Holdings", s["holdings"], f"{s['winners']}W / {s['losers']}L")

        show = val[["symbol", "name", "qty", "avg_cost", "close", "value",
                    "pnl", "pnl_pct", "day_chg", "weight"]].copy()
        show["pnl_pct"] = show["pnl_pct"].map(lambda v: f"{v:+.2f}%")
        for c in ("avg_cost", "close"):
            show[c] = show[c].map(lambda v: f"{v:,.2f}")
        for c in ("value", "pnl", "day_chg"):
            show[c] = show[c].map(lambda v: f"{v:,.0f}")
        show["weight"] = show["weight"].map(lambda v: f"{v:.1f}%")
        st.dataframe(show, width="stretch", hide_index=True)

        del_col, csv_col, _ = st.columns([1, 1, 3])
        with del_col:
            d_sym = st.selectbox("Remove", val["symbol"].tolist())
            if st.button("🗑 Remove"):
                db.delete_holding(d_sym)
                st.rerun()
        with csv_col:
            st.download_button("⬇️ Export", val.to_csv(index=False).encode(),
                               "portfolio.csv", mime="text/csv")

        c1, c2 = st.columns(2)
        with c1:
            sec = P.sector_allocation()
            if not sec.empty:
                fig = px.pie(sec, values="value", names="sector", hole=0.45,
                             title="Sector allocation")
                fig.update_layout(height=330, margin=dict(t=40, b=10))
                st.plotly_chart(fig, width="stretch")
        with c2:
            fig = px.bar(val.sort_values("weight"), x="weight", y="symbol",
                         orientation="h", title="Weight per holding (% of value)",
                         labels={"weight": "%", "symbol": ""})
            fig.update_layout(height=330, margin=dict(t=40, b=10))
            st.plotly_chart(fig, width="stretch")

        hist, _bench = P.series()
        if not hist.empty and len(hist) > 5:
            fig = go.Figure()
            fig.add_trace(go.Scatter(x=hist.index, y=hist["portfolio"],
                                     name="Portfolio", line=dict(width=2, color="#3a49b8")))
            if "nifty" in hist.columns:
                fig.add_trace(go.Scatter(x=hist.index, y=hist["nifty"],
                                         name="NIFTY 50 (scaled)",
                                         line=dict(width=1.5, color="#999", dash="dot")))
            fig.update_layout(height=340, template="plotly_white",
                              title="Portfolio vs NIFTY 50", margin=dict(t=40, b=10))
            st.plotly_chart(fig, width="stretch")

        h = P.health()
        if h:
            st.caption(
                f"Concentration: **{h['diversification']}** "
                f"(top-3 = {h['top3_weight']:.0f}% of portfolio, "
                f"HHI {h['concentration_hhi']:.2f})"
            )

# ================================================================ stock
with tab_stock:
    st.subheader("📈 Single stock")
    univ = db.load_universe()
    if univ.empty:
        st.info("Universe not loaded yet — run the update first.")
    else:
        opts = (univ["symbol"] + " — " + univ["name"]).tolist()
        pick = st.selectbox("Search", opts)
        sym = pick.split(" — ")[0]
        c1, c2 = st.columns([3, 1])
        with c2:
            if st.button("📄 Save HTML report"):
                p = R.save_stock_report(sym)
                st.success(f"Saved {p.name}")
                st.download_button("⬇️ Download", p.read_text(), f"{sym}.html",
                                   mime="text/html")
        try:
            h = db.load_history(sym, start=(pd.Timestamp.today()
                                             - pd.Timedelta(days=400)).strftime("%Y-%m-%d"))
            if h.empty:
                st.info(f"No cached prices for {sym}. Hit **Refresh prices** in the sidebar.")
            else:
                last, prev = h["close"].iloc[-1], h["close"].iloc[-2]
                chg = (last / prev - 1) * 100
                k = st.columns(5)
                k[0].metric("Close", f"₹{last:,.2f}", f"{chg:+.2f}%")
                k[1].metric("52w high", f"₹{h['close'].max():,.2f}")
                k[2].metric("52w low", f"₹{h['close'].min():,.2f}")
                rsi_v = M.rsi(h["close"]).iloc[-1]
                k[3].metric("RSI (14)", f"{rsi_v:.0f}")
                s50 = M.sma(h["close"], 50).iloc[-1]
                k[4].metric("vs 50 DMA", f"{(last / s50 - 1) * 100:+.1f}%")

                fig = go.Figure(go.Candlestick(
                    x=h.index, open=h["open"], high=h["high"], low=h["low"],
                    close=h["close"], name=sym))
                fig.add_trace(go.Scatter(x=h.index, y=h["close"].rolling(50).mean(),
                                         name="50 DMA", line=dict(color="#f5a623")))
                fig.add_trace(go.Scatter(x=h.index, y=h["close"].rolling(200).mean(),
                                         name="200 DMA", line=dict(color="#3a49b8")))
                fig.update_layout(height=400, template="plotly_white",
                                  xaxis_rangeslider_visible=False,
                                  margin=dict(l=10, r=10, t=30, b=10))
                st.plotly_chart(fig, width="stretch")

                sc_row = _scores_latest()
                sc_row = sc_row[sc_row["symbol"] == sym]
                if not sc_row.empty:
                    r = sc_row.iloc[0]
                    bars = pd.DataFrame(
                        {"Component": ["DVS", "Value", "Quality", "Momentum",
                                       "Growth", "Dividend"],
                         "Score": [r["dvs"], r["value"], r["quality"], r["momentum"],
                                   r["growth"], r["dividend"]]})
                    figb = px.bar(bars, x="Score", y="Component", orientation="h",
                                  color="Score", color_continuous_scale="RdYlGn",
                                  range_color=[0, 100], title=f"DVS rank #{int(r['rank'])}")
                    figb.update_layout(height=260, margin=dict(t=40, b=10),
                                       coloraxis_showscale=False)
                    st.plotly_chart(figb, width="stretch")

                from marketdesk import fundamentals as F
                met = F.metrics_frame()
                row = met[met["symbol"] == sym] if not met.empty else pd.DataFrame()
                if not row.empty:
                    r = row.iloc[0]
                    mcols = st.columns(4)
                    pairs = [
                        ("P/E", r.get("pe"), "{:.1f}"), ("P/B", r.get("pb"), "{:.2f}"),
                        ("ROE", r.get("roe"), "{:.1f}%"),
                        ("Debt/Equity", r.get("debt_equity"), "{:.0f}%"),
                        ("Op margin", r.get("op_margin"), "{:.1f}%"),
                        ("Div yield", r.get("div_yield"), "{:.2f}%"),
                        ("EV/EBITDA", r.get("ev_ebitda"), "{:.1f}"),
                        ("Market cap", r.get("market_cap"), None),
                        ("Analyst target", r.get("analyst_target"), "{:,.0f}"),
                        ("Target upside", r.get("target_upside"), "{:+.1f}%"),
                        ("Earnings growth", r.get("earnings_growth"), "{:+.1f}%"),
                        ("Next earnings", r.get("next_earnings"), "{}"),
                        ("Recommendation", r.get("recommendation"), "{}"),
                    ]
                    for i, (lab, v, fmt) in enumerate(pairs):
                        if v is None:
                            continue
                        if fmt is None:
                            txt = _fmt_cr(v)
                        else:
                            try:
                                txt = fmt.format(float(v) / 1e7
                                                 if lab == "Market cap" else v)
                            except (TypeError, ValueError):
                                txt = str(v)
                        with mcols[i % 4]:
                            st.metric(lab, txt)
                else:
                    st.caption("No fundamentals cached — run `jobs update`.")
        except Exception as e:  # noqa: BLE001
            st.error(f"{e}")

# ================================================================ reports
with tab_reports:
    st.subheader("📰 Reports")
    c1, c2 = st.columns([1, 3])
    with c1:
        if st.button("📝 Generate daily report", type="primary"):
            p = R.save_daily_report()
            st.success(f"Wrote {p.name}")
        sym_r = st.text_input("Stock report symbol", placeholder="RELIANCE.NS")
        if sym_r and st.button("📄 Stock report"):
            p = R.save_stock_report(sym_r)
            st.success(f"Wrote {p.name}")
    files = R.list_reports()
    if files:
        names = [f.name for f in files]
        pick = st.selectbox("Saved reports", names)
        path = ROOT / "reports" / pick
        with open(path, encoding="utf-8") as fh:
            html = fh.read()
        st.iframe(html, height=700)
        st.download_button("⬇️ Download HTML", html, pick, mime="text/html")
    else:
        st.info("No reports yet — generate one above.")

# ================================================================ alerts
with tab_alerts:
    st.subheader("🔔 Alerts")
    c1, c2 = st.columns([1, 1])
    with c1:
        st.caption(f"Telegram {'✅ configured' if telegram_ready() else '❌ not configured'}")
    with c2:
        st.caption(f"Email {'✅ configured' if email_ready() else '❌ not configured'}")
    if st.button("🧪 Send test message"):
        res = A.test_channels()
        if any(res.values()):
            st.success(f"Sent: {res}")
        else:
            st.warning("No channel configured — fill TELEGRAM_*/SMTP_* in .env "
                       "(see ⚙️ Settings).")

    st.markdown("**Create rule**")
    r1, r2, r3 = st.columns([2, 2, 2])
    rtype = st.selectbox("Type", list(A.RULE_TYPES),
                         format_func=lambda k: A.RULE_TYPES[k]["label"])
    params = dict(A.RULE_TYPES[rtype]["params"])
    symbol = ""
    if rtype in ("price_above", "price_below", "pct_move", "rsi", "dvs",
                 "earnings_within"):
        univ = db.load_universe()
        opts = univ["symbol"].tolist() if not univ.empty else []
        symbol = r1.selectbox("Symbol", opts) if opts else r1.text_input("Symbol")
    if rtype == "price_above" or rtype == "price_below":
        params["price"] = r2.number_input("Price (₹)", 1.0, 1e5, 100.0)
    elif rtype == "pct_move":
        params["pct"] = r2.number_input("Move ± (%)", 1.0, 50.0, 5.0)
    elif rtype == "rsi":
        params["op"] = r2.selectbox("Condition", ["below", "above"])
        params["value"] = r3.number_input("RSI level", 0, 100, 30)
    elif rtype == "dvs":
        params["min"] = r2.number_input("DVS ≥", 0, 100, 80)
    elif rtype == "earnings_within":
        params["days"] = r2.number_input("Within (days)", 1, 30, 7)

    if st.button("➕ Add rule"):
        A.add_rule(rtype, symbol, params)
        st.success("Rule added")
        st.rerun()

    st.markdown("**Active rules**")
    rules = A.list_rules()
    if rules.empty:
        st.info("No rules yet.")
    else:
        for _, r in rules.iterrows():
            c1, c2, c3, c4, c5 = st.columns([4, 3, 2, 1, 1])
            c1.write(f"**{r['label']}**" + (f" — {r['symbol']}" if r["symbol"] else ""))
            c2.write(json.dumps(r["params"]))
            c3.write(f"last: {r['last_triggered'] or 'never'}")
            if c4.toggle("On", value=bool(r["enabled"]), key=f"tg{r['id']}"):
                if not r["enabled"]:
                    A.toggle_rule(r["id"], True)
            else:
                if r["enabled"]:
                    A.toggle_rule(r["id"], False)
            if c5.button("🗑", key=f"del{r['id']}"):
                A.delete_rule(r["id"])
                st.rerun()

    st.markdown("**Recent log**")
    log = db.frame("SELECT ts, rule, symbol, message FROM alert_log "
                   "ORDER BY id DESC LIMIT 20")
    if log.empty:
        st.caption("Nothing fired yet.")
    else:
        st.dataframe(log, width="stretch", hide_index=True)

# ================================================================ settings
with tab_settings:
    st.subheader("⚙️ Settings")

    st.markdown("### Notification channels")
    with st.form("env_form"):
        f1, f2 = st.columns(2)
        tg_token = f1.text_input("Telegram bot token", value=env("TELEGRAM_BOT_TOKEN"),
                                 type="password")
        tg_chat = f1.text_input("Telegram chat id", value=env("TELEGRAM_CHAT_ID"))
        smtp_host = f2.text_input("SMTP host", value=env("SMTP_HOST", "smtp.gmail.com"))
        smtp_port = f2.text_input("SMTP port", value=env("SMTP_PORT", "587"))
        f3, f4 = st.columns(2)
        smtp_user = f3.text_input("SMTP user (gmail)", value=env("SMTP_USER"))
        smtp_pass = f3.text_input("SMTP app password", value=env("SMTP_PASSWORD"),
                                  type="password")
        email_to = f4.text_input("Send mail to", value=env("EMAIL_TO"))
        if st.form_submit_button("💾 Save to .env"):
            _write_env({
                "TELEGRAM_BOT_TOKEN": tg_token.strip(),
                "TELEGRAM_CHAT_ID": tg_chat.strip(),
                "SMTP_HOST": smtp_host.strip(),
                "SMTP_PORT": smtp_port.strip(),
                "SMTP_USER": smtp_user.strip(),
                "SMTP_PASSWORD": smtp_pass.strip(),
                "EMAIL_TO": email_to.strip(),
            })
            st.success("Saved. Restart the app if channels don't activate.")
            st.rerun()

    st.markdown("### Broker API keys (optional)")
    with st.form("broker_form"):
        b1, b2 = st.columns(2)
        dhan_id = b1.text_input("Dhan client id", value=env("DHAN_CLIENT_ID"))
        dhan_tok = b1.text_input("Dhan access token", value=env("DHAN_ACCESS_TOKEN"),
                                 type="password")
        angel_key = b2.text_input("Angel API key", value=env("ANGEL_API_KEY"))
        angel_id = b2.text_input("Angel client id", value=env("ANGEL_CLIENT_ID"))
        if st.form_submit_button("💾 Save broker keys"):
            _write_env({
                "DHAN_CLIENT_ID": dhan_id.strip(),
                "DHAN_ACCESS_TOKEN": dhan_tok.strip(),
                "ANGEL_API_KEY": angel_key.strip(),
                "ANGEL_CLIENT_ID": angel_id.strip(),
            })
            st.success("Saved")

    st.markdown("### Data status")
    s1, s2, s3, s4 = st.columns(4)
    s1.metric("Price rows", f"{db.rows('SELECT COUNT(*) c FROM prices')[0]['c']:,}")
    s2.metric("Symbols priced",
              f"{db.rows('SELECT COUNT(DISTINCT symbol) c FROM prices')[0]['c']}")
    s3.metric("Fundamentals",
              f"{db.rows('SELECT COUNT(*) c FROM fundamentals')[0]['c']}")
    s4.metric("Scored today",
              f"{db.rows('SELECT COUNT(*) c FROM scores WHERE date=?', (date.today().isoformat(),))[0]['c']}")

    st.markdown("### Run pipeline")
    st.caption("`python -m marketdesk.jobs daily` does update → scores → "
               "alerts → report. Schedule it with cron/launchd for daily automation.")
    if st.button("▶️ Run daily pipeline now", type="primary"):
        import subprocess
        with st.spinner("Running (can take minutes for Nifty 500)..."):
            proc = subprocess.run(
                ["python3", "-m", "marketdesk.jobs", "daily", "--limit", "50"],
                cwd=ROOT, capture_output=True, text=True, timeout=1800,
            )
        if proc.returncode == 0:
            st.code(proc.stdout[-3000:])
            st.success("Pipeline finished")
            st.cache_data.clear()
        else:
            st.code((proc.stdout + proc.stderr)[-3000:])
            st.error("Pipeline failed")

    st.markdown("### Config")
    st.json(CFG)
    st.caption(f"Project root: {ROOT}")
