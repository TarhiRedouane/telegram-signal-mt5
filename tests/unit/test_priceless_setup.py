"""Story 20.10: "sell gold now" waits for its entry price, then fires only close to it or better."""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from config.settings import settings
from src.signal_parser import ParsedAction
from tests.unit.test_setup_aggregator import NOW, ZONE_CHAT, _send, _zone_app

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _gold_values(monkeypatch):
    # Story values (D2, D3); .env may tune them on a live instance
    monkeypatch.setattr(settings, "gold_entry_tolerance", Decimal("1.0"))
    monkeypatch.setattr(settings, "gold_default_sl_distance", Decimal("10.0"))


async def _awaiting(*texts: str, opener: str = "sell gold now", price: str = "4146"):
    app, opened = _zone_app(price)
    for message_id, text in enumerate([opener, *texts], start=1):
        await _send(app, message_id, text)
    return app, opened, app._setup_aggregator.get(ZONE_CHAT)


async def _filled_sell(price: str = "4150"):
    app, opened = _zone_app(price)
    for message_id, text in enumerate(["sell gold 4150", "sl 4165", "TP 4115"], start=1):
        await _send(app, message_id, text)
    setup = app._setup_aggregator.get(ZONE_CHAT)
    await app._maybe_fire_setup(setup, Decimal(price))
    assert len(opened) == 1 and not setup.armed
    return app, opened, setup


# --- Detection helpers ---------------------------------------------------------------------------


def test_priceless_opener_detection():
    from src.correlation_engine.setup_aggregator import priceless_opener

    sell, buy = ParsedAction.SELL, ParsedAction.BUY
    for text, expected in [
        ("sell gold now", (sell, "GOLD")),
        ("SELL GOLD NOW", (sell, "GOLD")),
        ("je vends gold maintenant", (sell, "GOLD")),
        ("buy xauusd now", (buy, "XAUUSD")),
        ("sell gold", (sell, "GOLD")),
    ]:
        assert priceless_opener(text) == expected, text
    for text in [
        "sell gold 4149",
        "sell gold now SL 4165",
        "gold is moving",
        "buy and sell gold",
        "sell eurusd now",  # symbol not enabled
        "je ne vends pas gold",
        "gold sell-off",
        "pas de sell gold",
    ]:
        assert priceless_opener(text) is None, text


def test_bare_price_parsing():
    from src.correlation_engine.setup_aggregator import bare_price

    d = Decimal
    assert bare_price("4146") == (d("4146"), d("4146"))
    assert bare_price(" 4146,5 ") == (d("4146.5"), d("4146.5"))
    assert bare_price("4150 - 4146") == (d("4146"), d("4150"))
    assert bare_price("4146\u20134150") == (d("4146"), d("4150"))
    for text in ["sl 4146", "4146 sl", "4146 4150 4160", "414", "entrer à 4146", "TP 4115"]:
        assert bare_price(text) is None, text


# --- AC 1-2: opener, then entry ------------------------------------------------------------------


async def test_priceless_opener_starts_a_setup_awaiting_its_entry(caplog):
    caplog.set_level(logging.INFO)
    app, opened, setup = await _awaiting()
    assert setup is not None
    assert (setup.action, setup.symbol, setup.low, setup.high) == (
        ParsedAction.SELL,
        "GOLD",
        None,
        None,
    )
    assert setup.await_entry and not setup.fire_now
    app.llm_parser.parse_with_llm.assert_not_awaited()
    app._validate_live_price.assert_not_awaited()
    assert "awaiting entry" in caplog.text
    await app._maybe_fire_setup(setup, Decimal("4146"))
    assert opened == []


@pytest.mark.parametrize(
    "text, low, high",
    [
        ("4146", "4146", "4146"),
        ("4146.5", "4146.5", "4146.5"),
        ("4146,5", "4146.5", "4146.5"),
        ("4146-4150", "4146", "4150"),
        ("4146 - 4150", "4146", "4150"),
        ("4146\u20134150", "4146", "4150"),
    ],
)
async def test_bare_price_or_range_sets_the_entry_without_llm(text, low, high):
    app, _, setup = await _awaiting(text)
    assert (setup.low, setup.high) == (Decimal(low), Decimal(high))
    app.llm_parser.parse_with_llm.assert_not_awaited()


async def test_second_bare_price_before_the_fill_replaces_the_entry(caplog):
    caplog.set_level(logging.INFO)
    app, _, setup = await _awaiting("4146", "4148")
    assert (setup.low, setup.high) == (Decimal("4148"), Decimal("4148"))
    app.llm_parser.parse_with_llm.assert_not_awaited()
    assert caplog.text.count("Setup entry received") == 2


