"""DhanHQ API v2 client (free API access with a Dhan account).

Setup: https://dhan.co -> Developer Console -> create app -> copy
Client ID + Access Token into .env.  NOTE: untested until credentials are
added; endpoints follow Dhan's public v2 docs.
"""
from __future__ import annotations

import requests

from ..config import env
from .base import Broker, Holding, NotConfigured

BASE = "https://api.dhan.co/v2"


class DhanBroker(Broker):
    name = "dhan"

    def is_configured(self) -> bool:
        return bool(env("DHAN_CLIENT_ID") and env("DHAN_ACCESS_TOKEN"))

    def _headers(self) -> dict:
        self.require()
        return {
            "access-token": env("DHAN_ACCESS_TOKEN"),
            "Client-Id": env("DHAN_CLIENT_ID"),
            "Content-Type": "application/json",
        }

    def get_holdings(self) -> list[Holding]:
        r = requests.get(f"{BASE}/holdings", headers=self._headers(), timeout=20)
        if r.status_code != 200:
            raise NotConfigured(
                f"Dhan /holdings returned {r.status_code}: {r.text[:200]}. "
                "Check DHAN_CLIENT_ID / DHAN_ACCESS_TOKEN in .env."
            )
        data = r.json() or []
        out = []
        for h in data:
            qty = float(h.get("quantity") or 0)
            if qty <= 0:
                continue
            out.append(Holding(
                symbol=str(h.get("tradingSymbol") or h.get("dhanSymbol") or "").upper(),
                qty=qty,
                avg_cost=float(h.get("averagePrice") or 0),
                ltp=float(h.get("lastPrice")) if h.get("lastPrice") else None,
            ))
        return out
