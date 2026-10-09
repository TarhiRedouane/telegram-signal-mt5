"""Broker profiles: one MT5 terminal + account per entry in config/brokers.json.

Each running bot instance (BOT_INSTANCE=<key>) uses exactly one profile. The file holds no secrets:
the password comes from the BROKER_<KEY>_PASSWORD environment variable (.env).
"""

import json
import re
from pathlib import Path

BROKERS_FILE = Path(__file__).parent / "brokers.json"
_REQUIRED = ("mt5_path", "login", "server", "mode")


def load_brokers(path: Path | None = None) -> dict[str, dict]:
    """All configured profiles ({} when the file does not exist)."""
    path = path or BROKERS_FILE
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def password_var(key: str) -> str:
    """Environment variable holding the account password: raise -> BROKER_RAISE_PASSWORD."""
    return f"BROKER_{re.sub(r'[^A-Za-z0-9]', '_', key).upper()}_PASSWORD"


def get_broker(key: str, brokers: dict[str, dict] | None = None) -> dict:
    """Validated profile for `key`; raises ValueError with a readable reason."""
    brokers = load_brokers() if brokers is None else brokers
    if key not in brokers:
        raise ValueError(f"Unknown broker '{key}'. Configured: {', '.join(brokers) or 'none'} ({BROKERS_FILE})")
    profile = brokers[key]
    missing = [name for name in _REQUIRED if not profile.get(name)]
    if missing:
        raise ValueError(f"Broker '{key}' is missing {', '.join(missing)} in {BROKERS_FILE}")
    if profile["mode"] not in ("demo", "real"):
        raise ValueError(f"Broker '{key}': mode must be 'demo' or 'real', got '{profile['mode']}'")
    return profile


def parse_selection(text: str, keys: list[str]) -> list[str]:
    """Menu answer -> broker keys: '1,3', 'all', or names ('raise vt'). Raises ValueError on junk."""
    tokens = [t for t in re.split(r"[\s,]+", text.strip().lower()) if t]
    if not tokens:
        raise ValueError("Nothing selected")
    if tokens == ["all"]:
        return list(keys)
    chosen: list[str] = []
    for token in tokens:
        if token.isdigit() and 1 <= int(token) <= len(keys):
            key = keys[int(token) - 1]
        elif token in keys:
            key = token
        else:
            raise ValueError(f"Unknown choice '{token}'")
        if key not in chosen:
            chosen.append(key)
    return chosen
