"""Multi-message setup aggregation + regression guard for complete single-message signals."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.correlation_engine.setup_aggregator import (
    SetupAggregator,
    extract_followup,
    looks_like_new_trade,
)
from src.signal_parser import ParsedAction
from src.signal_parser.regex_parser import RegexParser

pytestmark = pytest.mark.unit

NOW = datetime(2026, 9, 28, 13, 0, 0)
EXAMPLE = [
    "SELL GOLD 4147 - 4153",
    "Je vous préviens juste je suis dans les aire si ça prend du temps ou j’ai un décalage désoler 😒",
    "Es je prévient je rentre très bas mes alors vraiment très bas",
    "Je vise les 4110",
    "Pour l’invalidation du setup ça serais une clôture m30 au dessus de 4162",
    "entrer a 4146",
]


def _parse(text: str, chat_id: int = 1, message_id: int = 100):
    signal = RegexParser().parse(text)
    assert signal is not None
    signal.telegram_chat_id = chat_id
    signal.telegram_message_id = message_id
    signal.timestamp = NOW
    return signal


def _run(messages: list[str]):
    agg = SetupAggregator()
    setup, _ = agg.start(_parse(messages[0]), NOW)
    for i, text in enumerate(messages[1:], start=1):
        assert not looks_like_new_trade(text), text
        setup.apply(extract_followup(text), text, NOW + timedelta(seconds=20 * i))
    return agg, setup


def test_complete_single_message_signal_unchanged():
    signal = _parse("sell gold 4149\nSL 4165\nTP 4115113:14")
    assert signal.parsed_action == ParsedAction.SELL
    assert signal.symbol == "GOLD"
    assert signal.entry_price == Decimal("4149")
    assert signal.stop_loss == Decimal("4165")
    assert signal.take_profit == Decimal("4115")
    assert signal.entry_price_high is None  # complete -> main takes the instant path


def test_split_complete_signal_becomes_complete_after_last_message():
    _, setup = _run(["sell gold 4149", "SL 4165", "TP 4115"])
    assert setup.is_complete
    assert (setup.low, setup.sl, setup.tp) == (Decimal("4149"), Decimal("4165"), Decimal("4115"))


def test_example_sequence_aggregates_entry_sl_tp():
    signal = _parse(EXAMPLE[0])
    assert (signal.entry_price, signal.entry_price_high) == (Decimal("4147"), Decimal("4153"))
    assert signal.stop_loss is None and signal.take_profit is None

    _, setup = _run(EXAMPLE)
    assert setup.action == ParsedAction.SELL
    assert (setup.low, setup.high) == (Decimal("4146"), Decimal("4146"))
    assert setup.sl == Decimal("4162")
    assert setup.tp == Decimal("4110")
    assert len(setup.raw_texts) == 4  # setup + TP + SL + entry; noise not recorded


def test_live_log_sequence_sell_now_then_levels():
    signal = _parse("sell gold 4151-4152")
    assert (signal.entry_price, signal.entry_price_high) == (Decimal("4151"), Decimal("4152"))

    _, setup = _run(["sell gold 4151-4152", "you can sell now 4144", "sl 4150", "TP 4135", "TP 2 : 4128"])
    assert setup.fire_now
    assert (setup.low, setup.high) == (Decimal("4144"), Decimal("4144"))
    assert setup.sl == Decimal("4150")
    assert setup.tp == Decimal("4135")  # TP2 does not replace TP1
    assert setup.fire_levels(Decimal("4146.65")) == (Decimal("4150"), Decimal("4135"))


def test_same_side_signal_rearms_setup_and_keeps_positions():
    agg, setup = _run(["sell gold 4141-4144", "sell now again"])
    assert setup.fire_now
    setup.positions.append("pos5")  # watcher fill
    setup.armed = setup.fire_now = False

    again, changes = agg.start(_parse("sell gold now 4148", message_id=101), NOW)
    assert again is setup and changes == ["entry"]
    assert setup.armed and setup.fire_now and setup.message_id == 101
    assert (setup.low, setup.high) == (Decimal("4148"), Decimal("4148"))
    setup.positions.append("pos6")

    for text in ["Sl : 4158", "Sl : 4158 for both", "TP : 4128"]:
        setup.apply(extract_followup(text), text, NOW)
    assert (setup.sl, setup.tp) == (Decimal("4158"), Decimal("4128"))
    assert setup.positions == ["pos5", "pos6"]  # main pushes SL/TP to every position


def test_opposite_side_signal_replaces_setup():
    agg = SetupAggregator()
    first, _ = agg.start(_parse("sell gold 4141-4144"), NOW)
    second, changes = agg.start(_parse("buy gold 4141"), NOW)
    assert second is not first and changes is None


def test_negated_now_does_not_fire():
    assert not extract_followup("pas maintenant, on attend").now
    assert extract_followup("sell now again").now


def test_noise_does_not_reset_idle_timer():
    agg = SetupAggregator()
    setup, _ = agg.start(_parse(EXAMPLE[0]), NOW)
    setup.apply(extract_followup(EXAMPLE[1]), EXAMPLE[1], NOW + timedelta(minutes=4))
    assert setup.is_expired(NOW + timedelta(minutes=5, seconds=1), 300)


@pytest.mark.parametrize(
    "text",
    ["Setup annulé", "on annule tout", "cancel this one", "oubliez le trade", "on laisse tomber", "pas de trade"],
)
def test_cancel_phrases(text):
    assert extract_followup(text).cancel


def test_wrong_side_levels_ignored():
    _, setup = _run(["SELL GOLD 4147 - 4153", "SL 4140", "TP 4160"])
    assert setup.sl is None and setup.tp is None


def test_default_sl_when_missing_and_invalidated_zone():
    _, setup = _run(["SELL GOLD 4147 - 4153", "Je vise les 4110"])
    assert setup.in_entry_zone(Decimal("4153.9"), Decimal("1"))
    assert not setup.in_entry_zone(Decimal("4154.5"), Decimal("1"))
    sl, tp = setup.fire_levels(Decimal("4150"))
    assert sl == Decimal("4160.0")  # 100 pips default for GOLD via symbol_policy
    assert tp == Decimal("4110")

    setup.sl = Decimal("4149")
    assert setup.fire_levels(Decimal("4150")) is None  # price already beyond SL


def test_new_trade_detection_uses_word_boundaries():
    assert looks_like_new_trade("BUY GOLD 4120")
    assert not looks_like_new_trade("alors on vise 4110 longtemps")


@pytest.mark.asyncio
async def test_main_routes_complete_signal_to_instant_path_and_incomplete_to_setup():
    from main import ApplicationManager

    app = ApplicationManager()
    app.regex_parser = RegexParser()
    app._validate_live_price = AsyncMock(return_value=True)
    app._handle_parsed_signal = AsyncMock()
    app._start_setup = MagicMock()

    meta = {"telegram_chat_id": 1, "sender": "s", "timestamp": NOW}
    await app._on_telegram_message({**meta, "telegram_message_id": 1, "raw_text": "sell gold 4149\nSL 4165\nTP 4115"})
    app._handle_parsed_signal.assert_awaited_once()
    app._start_setup.assert_not_called()

    await app._on_telegram_message({**meta, "telegram_message_id": 2, "raw_text": EXAMPLE[0]})
    app._start_setup.assert_called_once()
    assert app._handle_parsed_signal.await_count == 1


LIVE_1418 = """📉** SELL  →  4197 - 4200**

