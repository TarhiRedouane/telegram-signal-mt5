"""Story 20.14: opposite "buy/sell gold now" while the chat's setup has open legs.

Under 300 pips (30.0) from the nearest open entry: refused, open trade untouched. At 300 pips or more:
the open trade is held (break-even and invalidation still managed) and a one-order counter-trade at
half risk waits for its entry, with a default TP of 100 pips (10.0).

Common: SELL legs open at 4150 and 4155 (tickets 101, 102), SL 4160, TPs 4140/4130, not armed.
"""

import asyncio
import contextlib
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from config.settings import settings
from src.correlation_engine.setup_aggregator import PendingSetup
from src.signal_parser import ParsedAction
from tests.unit.test_setup_aggregator import NOW, ZONE_CHAT, _send, _zone_app

pytestmark = pytest.mark.unit

D = Decimal
SELL, BUY = ParsedAction.SELL, ParsedAction.BUY
OK = SimpleNamespace(success=True, retcode=10009, error_message=None)


@pytest.fixture(autouse=True)
def _gold_values(monkeypatch):
    # Story values (R1, R2); .env may tune them on a live instance
    monkeypatch.setattr(settings, "gold_entry_tolerance", D("1.0"))
    monkeypatch.setattr(settings, "gold_default_sl_distance", D("10.0"))
    monkeypatch.setattr(settings, "gold_min_sl_distance", D("2.0"))
    monkeypatch.setattr(settings, "gold_min_reversal_distance", D("30.0"), raising=False)
    monkeypatch.setattr(settings, "gold_counter_tp_distance", D("10.0"), raising=False)


def _open_sell(app, chat_id: int = ZONE_CHAT) -> PendingSetup:
    setup = PendingSetup(
        chat_id=chat_id,
        message_id=1,
        sender="s",
        timestamp=NOW,
        action=SELL,
        symbol="GOLD",
        low=D("4150"),
        high=D("4155"),
        sl=D("4160"),
        tps=[D("4140"), D("4130")],
        last_update=datetime.now(UTC),
    )
    setup.positions = [
        SimpleNamespace(id=1, mt5_ticket=101, open_price=4150.0),
        SimpleNamespace(id=2, mt5_ticket=102, open_price=4155.0),
    ]
    setup.armed = False
    app._setup_aggregator.adopt(setup)
    return setup


def _app(price: str | None, margin_mode: int = 2, reasons: dict | None = None):
    """`_zone_app` + hedging account + close reasons per ticket (missing ticket = still open)."""
    app, opened = _zone_app("0")
    app.mt5_connection_manager.get_current_price = AsyncMock(
        return_value=None if price is None else D(price)
    )
    app.mt5_connection_manager.get_account_info = AsyncMock(
        return_value=SimpleNamespace(margin_mode=margin_mode)
    )
    app.position_manager = SimpleNamespace(
        get_close_reason=AsyncMock(side_effect=lambda t: (reasons or {}).get(t))
    )
    return app, opened, _open_sell(app)


async def _reversed(price: str = "4118", *texts: str):
    """Accepted reversal: `buy gold now` at `price`, then `texts`."""
    app, opened, sell = _app(price)
    for message_id, text in enumerate(["buy gold now", *texts], start=10):
        await _send(app, message_id, text)
    return app, opened, sell, app._setup_aggregator.get(ZONE_CHAT)


async def _run_watcher(app, setup, polls: int = 30):
    from main import ApplicationManager

    task = asyncio.create_task(ApplicationManager._watch_setup(app, setup))
    for _ in range(polls):
        await asyncio.sleep(0)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return task


# --- AC 1, 7, 8, 11: refused, open trade untouched -----------------------------------------------


async def test_opposite_now_near_the_open_trade_is_refused(caplog):
    caplog.set_level(logging.INFO)
    app, opened, sell = _app("4132")
    await _send(app, 10, "buy gold now")
    assert app._setup_aggregator.get(ZONE_CHAT) is sell and sell.action == SELL
    app._watch_setup.assert_not_called()
    assert "Reversal refused" in caplog.text and "18" in caplog.text and "30.0" in caplog.text
    await _send(app, 11, "4132")
    assert opened == []


