"""Paths, environment and YAML configuration."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

# project root = folder containing this package
ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
REPORTS = ROOT / "reports"
CONFIG_FILE = ROOT / "config.yaml"
ENV_FILE = ROOT / ".env"

DATA.mkdir(parents=True, exist_ok=True)
REPORTS.mkdir(parents=True, exist_ok=True)

load_dotenv(ENV_FILE)

_DEFAULTS = {
    "universe": "nifty500",
    "scoring": {
        "weights": {
            "value": 0.25,
            "quality": 0.25,
            "momentum": 0.30,
            "growth": 0.10,
            "dividend": 0.10,
        }
    },
    "backtest": {"initial_capital": 1_000_000, "cost_bps": 5},
    "alerts": {"cooldown_hours": 20},
    "cache": {"fundamentals_ttl_hours": 20},
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict:
    cfg = _DEFAULTS
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            user_cfg = yaml.safe_load(f) or {}
        cfg = _deep_merge(_DEFAULTS, user_cfg)
    return cfg


CFG = load_config()
DB_PATH = DATA / "marketdesk.db"


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_ready(key: str) -> bool:
    return bool(env(key))


def telegram_ready() -> bool:
    return env_ready("TELEGRAM_BOT_TOKEN") and env_ready("TELEGRAM_CHAT_ID")


def email_ready() -> bool:
    return all(env_ready(k) for k in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_TO"))
