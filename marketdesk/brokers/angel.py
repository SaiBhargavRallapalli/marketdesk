"""Angel One SmartAPI client (free with an Angel One account).

Setup: https://smartapi.angelone.in -> create app -> API key; login creds
in .env.  NOTE: untested until credentials are added; follows Angel's
public SmartAPI docs. TOTP-based auto-login can be added via
ANGEL_TOTP_SECRET if you enable it.
"""
from __future__ import annotations

import time

import requests

from ..config import env
from .base import Broker, Holding, NotConfigured

AUTH = "https://apiconnect.angelone.in/rest/auth/secret"
HOLDINGS = "https://apiconnect.angelone.in/rest/api/holdings"


class AngelBroker(Broker):
    name = "angel"

    def is_configured(self) -> bool:
        return bool(env("ANGEL_API_KEY") and env("ANGEL_CLIENT_ID") and env("ANGEL_PASSWORD"))

    def _login(self) -> dict:
        self.require()
        body = {
            "apiKey": env("ANGEL_API_KEY"),
            "clientId": env("ANGEL_CLIENT_ID"),
            "password": env("ANGEL_PASSWORD"),
        }
        # optional TOTP auto-login (requires a TOTP lib + secret); falls back
        # to an existing ACCESS TOKEN if you paste one into .env later.
        r = requests.post(AUTH, json=body, timeout=20)
        if r.status_code != 200:
            raise NotConfigured(
                f"Angel One login failed ({r.status_code}): {r.text[:200]}. "
                "Check ANGEL_* credentials in .env."
            )
        return r.json().get("data", {})

    def get_holdings(self) -> list[Holding]:
        tok = self._login()
        headers = {
            "Authorization": f"Bearer {tok.get('jwtToken')}",
            "X-PrivateKey": env("ANGEL_API_KEY"),
            "Accept": "application/json",
            "X-ClientId": env("ANGEL_CLIENT_ID"),
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-ClientPublicIP": "127.0.0.1",
            "X-ClientLocalIP": "127.0.0.1",
            "X-IPAddress": "127.0.0.1",
        }
        r = requests.get(HOLDINGS, headers=headers, timeout=20)
        if r.status_code != 200:
            raise NotConfigured(f"Angel One /holdings returned {r.status_code}: {r.text[:200]}")
        data = (r.json().get("data") or [])
        out = []
        for h in data:
            qty = float(h.get("quantity") or 0)
            if qty <= 0:
                continue
            out.append(Holding(
                symbol=str(h.get("tradingSymbol") or h.get("symbol") or "").upper()
                        .replace("-EQ", ""),
                qty=qty,
                avg_cost=float(h.get("averageprice") or 0),
                ltp=float(h.get("ltp")) if h.get("ltp") else None,
            ))
        return out


_ = time  # reserved for TOTP auto-login extension
