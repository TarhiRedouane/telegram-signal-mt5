"""Story 20.2: break-even and invalidation close are retried until MT5 accepts them (review F21).

MT5 is mocked at the PositionManager layer (`modify_position_levels`, `close_position_full`,
`get_close_reason`), so `_apply_position_levels`, `_track_setup_legs` and `_check_close_invalidation`
run for real. The real decorated PositionManager methods are never called, so no circuit-breaker
state is shared between tests.
"""

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.settings import settings
from src.correlation_engine.setup_aggregator import PendingSetup
from src.signal_parser import ParsedAction

pytestmark = pytest.mark.unit

NOW = datetime(2026, 10, 12, 10, 0, 0, tzinfo=UTC)
OK = SimpleNamespace(success=True, retcode=10009, error_message=None)
REQUOTE = SimpleNamespace(success=False, retcode=10004, error_message="Requote")
NO_CHANGES = SimpleNamespace(success=False, retcode=10025, error_message="No changes in request")


def _ladder(action=ParsedAction.SELL, **extra) -> PendingSetup:
    """Three filled legs; leg 1 (ticket 1) is the TP1 leg."""
    setup = PendingSetup(
        chat_id=1,
        message_id=10,
        sender="s",
        timestamp=NOW,
        action=action,
        symbol="GOLD",
        low=Decimal("4190"),
        high=Decimal("4195"),
        sl=Decimal("4200") if action == ParsedAction.SELL else Decimal("4110"),
        tps=[Decimal("4180"), Decimal("4170"), Decimal("4160")],
        last_update=NOW,
        **extra,
    )
    setup.positions = [
        SimpleNamespace(id=t, mt5_ticket=t, open_price=4190.0 + t) for t in (1, 2, 3)
    ]
    setup.armed = False
    return setup


def _app(**position_manager):
    from main import ApplicationManager

    app = ApplicationManager()
    app.mt5_connection_manager = SimpleNamespace()  # "connected"; no direct calls in these paths
    app.position_manager = SimpleNamespace(**position_manager)
    return app


def _tp1_hit():
    """Leg 1 closed at TP; legs 2 and 3 still open."""
    return AsyncMock(side_effect=lambda ticket: "tp" if ticket == 1 else None)


async def test_break_even_failure_is_retried_on_next_poll():
    modify = AsyncMock(side_effect=[REQUOTE, OK, OK])  # leg 2 fails once, leg 3 ok, then leg 2 ok
    app = _app(get_close_reason=_tp1_hit(), modify_position_levels=modify)
    setup = _ladder()

    assert await app._track_setup_legs(setup) is False
    assert not setup.be_done and setup.be_pending
    assert not setup.armed and not setup.fire_now  # unfilled legs cancelled at once

    assert await app._track_setup_legs(setup) is False
    assert setup.be_done and not setup.be_pending
    assert [c.kwargs["ticket"] for c in modify.await_args_list] == [
        2,
        3,
        2,
    ]  # only the failed leg retried
    assert modify.await_args_list[2].kwargs["new_sl"] == 4192.0  # leg 2's open price


async def test_break_even_no_changes_counts_as_applied():
    modify = AsyncMock(return_value=NO_CHANGES)
    app = _app(get_close_reason=_tp1_hit(), modify_position_levels=modify)
    setup = _ladder()

    assert await app._apply_position_levels(setup.positions[1], Decimal("4192"), None) is True
    modify.reset_mock()

    await app._track_setup_legs(setup)
    assert setup.be_done and not setup.be_pending
    assert modify.await_count == 2  # once per open leg, no retry


async def test_leg_closed_while_break_even_pending_is_not_retried():
    reasons = {1: "tp", 2: None, 3: None}
    modify = AsyncMock(side_effect=[REQUOTE, OK])
    app = _app(
        get_close_reason=AsyncMock(side_effect=lambda t: reasons[t]),
        modify_position_levels=modify,
    )
    setup = _ladder()
    await app._track_setup_legs(setup)
    assert setup.be_pending

    reasons[2] = "sl"  # failed leg stopped out before the retry
    await app._track_setup_legs(setup)
    assert setup.be_done and not setup.be_pending
    assert modify.await_count == 2  # no retry on the closed leg