@pytest.mark.parametrize("account", ["netting", "unreadable"])
async def test_netting_or_unreadable_account_is_refused(account, caplog):
    app, opened, sell = _app("4118", margin_mode=0)
    if account == "unreadable":  # e.g. MT5 circuit breaker open: fail closed
        app.mt5_connection_manager.get_account_info = AsyncMock(side_effect=RuntimeError("open"))
    await _send(app, 10, "buy gold now")
    assert app._setup_aggregator.get(ZONE_CHAT) is sell
    assert any(
        r.levelno == logging.WARNING and "Reversal refused" in r.getMessage()
        for r in caplog.records
    )


async def test_live_price_unavailable_is_refused(caplog):
    app, opened, sell = _app(None)
    await _send(app, 10, "buy gold now")
    assert app._setup_aggregator.get(ZONE_CHAT) is sell
    assert any(
        r.levelno == logging.WARNING and "Reversal refused" in r.getMessage()
        for r in caplog.records
    )


async def test_all_legs_closed_is_replaced_as_today():
    app, opened, sell = _app("4132", reasons={101: "tp", 102: "tp"})
    await _send(app, 10, "buy gold now")
    buy = app._setup_aggregator.get(ZONE_CHAT)
    assert buy is not sell and buy.action == BUY and buy.await_entry and not buy.counter
    assert not app._setup_aggregator.is_watched(sell)


async def test_only_open_legs_count_for_the_distance(caplog):
    caplog.set_level(logging.INFO)
    app, opened, sell = _app("4132", reasons={101: "tp"})  # leg 2 still open at 4155
    await _send(app, 10, "buy gold now")
    assert app._setup_aggregator.get(ZONE_CHAT) is sell
    assert "Reversal refused" in caplog.text and "23" in caplog.text


# --- AC 2-4: counter-trade -----------------------------------------------------------------------


async def test_opposite_now_far_from_the_open_trade_starts_a_counter_trade(caplog):
    caplog.set_level(logging.INFO)
    app, opened, sell, buy = await _reversed("4118")
    assert buy is not sell and buy.action == BUY and buy.counter and buy.await_entry
    assert app._setup_aggregator.is_watched(sell) and not sell.armed
    assert "Reversal accepted" in caplog.text and "[101, 102]" in caplog.text
    await _send(app, 11, "4118")
    await app._maybe_fire_setup(buy, D("4118.5"))
    assert opened == [(D("4108.5"), D("4128.5"), False)]  # default SL and TP, 10.0 each
    assert app._order_volume.await_args.args[1] == 2  # half risk, one order
    await app._maybe_fire_setup(buy, D("4110"))
    assert len(opened) == 1


@pytest.mark.parametrize("tp_text", ["TP 4125", "TP1 4125\nTP2 4135"])
async def test_author_tp_replaces_the_default_and_one_order_only(tp_text):
    app, opened, sell, buy = await _reversed("4118", tp_text, "4118")
    await app._maybe_fire_setup(buy, D("4118.5"))
    assert opened == [(D("4108.5"), D("4125"), False)]
    await app._maybe_fire_setup(buy, D("4118.5"))
    assert len(opened) == 1


async def test_counter_entry_too_close_to_the_open_trade_waits_then_expires(caplog):
    from main import ApplicationManager

    caplog.set_level(logging.INFO)
    app, opened, sell, buy = await _reversed("4118", "4135")
    for _ in range(3):
        await app._maybe_fire_setup(buy, D("4135"))
    assert opened == [] and caplog.text.count("Counter-trade waiting") == 1
    assert "15" in caplog.text
    buy.last_update = datetime.now(UTC) - timedelta(seconds=settings.setup_idle_timeout_seconds + 1)
    await ApplicationManager._watch_setup(app, buy)
    assert app._setup_aggregator.get(ZONE_CHAT) is None and opened == []
    assert app._setup_aggregator.is_watched(sell)


