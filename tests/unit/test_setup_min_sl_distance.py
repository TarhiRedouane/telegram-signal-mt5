"""Story 20.12: a setup leg waits while the market is closer to its SL than the policy minimum (F43)."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from config.settings import settings
from tests.unit.test_setup_aggregator import ZONE_CHAT, _send, _zone_app
from tests.unit.test_single_signal_live_sizing import CHAT, _app, _post

pytestmark = pytest.mark.unit

D = Decimal
SELL_2TP = "SELL GOLD 4150\nSL 4160\nTP1 4140\nTP2 4130"
BUY_2TP = "BUY GOLD 4150\nSL 4140\nTP1 4160\nTP2 4170"
WAITING = "Setup leg waiting"


@pytest.fixture(autouse=True)
def _gold_values(monkeypatch):
    # Story values; .env may tune them on a live instance
    monkeypatch.setattr(settings, "gold_min_sl_distance", D("2.0"), raising=False)
    monkeypatch.setattr(settings, "gold_entry_tolerance", D("1.0"))
    monkeypatch.setattr(settings, "gold_default_sl_distance", D("10.0"))


async def _setup(*texts: str, price: str = "4150"):
    app, opened = _zone_app(price)
    for message_id, text in enumerate(texts, start=1):
        await _send(app, message_id, text)
    return app, opened, app._setup_aggregator.get(ZONE_CHAT)


async def test_complete_multi_tp_sell_waits_near_the_sl_then_fires(caplog):  # AC 1
    caplog.set_level(logging.INFO)
    app, opened, setup = await _setup(SELL_2TP)
    await app._maybe_fire_setup(setup, D("4159.8"))
    assert opened == [] and setup.armed
    assert caplog.text.count(WAITING) == 1
    assert "SELL price 4159.8 is 0.2 from SL 4160 (minimum 2.0)" in caplog.text

    await app._maybe_fire_setup(setup, D("4157"))
    assert [o[:2] for o in opened] == [(D("4160"), D("4140")), (D("4160"), D("4130"))]


async def test_legs_are_sized_on_the_distance_they_fire_at(monkeypatch, tmp_path):  # AC 1 sizing
    app = _app(monkeypatch, tmp_path, "4157")
    app._watch_setup = AsyncMock()
    await _post(app, SELL_2TP)
    setup = app._setup_aggregator.get(CHAT)
    await app._maybe_fire_setup(setup, D("4159.8"))
    app.position_manager.open_market_order.assert_not_awaited()

    await app._maybe_fire_setup(setup, D("4157"))
    # 300 / (3.0 / 0.01 * 1) = 1.0 lot, split over 2 legs
    volumes = [c.kwargs["volume"] for c in app.position_manager.open_market_order.await_args_list]
    assert volumes == pytest.approx([0.50, 0.50])


async def test_buy_mirror():  # AC 2
    app, opened, setup = await _setup(BUY_2TP)
    await app._maybe_fire_setup(setup, D("4140.5"))
    assert opened == []
    await app._maybe_fire_setup(setup, D("4143"))
    assert len(opened) == 2


@pytest.mark.parametrize("price, fires", [("4146", True), ("4148.5", False), ("4148", True)])
async def test_await_entry_sell(price, fires):  # AC 3: a fresh setup per price
    app, opened, setup = await _setup("sell gold now", "sl 4150", "4146", price=price)
    await app._maybe_fire_setup(setup, D(price))
    assert bool(opened) is fires


async def test_zone_sell_waits_inside_the_tolerance_next_to_the_sl():  # AC 4
    app, opened, setup = await _setup(
        "SELL GOLD 4147 - 4153", "SL 4155", "TP1 4140", "TP2 4130", price="4154"
    )
    await app._maybe_fire_setup(setup, D("4154"))
    assert opened == []
    await app._maybe_fire_setup(setup, D("4152"))
    assert opened


@pytest.mark.parametrize("price, fires", [("4150", True), ("4150.5", False)])
async def test_author_tighter_stop_lowers_the_minimum(price, fires):  # AC 5 (D1)
    app, opened, setup = await _setup("SELL GOLD 4150\nSL 4151\nTP1 4140\nTP2 4130", price=price)
    await app._maybe_fire_setup(setup, D(price))
    assert bool(opened) is fires


async def test_later_ladder_leg_waits_near_the_sl():  # rule 1: leg 1 or later
    app, opened, setup = await _setup(
        "SELL GOLD 4147 - 4153", "SL 4155", "TP1 4140", "TP2 4130", price="4147"
    )
    await app._maybe_fire_setup(setup, D("4147"))
    assert len(opened) == 1 and setup.armed  # leg 2 laddered at 4153
    await app._maybe_fire_setup(setup, D("4154"))  # leg 2 due (tolerance) but 1.0 from SL
    assert len(opened) == 1
    await app._maybe_fire_setup(setup, D("4152.5"))
    assert len(opened) == 2


async def test_default_sl_legs_fire_as_today():  # AC 6
    app, opened, setup = await _setup("SELL GOLD 4147 - 4153", "TP1 4140", "TP2 4130")
    assert setup.sl is None
    await app._maybe_fire_setup(setup, D("4153"))
    assert opened and all(o[0] == D("4163.0") for o in opened)


async def test_waiting_until_idle_timeout_expires_without_order(caplog):  # AC 7
    from main import ApplicationManager

    caplog.set_level(logging.INFO)
    app, opened, setup = await _setup(SELL_2TP)
    await app._maybe_fire_setup(setup, D("4159.8"))
    setup.last_update = datetime.now(UTC) - timedelta(
        seconds=settings.setup_idle_timeout_seconds + 1
    )
    await asyncio.wait_for(ApplicationManager._watch_setup(app, setup), 2)
    assert app._setup_aggregator.get(ZONE_CHAT) is None and opened == []
    assert "expired before entry" in caplog.text


async def test_waiting_logs_once_and_skips_the_live_check(caplog):  # AC 8
    caplog.set_level(logging.INFO)
    app, opened, setup = await _setup(SELL_2TP)
    app._validate_live_price.reset_mock()  # message intake checks once (main.py:430)
    for _ in range(30):
        await app._maybe_fire_setup(setup, D("4159.8"))
    assert opened == [] and caplog.text.count(WAITING) == 1
    app._validate_live_price.assert_not_awaited()


async def test_price_beyond_sl_is_not_delayed_by_the_guard(caplog):  # AC 9 (rule 3)
    caplog.set_level(logging.INFO)
    app, opened, setup = await _setup("sell gold now", "sl 4150", "4146", price="4150.5")
    await app._maybe_fire_setup(setup, D("4150.5"))
    assert opened == [] and app._setup_aggregator.get(ZONE_CHAT) is None
    assert "invalidated at price 4150.5" in caplog.text

    app, opened, setup = await _setup(SELL_2TP, price="4160.5")
    await app._maybe_fire_setup(setup, D("4160.5"))
    # Leg 1 is sent as today (MT5 refuses these stops in real use); leg 2 is "invalidated"
    assert opened == [(D("4160"), D("4140"), False)]
