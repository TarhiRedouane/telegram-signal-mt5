"""Story 20.9: a filled order stays tracked even when a database write fails (review F37).

After MT5 accepted the order, a failing `create` / `update_signal_status` must not turn the fill into
"not filled"; after MT5 accepted an SL move, a failing `update_position_mt5_data` must not kill the
setup watcher.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.database.models import SignalStatus
from src.database.repository import RepositoryError
from src.signal_parser.regex_parser import RegexParser
from tests.unit.test_break_even_retry import OK, _ladder, _tp1_hit
from tests.unit.test_break_even_retry import _app as _be_app
from tests.unit.test_single_signal_live_sizing import CHAT, SELL, _app, _post

pytestmark = pytest.mark.unit

D = Decimal


def _signal(text=SELL, message_id=1):
    signal = RegexParser().parse(text)
    signal.telegram_chat_id = CHAT
    signal.telegram_message_id = message_id
    signal.timestamp = datetime.now(UTC)
    return signal


def _errors(app) -> list[str]:
    return [str(c) for c in app.logger.error.call_args_list]


def _db_down(app) -> None:
    app._position_repository.create = AsyncMock(side_effect=RepositoryError("database is locked"))


# --- _execute_signal_on_mt5 after a fill


async def test_failed_position_save_still_returns_the_filled_position(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4150")
    _db_down(app)

    position = await app._execute_signal_on_mt5(_signal(), volume=0.3)

    assert position is not None
    assert (position.mt5_ticket, position.id) == (99, None)
    assert (position.open_price, position.volume) == (4150.0, 0.3)
    assert (position.current_sl, position.current_tp) == (4160.0, 4130.0)
    lines = [line for line in _errors(app) if "Ticket 99" in line]
    assert len(lines) == 1 and "not saved" in lines[0]
    app._signal_repository.update_signal_status.assert_awaited_once()  # still marked EXECUTED


async def test_missing_position_repository_still_returns_the_filled_position(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4150")
    app._position_repository = None

    position = await app._execute_signal_on_mt5(_signal(), volume=0.3)

    assert position is not None
    assert (position.mt5_ticket, position.id, position.open_price) == (99, None, 4150.0)
    assert len([line for line in _errors(app) if "Ticket 99" in line]) == 1


async def test_failed_executed_status_keeps_the_position(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4150")

    async def update_signal_status(**kwargs):
        if kwargs["status"] == SignalStatus.EXECUTED:
            raise RepositoryError("database is locked")

    app._signal_repository.update_signal_status = AsyncMock(side_effect=update_signal_status)

    position = await app._execute_signal_on_mt5(_signal(), volume=0.3)

    assert position is app._position_repository.create.return_value
    lines = [line for line in _errors(app) if "Ticket 99" in line]
    assert len(lines) == 1 and "EXECUTED" in lines[0]


async def test_setup_leg_with_failed_save_stays_in_the_setup(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4190.3")
    app._validate_live_price = AsyncMock(return_value=True)
    app._order_volume = AsyncMock(return_value=0.1)
    _db_down(app)
    setup, _ = app._setup_aggregator.start(
        _signal("SELL GOLD 4190 4195\nSL 4200\nTP 4180"), datetime.now(UTC)
    )

    await app._maybe_fire_setup(setup, D("4190.3"))

    assert [p.mt5_ticket for p in setup.positions] == [99]
    assert setup.positions[0].open_price == 4190.3  # break-even moves it to the fill
    assert app._setup_aggregator.get(CHAT) is setup
    assert not any("order not filled" in str(c) for c in app.logger.method_calls)


async def test_single_signal_with_failed_save_shows_new_not_not_filled(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4150")
    app.console_dashboard = MagicMock()
    _db_down(app)

    await _post(app, SELL)

    kwargs = app.console_dashboard.add_recent_signal.call_args.kwargs
    assert (kwargs["signal_type"], kwargs["ticket_id"]) == ("NEW", "99")


# --- _apply_position_levels after MT5 accepted the change


def _levels_app(update):
    app = _be_app(modify_position_levels=AsyncMock(return_value=OK))
    app.logger = MagicMock()
    app._position_repository = SimpleNamespace(update_position_mt5_data=update)
    return app


async def test_db_error_after_accepted_levels_returns_true():
    app = _levels_app(AsyncMock(side_effect=RepositoryError("database is locked")))
    position = SimpleNamespace(id=5, mt5_ticket=2, open_price=4192.0)

    assert await app._apply_position_levels(position, D("4192"), None) is True
    lines = [line for line in _errors(app) if "ticket 2" in line.lower()]
    assert len(lines) == 1 and "4192" in lines[0]


async def test_unsaved_position_skips_the_db_write():
    update = AsyncMock()
    app = _levels_app(update)
    position = SimpleNamespace(id=None, mt5_ticket=2, open_price=4192.0)

    assert await app._apply_position_levels(position, D("4192"), None) is True
    update.assert_not_awaited()


async def test_break_even_completes_when_its_db_write_fails():
    app = _levels_app(AsyncMock(side_effect=RepositoryError("database is locked")))
    app.position_manager.get_close_reason = _tp1_hit()
    setup = _ladder()

    assert await app._track_setup_legs(setup) is False  # no exception into the watcher
    assert setup.be_done and not setup.be_pending


# --- Regression: no order sent -> None, healthy DB unchanged


async def test_rejected_order_still_returns_none(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4150")
    app.position_manager.open_market_order = AsyncMock(
        return_value=SimpleNamespace(success=False, ticket=None, error_message="no money")
    )

    assert await app._execute_signal_on_mt5(_signal(), volume=0.3) is None
    status = app._signal_repository.update_signal_status.await_args.kwargs["status"]
    assert status == SignalStatus.REJECTED


async def test_lost_claim_sends_no_order(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4150")
    app._signal_repository.claim_signal_for_execution = AsyncMock(return_value=False)

    assert await app._execute_signal_on_mt5(_signal(), volume=0.3) is None
    app.position_manager.open_market_order.assert_not_awaited()


async def test_healthy_db_returns_the_saved_position(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4150")

    position = await app._execute_signal_on_mt5(_signal(), volume=0.3)

    assert position is app._position_repository.create.return_value
    assert not _errors(app)
