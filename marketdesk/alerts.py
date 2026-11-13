"""Alert rules: evaluate daily, deliver via Telegram + email."""
from __future__ import annotations

import json
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pandas as pd
import requests

from . import db
from .config import CFG
from .config import email_ready, env, telegram_ready

# ---------------------------------------------------------------- channels
def send_telegram(text: str) -> bool:
    if not telegram_ready():
        return False
    url = f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage"
    try:
        r = requests.post(
            url,
            json={"chat_id": env("TELEGRAM_CHAT_ID"), "text": text,
                  "parse_mode": "HTML", "disable_web_page_preview": True},
            timeout=15,
        )
        return r.ok
    except requests.RequestException:
        return False


def send_email(subject: str, html: str) -> bool:
    if not email_ready():
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = env("SMTP_USER")
        msg["To"] = env("EMAIL_TO")
        msg.attach(MIMEText(html, "html"))
        host, port = env("SMTP_HOST", "smtp.gmail.com"), int(env("SMTP_PORT", "587"))
        with smtplib.SMTP(host, port, timeout=20) as s:
            s.starttls()
            s.login(env("SMTP_USER"), env("SMTP_PASSWORD"))
            s.sendmail(env("SMTP_USER"), [env("EMAIL_TO")], msg.as_string())
        return True
    except Exception:  # noqa: BLE001
        return False


def send_alert(text: str, subject: str = "MarketDesk alert") -> dict:
    return {"telegram": send_telegram(text), "email": send_email(subject, f"<p>{text}</p>")}


def test_channels() -> dict:
    msg = f"✅ MarketDesk test message — {datetime.now():%d %b %Y %H:%M}"
    return send_alert(msg, subject="MarketDesk test")


# ---------------------------------------------------------------- rules
RULE_TYPES = {
    "price_above":  {"label": "Price crosses above", "params": {"price": 100}},
    "price_below":  {"label": "Price crosses below", "params": {"price": 100}},
    "pct_move":     {"label": "Daily move beyond ±%", "params": {"pct": 5}},
    "rsi":          {"label": "RSI beyond level", "params": {"op": "below", "value": 30}},
    "dvs":          {"label": "DVS score ≥", "params": {"min": 80}},
    "earnings_within": {"label": "Earnings within N days", "params": {"days": 7}},
}


def add_rule(type_: str, symbol: str = "", params: dict | None = None) -> None:
    if type_ not in RULE_TYPES:
        raise ValueError(f"unknown rule type {type_}")
    db.run(
        "INSERT INTO alert_rules(type,symbol,params,created_at) VALUES (?,?,?,?)",
        (type_, symbol.upper().strip(),
         json.dumps(params or RULE_TYPES[type_]["params"]),
         datetime.now().isoformat(timespec="seconds")),
    )


def list_rules() -> pd.DataFrame:
    df = db.frame(
        "SELECT id,type,symbol,params,enabled,last_triggered,created_at "
        "FROM alert_rules ORDER BY id DESC"
    )
    if not df.empty:
        df["label"] = df["type"].map(lambda t: RULE_TYPES.get(t, {}).get("label", t))
        df["params"] = df["params"].map(json.loads)
    return df


def delete_rule(rule_id: int) -> None:
    db.run("DELETE FROM alert_rules WHERE id=?", (rule_id,))


def toggle_rule(rule_id: int, enabled: bool) -> None:
    db.run("UPDATE alert_rules SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id))


# ---------------------------------------------------------------- evaluation
def _cooldown_ok(last: str | None) -> bool:
    if not last:
        return True
    hours = CFG["alerts"]["cooldown_hours"]
    try:
        return datetime.fromisoformat(last) < datetime.now() - timedelta(hours=hours)
    except ValueError:
        return True


def evaluate() -> list[dict]:
    """Run all enabled rules; send + log anything that fires."""
    from . import market as M, scores as S

    rules = db.rows("SELECT * FROM alert_rules WHERE enabled=1")
    if not rules:
        return []

    lc = db.latest_closes().set_index("symbol")
    scores = db.latest_scores().set_index("symbol")
    fired: list[dict] = []

    for rule in rules:
        params = json.loads(rule["params"] or "{}")
        sym = (rule["symbol"] or "").upper()
        text = None

        if rule["type"] in ("price_above", "price_below", "pct_move"):
            row = lc.loc[sym] if sym in lc.index else None
            if row is None or pd.isna(row.get("prev_close")):
                continue
            px, prev = float(row["close"]), float(row["prev_close"])
            if rule["type"] == "price_above" and px >= float(params["price"]):
                text = f"📈 <b>{sym}</b> crossed above ₹{params['price']} (now ₹{px:,.2f})"
            elif rule["type"] == "price_below" and px <= float(params["price"]):
                text = f"📉 <b>{sym}</b> crossed below ₹{params['price']} (now ₹{px:,.2f})"
            elif rule["type"] == "pct_move" and abs(px / prev - 1) * 100 >= float(params["pct"]):
                chg = (px / prev - 1) * 100
                arrow = "📈" if chg > 0 else "📉"
                text = f"{arrow} <b>{sym}</b> {chg:+.2f}% today (₹{px:,.2f})"

        elif rule["type"] == "rsi":
            closes = db.load_closes(symbols=[sym])
            if closes.empty or sym not in closes:
                continue
            v = float(M.rsi(closes[sym].dropna()).iloc[-1])
            op, val = params.get("op", "below"), float(params["value"])
            hit = v < val if op == "below" else v > val
            if hit:
                text = f"🔔 <b>{sym}</b> RSI({v:.0f}) {op} {val:g}"

        elif rule["type"] == "dvs":
            if sym in scores.index and float(scores.loc[sym, "dvs"]) >= float(params["min"]):
                text = (f"⭐ <b>{sym}</b> DVS "
                        f"{float(scores.loc[sym,'dvs']):.0f} ≥ {params['min']:g}")

        elif rule["type"] == "earnings_within":
            from . import fundamentals as F
            met = F.metrics_frame()
            row = met[met["symbol"] == sym] if not met.empty else pd.DataFrame()
            if row.empty or row.iloc[0].get("next_earnings") is None:
                continue
            try:
                d = pd.Timestamp(row.iloc[0]["next_earnings"])
                days = (d - pd.Timestamp.now()).days
            except (ValueError, TypeError):
                continue
            if 0 <= days <= int(params["days"]):
                text = f"🗓️ <b>{sym}</b> earnings in {days} day(s) — {d.date()}"

        if text and _cooldown_ok(rule["last_triggered"]):
            channels = send_alert(text, subject=f"MarketDesk: {sym or 'market'}")
            if any(channels.values()):
                db.run(
                    "UPDATE alert_rules SET last_triggered=? WHERE id=?",
                    (datetime.now().isoformat(timespec="seconds"), rule["id"]),
                )
            db.run(
                "INSERT INTO alert_log(ts,rule,symbol,message,channels) VALUES (?,?,?,?,?)",
                (datetime.now().isoformat(timespec="seconds"), rule["type"], sym, text,
                 json.dumps(channels)),
            )
            fired.append({"rule": rule["type"], "symbol": sym, "message": text,
                          "channels": channels})
    return fired
