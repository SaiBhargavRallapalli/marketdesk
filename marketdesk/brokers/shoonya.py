"""Shoonya (Finvasia) client — free API.

Setup: https://shoonya.com -> API section -> enable API, copy credentials
into .env.  NOTE: Shoonya's NorenWClientTP endpoints occasionally change;
verify action names against their current docs before relying on this.
Untested until credentials are added.
"""
from __future__ import annotations

import requests

from ..config import env
from .base import Broker, Holding, NotConfigured

BASE = "https://api.shoonya.com/NorenWClientTP/"


class ShoonyaBroker(Broker):
    name = "shoonya"

    def is_configured(self) -> bool:
        return bool(env("SHOONYA_USER_ID") and env("SHOONYA_PASSWORD"))

    def _post(self, payload: dict) -> dict:
        r = requests.post(BASE, json=payload, timeout=20)
        r.raise_for_status()
        data = r.json()
        if str(data.get("stat")) != "Ok":
            raise NotConfigured(
                f"Shoonya error: {data.get('emsg', data)}. "
                "Check SHOONYA_* credentials in .env."
            )
        return data

    def _login(self) -> dict:
        self.require()
        return self._post({
            "t": "D", "uid": env("SHOONYA_USER_ID"),
            "pwd": env("SHOONYA_PASSWORD"), "factor": "",
        })

    def get_holdings(self) -> list[Holding]:
        sess = self._login()
        data = self._post({"jKey": sess.get("jKey"), "uid": env("SHOONYA_USER_ID"),
                           "actid": env("SHOONYA_USER_ID"), "o": "1", "s": "N"})
        out = []
        for h in (data.get("data") or []):
            qty = float(h.get("qty") or 0)
            if qty <= 0:
                continue
            out.append(Holding(
                symbol=str(h.get("tradsym") or h.get("tsym") or "").upper().split("-")[0],
                qty=qty,
                avg_cost=float(h.get("avgprc") or 0),
                ltp=float(h.get("lp")) if h.get("lp") else None,
            ))
        return out