**🎯 TP  4190 | 4173 | 4153| 4125 **

**❌**** __SL / INVALIDATION  4203 - 2017 __**

Le corps confirme le mouvement"""


def test_component_parse_keeps_range_and_invalidation_sl():
    signal = RegexParser().parse(LIVE_1418)
    assert (signal.entry_price, signal.entry_price_high) == (Decimal("4197"), Decimal("4200"))
    assert (signal.stop_loss, signal.take_profit) == (Decimal("4203"), Decimal("4190"))


def test_space_separated_range():
    signal = _parse("SELL GOLD 4197 4200 !")
    assert (signal.entry_price, signal.entry_price_high) == (Decimal("4197"), Decimal("4200"))
    assert _parse("sell gold 4149\nSL 4165\nTP 4115").entry_price_high is None


def test_live_edit_sequence_restatement_keeps_zone_and_edit_note_is_ignored():
    from src.correlation_engine.setup_aggregator import Followup

    # #1416 edited to "SELL GOLD 4197 4200 !" starts the setup
    _, setup = _run(["SELL GOLD 4197 4200 !"])
    # #1418 restates the zone with levels: range kept, SL/TP collected
    followup = extract_followup(LIVE_1418)
    assert (followup.entry, followup.entry_high) == (Decimal("4197"), Decimal("4200"))
    assert setup.apply(followup, LIVE_1418, NOW) == ["entry", "sl", "tp"]
    assert (setup.low, setup.high, setup.sl, setup.tp) == (
        Decimal("4197"), Decimal("4200"), Decimal("4203"), Decimal("4190")
    )
    # "J'ai modifier 205 !" is a note about the edit; an LLM reading SL=205 must not move the SL
    assert setup.apply(Followup(sl=Decimal("205")), "J'ai modifier 205 !", NOW) == []
    assert setup.sl == Decimal("4203")