async def test_bare_price_is_not_an_entry_for_a_priced_setup():
    app, _, setup = await _awaiting("4146", opener="sell gold 4150")
    assert (setup.low, setup.high) == (Decimal("4150"), Decimal("4150"))
    app.llm_parser.parse_with_llm.assert_awaited_once()  # as today: LLM fallback


# --- AC 3-4: price band --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "price, fires", [("4140", False), ("4145.2", True), ("4150", True), ("4157", False)]
)
async def test_sell_fires_only_close_to_the_entry_or_better(price, fires, caplog):
    caplog.set_level(logging.INFO)
    app, opened, setup = await _awaiting("4146")
    await app._maybe_fire_setup(setup, Decimal(price))
    if not fires:
        assert opened == [] and setup.armed
        return
    assert opened == [(Decimal(price) + Decimal("10.0"), None, False)]  # default SL
    assert app._order_volume.await_args.args[1] == 2  # blind leg 1: half the risk
    assert "Setup firing" in caplog.text and not setup.await_entry


@pytest.mark.parametrize(
    "price, fires", [("4152", False), ("4146.8", True), ("4140", True), ("4135", False)]
)
async def test_buy_fires_only_close_to_the_entry_or_better(price, fires):
    app, opened, setup = await _awaiting("4146", opener="buy gold now")
    await app._maybe_fire_setup(setup, Decimal(price))
    expected = [(Decimal(price) - Decimal("10.0"), None, False)] if fires else []
    assert opened == expected


# --- AC 5-9, 11: sequences -----------------------------------------------------------------------


async def test_sl_tp_before_the_entry_are_used_at_fire_time():
    app, opened, setup = await _awaiting("sl 4165", "TP 4115")
    assert (setup.sl, setup.tps, setup.low) == (Decimal("4165"), [Decimal("4115")], None)
    await _send(app, 9, "4146")
    await app._maybe_fire_setup(setup, Decimal("4147"))
    assert opened == [(Decimal("4165"), Decimal("4115"), False)]


async def test_wrong_side_sl_before_the_entry_drops_the_setup_at_fire_time():
    app, opened, setup = await _awaiting("sl 4140", "4146")  # SELL with SL below the entry
    assert setup.sl == Decimal("4140")  # stored: no entry to check against yet
    await app._maybe_fire_setup(setup, Decimal("4146"))
    assert opened == [] and app._setup_aggregator.get(ZONE_CHAT) is None


async def test_now_received_while_waiting_does_not_push_later_legs_to_market():
    app, opened, setup = await _awaiting("vends maintenant", "TP 4130 | 4120", "4146-4150")
    assert setup.fire_now and setup.legs() == [Decimal("4146"), Decimal("4150")]
    await app._maybe_fire_setup(setup, Decimal("4146"))
    assert len(opened) == 1  # leg 2 (4150) waits for its level
    assert not setup.fire_now and not setup.await_entry
    await app._maybe_fire_setup(setup, Decimal("4149.2"))
    assert len(opened) == 2


async def test_complete_setup_still_waits_for_the_band():
    app, opened, setup = await _awaiting("sl 4165", "TP 4115", "4146")
    assert setup.is_complete
    await app._maybe_fire_setup(setup, Decimal("4130"))
    assert opened == []
    app._validate_live_price.assert_not_awaited()


async def test_now_follow_up_does_not_fire_at_market():
    app, opened, setup = await _awaiting("vends maintenant")
    for price in ("4100", "4146", "4200"):
        await app._maybe_fire_setup(setup, Decimal(price))
    assert opened == [] and app._setup_aggregator.get(ZONE_CHAT) is setup
    await _send(app, 9, "4146")
    await app._maybe_fire_setup(setup, Decimal("4130"))
    assert opened == []
    await app._maybe_fire_setup(setup, Decimal("4147"))
    assert len(opened) == 1


async def test_no_entry_within_the_idle_timeout_expires(caplog):
    from main import ApplicationManager

    caplog.set_level(logging.INFO)
    app, opened, setup = await _awaiting()
    setup.last_update = datetime.now(UTC) - timedelta(
        seconds=settings.setup_idle_timeout_seconds + 1
    )
    await ApplicationManager._watch_setup(app, setup)
    assert app._setup_aggregator.get(ZONE_CHAT) is None and opened == []
    assert "expired before entry" in caplog.text


async def test_cancel_drops_the_waiting_setup(caplog):
    caplog.set_level(logging.INFO)
    app, opened, _ = await _awaiting("annuler le setup")
    assert app._setup_aggregator.get(ZONE_CHAT) is None and opened == []
    assert "cancelled by author" in caplog.text


