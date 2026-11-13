"""Zerodha Kite Connect (PAID API, ~Rs 2000/mo) — optional adapter.

Only useful if you already subscribe to Kite Connect. Install:
    pip install kiteconnect
"""
from __future__ import annotations

from ..config import env
from .base import Broker, Holding, NotConfigured


class KiteBroker(Broker):
    name = "zerodha"

    def is_configured(self) -> bool:
        return bool(env("KITE_API_KEY") and env("KITE_ACCESS_TOKEN"))

    def get_holdings(self) -> list[Holding]:
        self.require()
        try:
            from kiteconnect import KiteConnect
        except ImportError as e:
            raise NotConfigured(
                "kiteconnect not installed. Run: pip install kiteconnect"
            ) from e
        kite = KiteConnect(api_key=env("KITE_API_KEY"))
        kite.set_access_token(env("KITE_ACCESS_TOKEN"))
        out = []
        for h in kite.holdings():
            qty = float(h.get("quantity") or 0)
            if qty <= 0:
                continue
            out.append(Holding(
                symbol=str(h.get("tradingsymbol") or "").upper(),
                qty=qty,
                avg_cost=float(h.get("average_price") or 0),
                ltp=float(h.get("ltp")) if h.get("ltp") else None,
            ))
        return out
