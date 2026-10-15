"""Story 20.11: the next ladder leg opens even when the EXECUTED status write failed.

Proof of fill when the claim is lost inside a setup: the signal was filled by this run, else a
position for it is in the DB (restart-shaped). Without a proof, no order and the skip log names the
status.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.database.models import SignalStatus
from src.database.repository import RepositoryError
from tests.unit.test_fill_survives_db_errors import _db_down, _errors, _signal
from tests.unit.test_single_signal_live_sizing import _app

pytestmark = pytest.mark.unit

D = Decimal
LADDER = "SELL GOLD 4190 4195\nSL 4200\nTP 4180\nTP 4170"


def _warnings(app) -> list[str]:
    return [str(c) for c in app.logger.warning.call_args_list]


def _infos(app) -> list[str]:
    return [str(c) for c in app.logger.info.call_args_list]


def _db_app(monkeypatch, tmp_path, executed_write=None, status="PENDING"):
    """App whose fake signal row behaves like the DB: claim PENDING -> VALIDATED, status writes
    stick unless `executed_write` replaces the EXECUTED write (raise or return False)."""
    app = _app(monkeypatch, tmp_path, "4190.3")
    app._validate_live_price = AsyncMock(return_value=True)
    app._order_volume = AsyncMock(return_value=0.1)
    row = SimpleNamespace(id=7, status=status)

    async def claim(_signal_id):
        if row.status != "PENDING":
            return False
        row.status = SignalStatus.VALIDATED
        return True

    async def update_signal_status(status, **_):
        if status == SignalStatus.EXECUTED and executed_write is not None:
            return executed_write()
        row.status = status
        return True

    app._signal_repository.find_by_message_id = AsyncMock(return_value=row)
    app._signal_repository.claim_signal_for_execution = AsyncMock(side_effect=claim)
    app._signal_repository.update_signal_status = AsyncMock(side_effect=update_signal_status)

    tickets = iter([99, 100, 101])

    async def open_market_order(signal, **_):
        return SimpleNamespace(
            success=True,
            ticket=next(tickets),
            fill_price=float(signal.entry_price),
            error_message=None,
        )

    app.position_manager.open_market_order = AsyncMock(side_effect=open_market_order)
    app._position_repository.create = AsyncMock(side_effect=lambda **f: SimpleNamespace(id=1, **f))
    app._position_repository.get_positions_by_signal = AsyncMock(return_value=[])
    return app


def _raise():
    raise RepositoryError("database is locked")


async def _two_legs(app):
    setup, _ = app._setup_aggregator.start(_signal(LADDER), datetime.now(UTC))
    await app._maybe_fire_setup(setup, D("4190.3"))
    await app._maybe_fire_setup(setup, D("4195"))
    return setup


def _proof_warnings(app) -> list[str]:
    return [w for w in _warnings(app) if "not marked EXECUTED" in w]


# --- Through the setup (AC 1, 2, 7, 9)


async def test_leg_two_opens_when_executed_write_raises(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, executed_write=_raise)

    setup = await _two_legs(app)

    assert [p.mt5_ticket for p in setup.positions] == [99, 100]
    assert not any("order not filled" in str(c) for c in app.logger.method_calls)
    lines = _proof_warnings(app)
    assert len(lines) == 1
    assert "Signal 7" in lines[0] and "status=VALIDATED" in lines[0]
    assert "filled by this run" in lines[0]


async def test_leg_two_opens_when_executed_write_finds_no_row(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, executed_write=lambda: False)

    setup = await _two_legs(app)

    assert [p.mt5_ticket for p in setup.positions] == [99, 100]
    lines = [e for e in _errors(app) if "was not marked EXECUTED" in e]
    assert len(lines) == 1 and "row not found" in lines[0]


async def test_unsaved_position_and_failed_status_still_open_leg_two(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, executed_write=_raise)
    _db_down(app)

    setup = await _two_legs(app)

    assert [p.mt5_ticket for p in setup.positions] == [99, 100]
    lines = _proof_warnings(app)
    assert len(lines) == 1 and "filled by this run" in lines[0]
    app._position_repository.get_positions_by_signal.assert_not_awaited()


async def test_healthy_db_ladder_opens_leg_two_without_warning(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path)

    setup = await _two_legs(app)

    assert [p.mt5_ticket for p in setup.positions] == [99, 100]
    assert not _proof_warnings(app)
    assert not _errors(app)


# --- _execute_signal_on_mt5 on a lost claim (AC 3-8)


async def test_in_flight_signal_without_proof_sends_no_order(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, status="VALIDATED")

    assert await app._execute_signal_on_mt5(_signal(), volume=0.1, allow_reuse=True) is None
    app.position_manager.open_market_order.assert_not_awaited()
    skips = [i for i in _infos(app) if "already claimed or processed" in i]
    assert len(skips) == 1 and "(status=VALIDATED)" in skips[0]


async def test_single_signal_never_reuses_a_filled_signal(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, status="VALIDATED")
    app._filled_signal_ids = frozenset({7})
    app._position_repository.get_positions_by_signal = AsyncMock(return_value=[object()])

    assert await app._execute_signal_on_mt5(_signal(), volume=0.1, allow_reuse=False) is None
    app.position_manager.open_market_order.assert_not_awaited()
    assert any("(status=VALIDATED)" in i for i in _infos(app))


async def test_position_in_db_opens_the_leg_after_restart(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, status="VALIDATED")
    app._position_repository.get_positions_by_signal = AsyncMock(return_value=[object()])

    position = await app._execute_signal_on_mt5(_signal(), volume=0.1, allow_reuse=True)

    assert position is not None and position.mt5_ticket == 99
    app._position_repository.get_positions_by_signal.assert_awaited_once_with(7)
    lines = _proof_warnings(app)
    assert len(lines) == 1 and "position in DB" in lines[0]
    app._signal_repository.update_signal_status.assert_not_awaited()  # extra leg: no status write


async def test_failed_db_proof_read_sends_no_order(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, status="VALIDATED")
    app._position_repository.get_positions_by_signal = AsyncMock(
        side_effect=RepositoryError("database is locked")
    )

    assert await app._execute_signal_on_mt5(_signal(), volume=0.1, allow_reuse=True) is None
    app.position_manager.open_market_order.assert_not_awaited()
    assert len([w for w in _warnings(app) if "Signal 7" in w]) == 1


async def test_signal_filled_by_this_run_skips_the_db_read(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, status="VALIDATED")
    app._filled_signal_ids = frozenset({7})

    position = await app._execute_signal_on_mt5(_signal(), volume=0.1, allow_reuse=True)

    assert position is not None
    app._position_repository.get_positions_by_signal.assert_not_awaited()


async def test_missing_position_repository_gives_no_db_proof(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, status="VALIDATED")
    app._position_repository = None

    assert await app._execute_signal_on_mt5(_signal(), volume=0.1, allow_reuse=True) is None
    app.position_manager.open_market_order.assert_not_awaited()
    assert not _proof_warnings(app)


async def test_rejected_signal_is_never_reused(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, status="REJECTED")

    assert await app._execute_signal_on_mt5(_signal(), volume=0.1, allow_reuse=True) is None
    app.position_manager.open_market_order.assert_not_awaited()
    assert any("(status=REJECTED)" in i for i in _infos(app))


# --- Regression (AC 9): risk guard on an extra leg


async def test_risk_guard_refusal_on_extra_leg_writes_no_status(monkeypatch, tmp_path):
    app = _db_app(monkeypatch, tmp_path, status="VALIDATED")
    app._filled_signal_ids = frozenset({7})
    app._risk_guard_refusal = AsyncMock(return_value="max open legs")

    assert await app._execute_signal_on_mt5(_signal(), volume=0.1, allow_reuse=True) is None
    app.position_manager.open_market_order.assert_not_awaited()
    app._signal_repository.update_signal_status.assert_not_awaited()
