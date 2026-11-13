"""Default broker: holdings entered manually in the app (no keys needed)."""
from __future__ import annotations

from .. import db
from .base import Broker, Holding


class ManualBroker(Broker):
    name = "manual"

    def is_configured(self) -> bool:
        return True

    def get_holdings(self) -> list[Holding]:
        df = db.load_holdings()
        return [
            Holding(symbol=r.symbol, qty=float(r.qty), avg_cost=float(r.avg_cost))
            for r in df.itertuples()
        ]
