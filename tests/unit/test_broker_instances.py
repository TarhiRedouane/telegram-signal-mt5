"""Broker instances: profiles, menu selection, per-instance folders, and guards that keep the bot
off the wrong MT5 account (e.g. the manual live terminal)."""

import json
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import config.brokers as brokers
from config.brokers import get_broker, parse_selection, password_var
from src.risk_manager.risk_guard import account_mode_name, refusal

pytestmark = pytest.mark.unit

PROFILES = {
    "raise": {"label": "Raise demo", "mt5_path": "C:\\MT5_Raise\\terminal64.exe", "portable": True,
              "mode": "demo", "login": 111, "server": "Raise-Demo"},
    "vt": {"label": "VT demo", "mt5_path": "C:\\MT5_VT\\terminal64.exe", "portable": True,
           "mode": "demo", "login": 222, "server": "VT-Demo"},
    "fx": {"label": "FX live", "mt5_path": "C:\\MT5_FX\\terminal64.exe", "mode": "real",
           "login": 333, "server": "FX-Live"},
}


def test_menu_selection():
    keys = list(PROFILES)
    assert parse_selection("1", keys) == ["raise"]
    assert parse_selection("1,3", keys) == ["raise", "fx"]
    assert parse_selection(" 2 1 2 ", keys) == ["vt", "raise"]
    assert parse_selection("all", keys) == keys
    assert parse_selection("vt", keys) == ["vt"]
    for junk in ("", "4", "0", "abc"):
        with pytest.raises(ValueError):
            parse_selection(junk, keys)


def test_profile_validation_and_password_var():
    assert get_broker("raise", PROFILES)["login"] == 111
    assert password_var("vt-markets") == "BROKER_VT_MARKETS_PASSWORD"
    with pytest.raises(ValueError, match="Unknown broker"):
        get_broker("nope", PROFILES)
    with pytest.raises(ValueError, match="missing server"):
        get_broker("x", {"x": {**PROFILES["raise"], "server": ""}})
    with pytest.raises(ValueError, match="mode must be"):
        get_broker("x", {"x": {**PROFILES["raise"], "mode": "live"}})


def test_instance_settings_use_profile_and_own_folders(tmp_path, monkeypatch):
    from config.settings import Settings
    from src.database import get_database_url

    path = tmp_path / "brokers.json"
    path.write_text(json.dumps(PROFILES), encoding="utf-8")
    monkeypatch.setattr(brokers, "BROKERS_FILE", path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BOT_INSTANCE", "raise")
    monkeypatch.setenv("BROKER_RAISE_PASSWORD", "secret")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///data/signal_ea.db")  # shared one is ignored

    s = Settings(env_file=str(tmp_path / "none.env"))
    assert (s.mt5_login, s.mt5_server, s.mt5_path) == ("111", "Raise-Demo", "C:\\MT5_Raise\\terminal64.exe")
    assert s.mt5_portable and s.mt5_account_mode == "demo" and s.mt5_password == "secret"
    assert str(s.data_dir).replace("\\", "/") == "data/raise" and str(s.log_dir).replace("\\", "/") == "logs/raise"
    assert str(s.session_file).replace("\\", "/") == "data/raise/telegram.session"
    assert get_database_url().replace("\\", "/").endswith("data/raise/signal_ea.db")
    mt5_settings = s.get_mt5_settings()
    assert mt5_settings.portable and mt5_settings.account_mode == "demo"

    monkeypatch.delenv("BROKER_RAISE_PASSWORD")
    with pytest.raises(ValueError, match="BROKER_RAISE_PASSWORD"):
        Settings(env_file=str(tmp_path / "none.env"))


def test_no_instance_keeps_todays_layout(tmp_path, monkeypatch):
    from config.settings import Settings
    from src.database import get_database_url

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("BOT_INSTANCE", raising=False)
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///data/signal_ea.db")
    s = Settings(env_file=str(tmp_path / "none.env"))
    assert str(s.data_dir) == "data" and str(s.log_dir) == "logs"
    assert get_database_url() == "sqlite+aiosqlite:///data/signal_ea.db"


def _fake_mt5(account):
    mt5 = Mock()
    mt5.initialize = Mock(return_value=True)
    mt5.login = Mock(return_value=True)
    mt5.shutdown = Mock()
    mt5.last_error = Mock(return_value=(0, "ok"))
    mt5.account_info = Mock(return_value=account)
    return mt5


@pytest.mark.asyncio
async def test_connect_never_switches_a_terminal_on_another_account(monkeypatch):
    import src.mt5_executor.connection as conn

    live = SimpleNamespace(login=999, server="Broker-Live", trade_mode=2)  # the manual live terminal
    fake = _fake_mt5(live)
    monkeypatch.setattr(conn, "mt5", fake)
    ok = await conn.MT5Connection(1).connect(111, "pw", "Raise-Demo", "C:\\MT5\\t.exe", True, "demo")
    assert ok is False
    fake.login.assert_not_called()
    fake.initialize.assert_called_once_with("C:\\MT5\\t.exe", portable=True)


@pytest.mark.asyncio
async def test_connect_refuses_real_account_in_demo_mode_and_accepts_match(monkeypatch):
    import src.mt5_executor.connection as conn

    fake = _fake_mt5(SimpleNamespace(login=111, server="Raise-Demo", trade_mode=2))
    monkeypatch.setattr(conn, "mt5", fake)
    assert await conn.MT5Connection(1).connect(111, "pw", "Raise-Demo", "p", False, "demo") is False
    fake.shutdown.assert_called_once()

    fake.account_info.return_value = SimpleNamespace(login=111, server="Raise-Demo", trade_mode=0)
    assert await conn.MT5Connection(2).connect(111, "pw", "Raise-Demo", "p", False, "demo") is True
    assert await conn.MT5Connection(3).connect(111, "pw", "Raise-Demo", "p", False, "real") is False


def test_risk_guard_checks_account_on_every_order(tmp_path):
    settings = SimpleNamespace(
        trading_enabled=True, kill_switch_file=tmp_path / "STOP", risk_percent=Decimal("3"),
        max_open_risk_percent=Decimal("6"), max_open_legs=6, daily_loss_limit_percent=Decimal("6"),
        mt5_account_mode="demo", mt5_login="111",
    )
    snap = {"balance": Decimal("10000"), "equity": Decimal("10000"), "open_legs": 0, "open_risk": Decimal(0),
            "unprotected": 0, "realized_today": Decimal(0), "login": 111, "account_mode": "demo"}
    assert refusal(snap, Decimal("100"), settings) is None
    assert "REAL account" in refusal({**snap, "account_mode": "real"}, Decimal("100"), settings)
    assert "account 999" in refusal({**snap, "login": 999}, Decimal("100"), settings)
    assert account_mode_name(0) == "demo" and account_mode_name(2) == "real" and account_mode_name(None) is None