async def test_ten_failures_log_one_error_and_keep_retrying(caplog):
    modify = AsyncMock(return_value=REQUOTE)
    app = _app(get_close_reason=_tp1_hit(), modify_position_levels=modify)
    setup = _ladder()
    for _ in range(12):
        await app._track_setup_legs(setup)
    errors = [r for r in caplog.records if "still not at break-even" in r.getMessage()]
    assert len(errors) == 2  # once for ticket 2, once for ticket 3, at the 10th failure each
    assert all(r.levelname == "ERROR" for r in errors)
    assert setup.be_pending and not setup.be_done
    assert modify.await_count == 24  # still retrying both legs every poll


async def test_idle_timeout_does_not_drop_setup_while_break_even_pending(monkeypatch):
    monkeypatch.setattr(settings, "setup_price_poll_seconds", 0)
    modify = AsyncMock(return_value=REQUOTE)
    app = _app(get_close_reason=_tp1_hit(), modify_position_levels=modify)
    setup = _ladder()
    setup.last_update = datetime.now(UTC) - timedelta(
        seconds=settings.setup_idle_timeout_seconds + 60
    )
    app._setup_aggregator.adopt(setup)

    task = asyncio.create_task(app._watch_setup(setup))
    for _ in range(50):
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert app._setup_aggregator.get(setup.chat_id) is setup  # kept for the retry
    assert modify.await_count >= 4  # retried over several polls


def _invalidation_app(close, reasons=None):
    app = _app(
        close_position_full=close,
        get_close_reason=AsyncMock(side_effect=lambda t: (reasons or {}).get(t)),
    )

    def bar(t, close_):  # (time, open, high, low, close, tick_volume)
        return (t, Decimal("4125"), Decimal("4126"), Decimal("4119"), Decimal(close_), 10)

    rates = [bar(0, "4124")]
    app.mt5_connection_manager = SimpleNamespace(get_closed_rates=AsyncMock(return_value=rates))
    setup = _ladder(action=ParsedAction.BUY, close_invalidation=(Decimal("4121"), 30))
    setup.positions = setup.positions[:2]
    app._setup_aggregator.adopt(setup)
    return app, setup, rates, bar


async def test_failed_invalidation_close_is_retried_and_setup_dropped_only_when_flat():
    close = AsyncMock(side_effect=[OK, REQUOTE, OK])
    app, setup, rates, bar = _invalidation_app(close)
    assert await app._check_close_invalidation(setup) is False  # baseline
    rates.append(bar(1800, "4120.4"))  # M30 close below 4121

    assert await app._check_close_invalidation(setup) is False  # leg 2 still open
    assert setup.close_pending and not setup.armed
    assert app._setup_aggregator.get(setup.chat_id) is setup

    assert await app._check_close_invalidation(setup) is True
    assert [c.args[0] for c in close.await_args_list] == [1, 2, 2]
    assert app._setup_aggregator.get(setup.chat_id) is None


async def test_failed_close_of_leg_already_closed_counts_as_done():
    close = AsyncMock(side_effect=[OK, SimpleNamespace(success=False, error_message="not found")])
    app, setup, rates, bar = _invalidation_app(close, reasons={2: "sl"})
    await app._check_close_invalidation(setup)
    rates.append(bar(1800, "4120.4"))

    assert await app._check_close_invalidation(setup) is True  # leg 2 was stopped out already
    assert app._setup_aggregator.get(setup.chat_id) is None


async def test_profit_locking_sl_during_pending_break_even_is_not_undone_by_retry():
    """QA 20.2: a tighter author SL applied while break-even is pending must not be loosened back to entry."""
    # BE: leg 2 fails, leg 3 ok; author SL: sent to legs 1-3 (leg 1 is closed; pre-existing behaviour)
    modify = AsyncMock(side_effect=[REQUOTE, OK, OK, OK, OK])
    app = _app(get_close_reason=_tp1_hit(), modify_position_levels=modify)
    setup = _ladder()
    await app._track_setup_legs(setup)
    assert setup.be_pending

    setup.sl = Decimal("4185")  # SELL legs opened at 4191-4193: locks profit
    await app._apply_setup_levels(setup, ["sl"])
    await app._track_setup_legs(setup)

    assert setup.be_done and not setup.be_pending
    sls = [(c.kwargs["ticket"], c.kwargs["new_sl"]) for c in modify.await_args_list]
    # No retry moving leg 2 back to 4192 after the author's 4185
    assert sls == [(2, 4192.0), (3, 4193.0), (1, 4185.0), (2, 4185.0), (3, 4185.0)]