async def test_same_side_opener_on_a_filled_counter_trade_keeps_half_risk():
    app, opened, sell, buy = await _reversed("4118", "4118")
    await app._maybe_fire_setup(buy, D("4118.5"))
    await _send(app, 20, "buy gold now")  # D4: one more order at the next entry
    await _send(app, 21, "4119")
    await app._maybe_fire_setup(buy, D("4119"))
    assert opened[1] == (D("4109"), D("4129"), True)
    assert [c.args[1] for c in app._order_volume.await_args_list] == [2, 2]


# --- AC 5, 6, 12: the held trade stays managed ---------------------------------------------------


async def test_held_trade_keeps_break_even(monkeypatch):
    monkeypatch.setattr(settings, "setup_price_poll_seconds", 0)
    app, opened, sell, buy = await _reversed("4118")
    app.position_manager.get_close_reason = AsyncMock(
        side_effect=lambda t: "tp" if t == 101 else None
    )
    await _run_watcher(app, sell)
    assert sell.be_done
    app._apply_position_levels.assert_any_await(sell.positions[1], D("4155.0"), None)
    assert app._setup_aggregator.get(ZONE_CHAT) is buy


async def test_held_trade_keeps_candle_close_invalidation(monkeypatch):
    monkeypatch.setattr(settings, "setup_price_poll_seconds", 0)
    app, opened, sell, buy = await _reversed("4118")
    sell.close_invalidation = (D("4158"), 30)

    def bar(t, close):  # (time, open, high, low, close, tick_volume)
        return (t, D("4150"), D("4159"), D("4149"), D(close), 10)

    rates = [bar(0, "4152")]
    app.mt5_connection_manager.get_closed_rates = AsyncMock(return_value=rates)
    app.position_manager.close_position_full = AsyncMock(return_value=OK)
    await _run_watcher(app, sell, polls=5)  # baseline candle
    rates.append(bar(1800, "4158.5"))  # M30 closes above 4158
    task = await _run_watcher(app, sell)
    assert [c.args[0] for c in app.position_manager.close_position_full.await_args_list] == [
        101,
        102,
    ]
    assert not app._setup_aggregator.is_watched(sell) and task.done()
    assert app._setup_aggregator.get(ZONE_CHAT) is buy


async def test_held_watcher_task_keeps_a_strong_reference():
    app, opened, sell = _app("4118")
    app._watch_setup = lambda _setup: asyncio.sleep(3600)
    held_task = asyncio.create_task(asyncio.sleep(3600))
    app._component_tasks[f"setup_{ZONE_CHAT}"] = held_task
    await _send(app, 10, "buy gold now")
    counter_task = app._component_tasks[f"setup_{ZONE_CHAT}"]
    try:
        assert app._component_tasks[f"setup_held_{ZONE_CHAT}_1"] is held_task
        assert counter_task is not held_task and not held_task.done()
    finally:
        for task in (held_task, counter_task):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


# --- AC 2: volume with the real sizing -----------------------------------------------------------


async def test_counter_trade_is_sized_at_half_risk(monkeypatch, tmp_path):
    from tests.unit.test_single_signal_live_sizing import CHAT, _post, _sent_volume
    from tests.unit.test_single_signal_live_sizing import _app as _sizing_app

    app = _sizing_app(monkeypatch, tmp_path, "4118")
    app._watch_setup = AsyncMock()
    app.mt5_connection_manager.get_account_info = AsyncMock(
        return_value=SimpleNamespace(margin_mode=2)
    )
    app.position_manager.get_close_reason = AsyncMock(return_value=None)
    _open_sell(app, chat_id=CHAT)
    await _post(app, "buy gold now", message_id=10)
    await _post(app, "4118", message_id=11)
    buy = app._setup_aggregator.get(CHAT)
    await app._maybe_fire_setup(buy, D("4118.5"))
    # 1.5 % of 10 000 = 150; 10.0 stop = 1 000 ticks x $1 per lot -> 0.15 lot
    assert _sent_volume(app) == pytest.approx(0.15)
