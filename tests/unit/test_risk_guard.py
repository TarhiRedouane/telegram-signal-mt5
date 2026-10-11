"""Risk guard: every account-wide limit refuses the order beyond it; normal orders still pass."""

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.risk_manager.risk_guard import order_risk, refusal

pytestmark = pytest.mark.unit

D = Decimal


def _settings(tmp_path: Path, **overrides):
    values = {
        "trading_enabled": True,
        "kill_switch_file": tmp_path / "STOP_TRADING",
        "risk_percent": D("3"),
        "max_open_risk_percent": D("6"),
        "max_open_legs": 6,
        "daily_loss_limit_percent": D("6"),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot(**overrides):
    values = {
        "balance": D("10000"),
        "equity": D("10000"),
        "open_legs": 0,
        "open_risk": D(0),
        "unprotected": 0,
        "realized_today": D(0),
        "tick_size": D("0.01"),
        "tick_value": D("1"),
    }
    values.update(overrides)
    return values


def test_order_risk_gold_example():
    # 0.14 lot, SL 10 points, tick 0.01 = $1 per lot -> $140
    assert order_risk(D("10"), D("0.14"), D("0.01"), D("1")) == D("140")
    assert order_risk(None, D("0.14"), D("0.01"), D("1")) is None


def test_regular_order_under_limits_passes(tmp_path):
    assert refusal(_snapshot(), D("300"), _settings(tmp_path)) is None  # one 3 % signal


def test_each_limit_refuses(tmp_path):
    s = _settings(tmp_path)
    assert "TRADING_ENABLED" in refusal(
        _snapshot(), D("1"), _settings(tmp_path, trading_enabled=False)
    )
    assert "6 legs open" in refusal(_snapshot(open_legs=6), D("1"), s)
    assert "open risk would be 6.01%" in refusal(_snapshot(open_risk=D("300")), D("301"), s)
    # Closed -400 today + floating -200 = 6 % of the 10 000 start-of-day balance
    assert "daily loss 6.00%" in refusal(
        _snapshot(balance=D("9600"), equity=D("9400"), realized_today=D("-400")), D("1"), s
    )
    assert "unavailable" in refusal(None, D("1"), s)


def test_kill_switch_file_blocks_until_removed(tmp_path):
    s = _settings(tmp_path)
    s.kill_switch_file.touch()
    assert "kill switch" in refusal(_snapshot(), D("1"), s)
    s.kill_switch_file.unlink()
    assert refusal(_snapshot(), D("1"), s) is None


def test_positions_and_order_without_sl_count_as_a_standard_trade(tmp_path):
    s = _settings(tmp_path)
    assert refusal(_snapshot(unprotected=1), None, s) is None  # 3 % + 3 % = 6 %: at the limit
    assert refusal(_snapshot(unprotected=1, open_risk=D("1")), None, s) is not None


@pytest.mark.asyncio
async def test_main_refused_order_never_reaches_mt5_and_signal_is_rejected(tmp_path, monkeypatch):
    from config.settings import settings
    from main import ApplicationManager
    from src.signal_parser.regex_parser import RegexParser

    monkeypatch.setattr(settings, "kill_switch_file", tmp_path / "STOP_TRADING")
    app = ApplicationManager()
    app.mt5_connection_manager = MagicMock()
    app._signal_repository = SimpleNamespace(
        find_by_message_id=AsyncMock(return_value=SimpleNamespace(id=7, status="PENDING")),
        claim_signal_for_execution=AsyncMock(return_value=True),
        update_signal_status=AsyncMock(),
    )
    app.position_manager = SimpleNamespace(
        get_risk_snapshot=AsyncMock(return_value=_snapshot(open_legs=6)),
        open_market_order=AsyncMock(),
    )
    signal = RegexParser().parse("SELL GOLD 4149\nSL 4165\nTP 4115")
    signal.telegram_message_id = 1

    assert await app._execute_signal_on_mt5(signal, volume=0.1) is None
    app.position_manager.open_market_order.assert_not_awaited()
    assert (
        "risk guard"
        in app._signal_repository.update_signal_status.await_args.kwargs["rejection_reason"]
    )

    # Under the limits the same order goes to MT5
    app.position_manager.get_risk_snapshot.return_value = _snapshot()
    app.position_manager.open_market_order.return_value = SimpleNamespace(
        success=False, error_message="x"
    )
    await app._execute_signal_on_mt5(signal, volume=0.1)
    app.position_manager.open_market_order.assert_awaited_once()


@pytest.mark.asyncio
async def test_snapshot_sums_sl_risk_and_only_today_server_deals(monkeypatch):
    from contextlib import asynccontextmanager

    import src.mt5_executor.position_manager as pm

    day = 86400 * 20000
    gold = SimpleNamespace(name="Gold", trade_tick_size=0.01, trade_tick_value=1.0)
    fake = SimpleNamespace(
        POSITION_TYPE_BUY=0,
        DEAL_TYPE_BUY=0,
        DEAL_TYPE_SELL=1,
        account_info=lambda: SimpleNamespace(balance=10000.0, equity=9950.0),
        terminal_info=lambda: SimpleNamespace(trade_allowed=True),
        symbol_info=lambda _: gold,
        symbol_info_tick=lambda _: SimpleNamespace(time=day + 3600),
        positions_get=lambda: [
            SimpleNamespace(
                symbol="Gold", type=0, price_open=4140.0, sl=4130.0, volume=0.1
            ),  # $100
            SimpleNamespace(
                symbol="Gold", type=1, price_open=4140.0, sl=4135.0, volume=0.1
            ),  # past BE: $0
            SimpleNamespace(symbol="Gold", type=0, price_open=4140.0, sl=0.0, volume=0.1),  # no SL
        ],
        history_deals_get=lambda *_: [
            SimpleNamespace(
                time=day - 60, type=0, profit=-500.0, commission=0.0, swap=0.0
            ),  # yesterday
            SimpleNamespace(time=day + 60, type=1, profit=-80.0, commission=-2.0, swap=0.0),
            SimpleNamespace(
                time=day + 90, type=2, profit=1000.0, commission=0.0, swap=0.0
            ),  # deposit
        ],
    )
    monkeypatch.setattr(pm, "mt5", fake)

    @asynccontextmanager
    async def connection():
        yield SimpleNamespace(connected=True)

    manager = pm.PositionManager(SimpleNamespace(get_connection=connection))
    snap = await manager.get_risk_snapshot("GOLD")
    assert snap["open_legs"] == 3 and snap["unprotected"] == 1
    assert snap["open_risk"] == D("100") and snap["realized_today"] == D("-82")
    assert snap["trade_allowed"] is True


def test_autotrading_off_refuses_with_clear_reason(tmp_path):
    reason = refusal(_snapshot(trade_allowed=False), D("1"), _settings(tmp_path))
    assert "AutoTrading is OFF" in reason
    assert refusal(_snapshot(trade_allowed=True), D("1"), _settings(tmp_path)) is None


@pytest.mark.asyncio
async def test_health_check_flags_autotrading_off_once_and_dashboard_shows_it(caplog):
    from src.monitoring.console_dashboard import ConsoleDashboard
    from src.monitoring.health_checker import HealthMonitor, HealthStatus

    monitor = HealthMonitor()
    status = {
        "pool_size": 1,
        "active_connections": 1,
        "healthy_connections": 1,
        "trade_allowed": False,
    }
    monitor.set_mt5_connection(
        SimpleNamespace(check_connection_health=AsyncMock(return_value=status))
    )

    await monitor._check_mt5_health()
    await monitor._check_mt5_health()
    health = monitor.get_component_health("mt5")
    assert health.status == HealthStatus.DEGRADED and health.details["trade_allowed"] is False
    assert (
        sum(r.levelname == "ERROR" and "AutoTrading is OFF" in r.message for r in caplog.records)
        == 1
    )
    panel = ConsoleDashboard(monitor)._build_connections_panel()
    assert "AUTOTRADING OFF" in str(panel.renderable.columns[1]._cells)

    status["trade_allowed"] = True
    await monitor._check_mt5_health()
    assert monitor.get_component_health("mt5").status == HealthStatus.HEALTHY
