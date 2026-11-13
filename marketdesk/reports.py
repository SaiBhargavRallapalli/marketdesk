"""HTML report generation: daily market report + single-stock one-pager."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from . import db
from .config import REPORTS

CSS = """
<style>
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;color:#1a1a2e;margin:0;padding:24px;background:#fafafa}
h1{font-size:22px;margin:0 0 4px} h2{font-size:16px;margin:26px 0 8px;border-bottom:2px solid #eee;padding-bottom:4px}
.sub{color:#666;font-size:12px;margin-bottom:18px}
table{border-collapse:collapse;width:100%;font-size:13px;background:#fff}
th{background:#1a1a2e;color:#fff;text-align:left;padding:7px 9px;font-weight:600}
td{padding:6px 9px;border-bottom:1px solid #f0f0f0}
tr:hover td{background:#f7f9ff}
.pos{color:#0a8a3e;font-weight:600} .neg{color:#d0342c;font-weight:600}
.cards{display:flex;gap:12px;flex-wrap:wrap}
.card{background:#fff;border:1px solid #eee;border-radius:10px;padding:12px 16px;min-width:150px}
.card .k{font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.5px}
.card .v{font-size:20px;font-weight:700;margin-top:3px}
.grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:800px){.grid2{grid-template-columns:1fr}}
.tag{display:inline-block;background:#eef1ff;color:#3a49b8;border-radius:20px;padding:2px 10px;font-size:11px;font-weight:600}
</style>
"""


def _df_table(df: pd.DataFrame, fmt: dict | None = None, max_rows: int = 15) -> str:
    if df is None or df.empty:
        return "<p class='sub'>No data.</p>"
    d = df.head(max_rows).copy()
    for col, f in (fmt or {}).items():
        if col in d.columns:
            d[col] = d[col].map(f)
    return d.to_html(index=False, border=0, escape=False)


def _pct(v) -> str:
    try:
        v = float(v)
        cls = "pos" if v >= 0 else "neg"
        return f"<span class='{cls}'>{v:+.2f}%</span>"
    except (TypeError, ValueError):
        return "-"


def _inr(v) -> str:
    try:
        return f"₹{float(v):,.0f}"
    except (TypeError, ValueError):
        return "-"


def daily_report(as_of: str | None = None) -> str:
    from . import market as M, portfolio as P, scores as S

    as_of = as_of or date.today().isoformat()
    idx = M.index_snapshot()
    br = M.breadth()
    gainers, losers = M.movers()
    top_scores = S.latest().head(10) if not db.latest_scores().empty else pd.DataFrame()
    pf = P.summary()
    alerts = db.rows(
        "SELECT ts, symbol, message FROM alert_log ORDER BY id DESC LIMIT 8"
    )

    breadth_bar = ""
    if br.get("advances") is not None and (br["advances"] + br["declines"]):
        tot = br["advances"] + br["declines"] + br["unchanged"]
        breadth_bar = (
            f"<div class='card'><div class='k'>Market breadth ({br.get('date','')})</div>"
            f"<div class='v'>{br['advances']}A / {br['declines']}D / {br['unchanged']}U</div>"
            f"<div style='background:#eee;height:8px;border-radius:4px;margin-top:6px;overflow:hidden'>"
            f"<div style='background:#0a8a3e;height:8px;width:{100*br['advances']/max(tot,1):.0f}%'></div>"
            f"</div></div>"
        )

    cards = "".join(
        f"<div class='card'><div class='k'>{r['index']}</div>"
        f"<div class='v'>{r['value']:,.0f}</div>{_pct(r['change_pct'])}</div>"
        for _, r in idx.iterrows()
    )
    cards += breadth_bar

    pf_card = ""
    if pf.get("holdings"):
        pf_card = (
            f"<div class='card'><div class='k'>Portfolio</div>"
            f"<div class='v'>{_inr(pf['value'])}</div>"
            f"<div>{_pct(pf['pnl_pct'])} overall &middot; today {_inr(pf['day_chg'])}</div></div>"
        )

    score_cols = [c for c in ["rank", "symbol", "name", "dvs", "value", "quality",
                              "momentum", "growth", "dividend", "pe", "roe"]
                  if c in top_scores.columns]

    alert_html = (
        _df_table(pd.DataFrame(alerts), max_rows=8) if alerts
        else "<p class='sub'>No alerts fired yet.</p>"
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">{CSS}</head><body>
<h1>MarketDesk Daily Report</h1>
<div class="sub">Generated {as_of} &middot; free self-hosted data &middot; not investment advice</div>
<div class="cards">{cards}{pf_card}</div>
<div class="grid2">
<div><h2>Top gainers</h2>{_df_table(gainers, {'close':'{:,.2f}'.format,'chg_pct':_pct})}</div>
<div><h2>Top losers</h2>{_df_table(losers, {'close':'{:,.2f}'.format,'chg_pct':_pct})}</div>
</div>
<h2>Top DVS scores (fundamental+momentum composite)</h2>
{_df_table(top_scores[score_cols] if not top_scores.empty else top_scores,
           {'dvs':'{:.0f}'.format,'value':'{:.0f}'.format,'quality':'{:.0f}'.format,
            'momentum':'{:.0f}'.format,'growth':'{:.0f}'.format,'dividend':'{:.0f}'.format,
            'pe':'{:.1f}'.format,'roe':'{:.1%}'.format})}
<h2>Recent alerts</h2>{alert_html}
<div class="sub" style="margin-top:24px">MarketDesk &middot; built on free data (Yahoo/NSE) &middot; DVS is a model score, not a recommendation.</div>
</body></html>"""


def stock_report(symbol: str) -> str:
    from . import market as M, scores as S

    sym = symbol.upper().strip()
    h = db.load_history(sym, start=(pd.Timestamp.today() - pd.Timedelta(days=400))
                        .strftime("%Y-%m-%d"))
    if h.empty:
        return f"<h1>No data for {sym}</h1><p>Run the daily update first.</p>"

    scores = db.latest_scores()
    score_row = scores[scores["symbol"] == sym] if not scores.empty else pd.DataFrame()
    from . import fundamentals as F
    met = F.metrics_frame()
    met_row = met[met["symbol"] == sym] if not met.empty else pd.DataFrame()

    # chart
    try:
        import plotly.graph_objects as go

        fig = go.Figure()
        fig.add_trace(go.Candlestick(
            x=h.index, open=h["open"], high=h["high"], low=h["low"], close=h["close"],
            name=sym))
        fig.add_trace(go.Scatter(x=h.index, y=M.sma(h["close"], 50), name="50 DMA",
                                 line=dict(width=1, color="#f5a623")))
        fig.add_trace(go.Scatter(x=h.index, y=M.sma(h["close"], 200), name="200 DMA",
                                 line=dict(width=1, color="#3a49b8")))
        fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10),
                          xaxis_rangeslider_visible=False, template="plotly_white")
        chart = fig.to_html(full_html=False, include_plotlyjs="cdn")
    except Exception:  # noqa: BLE001
        chart = "<p class='sub'>chart unavailable</p>"

    last = h.iloc[-1]
    prev = h.iloc[-2] if len(h) > 1 else last
    chg = (last["close"] / prev["close"] - 1) * 100

    stat_rows = ""
    if not met_row.empty:
        r = met_row.iloc[0]
        for label, key, fmt in [
            ("P/E", "pe", "{:.1f}"), ("P/B", "pb", "{:.2f}"), ("ROE", "roe", "{:.1f}%"),
            ("Debt/Equity", "debt_equity", "{:.0f}%"), ("Op margin", "op_margin", "{:.1f}%"),
            ("Div yield", "div_yield", "{:.2f}%"), ("EV/EBITDA", "ev_ebitda", "{:.1f}"),
            ("Mkt cap (₹cr)", "market_cap", "{:,.0f}"),
            ("Analyst target", "analyst_target", "{:,.0f}"),
            ("Target upside", "target_upside", "{:+.1f}%"),
            ("Earnings growth", "earnings_growth", "{:+.1f}%"),
            ("Next earnings", "next_earnings", "{}"),
            ("Sector", "sector", "{}"),
        ]:
            v = r.get(key) if hasattr(r, "get") else None
            if v is None:
                continue
            try:
                v = float(v) / 1e7 if key == "market_cap" else v
                txt = fmt.format(v)
            except (TypeError, ValueError):
                txt = str(v)
            stat_rows += f"<tr><td>{label}</td><td><b>{txt}</b></td></tr>"

    score_html = "<p class='sub'>No DVS score yet.</p>"
    if not score_row.empty:
        r = score_row.iloc[0]
        parts = []
        for k, c in [("dvs", "#3a49b8"), ("value", "#0a8a3e"), ("quality", "#0a8a3e"),
                     ("momentum", "#f5a623"), ("growth", "#9b59b6"), ("dividend", "#e67e22")]:
            if k in score_row.columns and pd.notna(r[k]):
                label = "DVS" if k == "dvs" else k.capitalize()
                v = float(r[k])
                parts.append(
                    f"<div style='margin:4px 0'><span style='display:inline-block;"
                    f"width:90px;font-size:12px'>{label}</span>"
                    f"<div style='background:#eee;border-radius:4px;height:14px;"
                    f"width:70%;display:inline-block;vertical-align:middle'>"
                    f"<div style='background:{c};height:14px;border-radius:4px;"
                    f"width:{max(0, min(100, v)):.0f}%'></div></div>"
                    f"<b style='font-size:12px'>{v:.0f}</b></div>"
                )
        rank_txt = (
            f"<div class='tag'>Rank #{int(r['rank'])}</div><br>"
            if "rank" in score_row.columns and pd.notna(r.get("rank")) else ""
        )
        score_html = rank_txt + ("".join(parts) or "<p class='sub'>no score</p>")

    tech_rows = ""
    rsi_v = M.rsi(h["close"]).iloc[-1]
    s50, s200 = M.sma(h["close"], 50).iloc[-1], M.sma(h["close"], 200).iloc[-1]
    for label, txt in [
        ("RSI (14)", f"{rsi_v:.0f}"),
        ("50 DMA", f"{s50:,.2f} ({'above' if last['close'] > s50 else 'below'})"),
        ("200 DMA", f"{s200:,.2f} ({'above' if last['close'] > s200 else 'below'})"),
        ("52w high", f"{h['close'].max():,.2f}"),
        ("52w low", f"{h['close'].min():,.2f}"),
    ]:
        tech_rows += f"<tr><td>{label}</td><td><b>{txt}</b></td></tr>"

    return f"""<!doctype html><html><head><meta charset="utf-8">{CSS}</head><body>
<h1>{sym}</h1>
<div class="sub">as of {last.name.date()} &middot; close {_inr(last['close'])} ({_pct(chg)})</div>
{chart}
<div class="grid2">
<div><h2>Valuation & fundamentals</h2><table>{stat_rows or '<tr><td>No fundamentals cached</td></tr>'}</table></div>
<div><h2>Technicals</h2><table>{tech_rows}</table></div>
</div>
<h2>DVS score breakdown</h2>{score_html}
<div class="sub" style="margin-top:20px">Not investment advice &middot; generated by MarketDesk</div>
</body></html>"""


def save_daily_report(as_of: str | None = None) -> Path:
    as_of = as_of or date.today().isoformat()
    path = REPORTS / f"{as_of}_daily.html"
    path.write_text(daily_report(as_of), encoding="utf-8")
    return path


def save_stock_report(symbol: str) -> Path:
    sym = symbol.upper().strip()
    path = REPORTS / f"{sym}_report.html"
    path.write_text(stock_report(sym), encoding="utf-8")
    return path


def list_reports() -> list[Path]:
    return sorted(REPORTS.glob("*.html"), reverse=True)