async def test_edited_opener_with_a_price_keeps_waiting_for_the_band():
    app, opened, setup = await _awaiting()
    await app._on_telegram_message(
        {
            "telegram_chat_id": ZONE_CHAT,
            "telegram_message_id": 1,
            "sender": "s",
            "timestamp": NOW,
            "raw_text": "sell gold now 4146",
            "edit_date": NOW,
        }
    )
    assert setup.low == Decimal("4146") and setup.await_entry
    await app._maybe_fire_setup(setup, Decimal("4130"))
    assert opened == []
    await app._maybe_fire_setup(setup, Decimal("4147"))
    assert len(opened) == 1


# --- AC 10: commentary is not an opener ----------------------------------------------------------


@pytest.mark.parametrize("text", ["je ne vends pas gold", "gold sell-off", "pas de sell gold"])
async def test_negated_or_commentary_side_word_starts_nothing(text):
    app, opened = _zone_app("4146")
    await _send(app, 1, text)
    assert app._setup_aggregator.get(ZONE_CHAT) is None and opened == []


# --- AC 12 (D4) and AC 13 (D5) -------------------------------------------------------------------


async def test_same_side_opener_after_a_fill_waits_for_its_entry():
    app, opened, setup = await _filled_sell()
    await _send(app, 4, "sell gold now")
    for price in ("4140", "4147", "4160"):
        await app._maybe_fire_setup(setup, Decimal(price))
    assert len(opened) == 1  # no market re-entry
    assert app._setup_aggregator.get(ZONE_CHAT) is setup and setup.await_entry
    await _send(app, 5, "4146")
    await app._maybe_fire_setup(setup, Decimal("4147"))
    assert opened[1] == (Decimal("4165"), Decimal("4115"), True)
    assert len(opened) == 2 and setup.positions[0].mt5_ticket == 1


async def test_opposite_opener_replaces_a_pending_setup():
    app, opened = _zone_app("4146.5")
    await _send(app, 1, "sell gold 4150")
    sell = app._setup_aggregator.get(ZONE_CHAT)
    await _send(app, 2, "buy gold now")
    buy = app._setup_aggregator.get(ZONE_CHAT)
    assert buy is not sell and buy.action == ParsedAction.BUY
    assert buy.await_entry and buy.low is None
    await app._maybe_fire_setup(buy, Decimal("4146.5"))
    assert opened == []
    await _send(app, 3, "4146")
    await app._maybe_fire_setup(buy, Decimal("4146.5"))
    assert opened == [(Decimal("4136.5"), None, False)]  # BUY: default SL below


async def test_opposite_opener_after_a_fill_warns_and_never_reenters_the_old_side(caplog):
    app, opened, sell = await _filled_sell()
    await _send(app, 4, "buy gold now")
    buy = app._setup_aggregator.get(ZONE_CHAT)
    assert buy is not sell and buy.action == ParsedAction.BUY
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("[1]" in r.getMessage() for r in warnings)
    await app._maybe_fire_setup(buy, Decimal("4150"))
    assert len(opened) == 1


# --- AC 14: unchanged behaviour (pass before and after) ------------------------------------------


async def test_complete_single_message_still_takes_the_instant_path():
    app, opened = _zone_app("4149")
    app._use_live_price_for_sizing = AsyncMock(return_value=True)
    await _send(app, 1, "sell gold 4149\nSL 4165\nTP 4115")
    assert opened == [(Decimal("4165"), Decimal("4115"), False)]
    assert app._setup_aggregator.get(ZONE_CHAT) is None


async def test_now_with_a_price_inside_a_setup_fires_as_today():
    app, opened = _zone_app("4140")
    await _send(app, 1, "sell gold 4151-4152")
    await _send(app, 2, "you can sell now 4144")
    setup = app._setup_aggregator.get(ZONE_CHAT)
    assert setup.fire_now and not getattr(setup, "await_entry", False)
    await app._maybe_fire_setup(setup, Decimal("4140"))
    assert len(opened) == 1


async def test_no_symbol_now_after_a_fill_still_reenters_at_market():
    app, opened, setup = await _filled_sell()
    await _send(app, 4, "vends maintenant")
    assert not getattr(setup, "await_entry", False)
    await app._maybe_fire_setup(setup, Decimal("4140"))
    assert len(opened) == 2


async def test_bare_number_without_a_setup_does_nothing():
    app, opened = _zone_app("4146")
    await _send(app, 1, "4146")
    assert app._setup_aggregator.get(ZONE_CHAT) is None and opened == []
