"""Broker adapter interface — pluggable, free-broker friendly."""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


class NotConfigured(RuntimeError):
    """Raised when a broker lacks credentials in .env."""


@dataclass
class Holding:
    symbol: str          # NSE symbol, e.g. RELIANCE
    qty: float
    avg_cost: float      # average buy price
    ltp: float | None = None

    def to_row(self) -> dict:
        return {"symbol": self.symbol, "qty": self.qty,
                "avg_cost": self.avg_cost, "ltp": self.ltp}


class Broker(ABC):
    name: str = "base"

    @abstractmethod
    def is_configured(self) -> bool: ...

    @abstractmethod
    def get_holdings(self) -> list[Holding]: ...

    def require(self) -> None:
        if not self.is_configured():
            raise NotConfigured(
                f"Broker '{self.name}' is not configured. "
                f"Add its credentials to .env (see .env.example)."
            )
