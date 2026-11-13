"""Broker registry — add new brokers here."""
from __future__ import annotations

from .angel import AngelBroker
from .base import Broker, Holding, NotConfigured
from .dhan import DhanBroker
from .manual import ManualBroker
from .shoonya import ShoonyaBroker
from .zerodha import KiteBroker

_BROKERS: dict[str, type[Broker]] = {
    "manual": ManualBroker,
    "dhan": DhanBroker,
    "angel": AngelBroker,
    "shoonya": ShoonyaBroker,
    "zerodha": KiteBroker,
}


def get_broker(name: str) -> Broker:
    cls = _BROKERS.get(name)
    if cls is None:
        raise ValueError(f"unknown broker '{name}'. Options: {list(_BROKERS)}")
    return cls()


def available() -> dict[str, bool]:
    """{name: configured?} for every registered broker."""
    out = {}
    for name, cls in _BROKERS.items():
        try:
            out[name] = cls().is_configured()
        except Exception:  # noqa: BLE001
            out[name] = False
    return out


def import_holdings(broker_name: str, overwrite: bool = True) -> int:
    """Fetch holdings from a broker and write them into the local portfolio."""
    from .. import db

    b = get_broker(broker_name)
    holdings = b.get_holdings()
    if overwrite:
        for h in db.load_holdings()["symbol"].tolist():
            db.delete_holding(h)
    n = 0
    for h in holdings:
        if h.qty > 0 and h.avg_cost > 0:
            db.add_holding(h.symbol, h.qty, h.avg_cost, note=f"imported:{broker_name}")
            n += 1
    return n


__all__ = ["Broker", "Holding", "NotConfigured", "get_broker", "available",
           "import_holdings"]
