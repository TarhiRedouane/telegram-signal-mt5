"""Multi-message setup aggregation + regression guard for complete single-message signals."""

from datetime import datetime, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.correlation_engine.setup_aggregator import (
    SetupAggregator,
    extract_followup,
    extract_zone_hint,
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
    # "clôture m30 au dessus de 4162": exit on an M30 close above 4162, protective hard SL 3 beyond
    assert setup.close_invalidation == (Decimal("4162"), 30)
    assert setup.sl == Decimal("4165.0")
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


def test_three_step_setup_regex_only_no_llm_needed():
    from src.correlation_engine.setup_aggregator import has_action_word

    assert RegexParser().parse("sell GOld") is None  # no price: nothing started
    _, setup = _run(["Sell gold 4100 4105", "je vise les 4050", "invalidation autour 4115"])
    assert (setup.low, setup.high, setup.tp, setup.sl) == (
        Decimal("4100"), Decimal("4105"), Decimal("4050"), Decimal("4115")
    )
    # follow-ups are fully regex-resolved -> LLM fallback (and its context) never involved
    assert not extract_followup("je vise les 4050").empty
    assert not extract_followup("invalidation autour 4115").empty
    # new-trade LLM guard: no direction word -> no trade; French verbs still count
    assert not has_action_word("**PE SNIPER 4180,95 GOLD SCALP**")
    assert not has_action_word("on regarde ça vendredi")
    assert all(has_action_word(t) for t in ("je vais vendre l'or", "acheter gold", "j'achète", "je vends"))


def test_llm_user_message_carries_setup_context():
    from src.signal_parser.llm_parser import LLMParser

    parser = LLMParser()
    prompt = parser._prepare_prompt("Je vise les 4050", "Setup en attente: SELL GOLD entrée 4100-4105")
    user = prompt.removeprefix(parser._prompt_template).strip()
    assert user.startswith("Contexte:\nSetup en attente: SELL GOLD entrée 4100-4105")
    assert user.endswith("Message à analyser:\nJe vise les 4050")
    assert parser._prompt_template not in user


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("SELL GOLD 4149\nSL 4165\nTP 4115 | 4100 | 4080", ["4115", "4100", "4080"]),
        ("SELL GOLD 4149\nSL 4165\nTP1 4115\nTP2 4100\nTP3 4080", ["4115", "4100", "4080"]),
        ("SELL GOLD 4149\nSL 4165\nTP1: 4115\nTP2: 4100", ["4115", "4100"]),
        ("SELL GOLD 4149\nSL 4165\nTP 4115\nTP 4100", ["4115", "4100"]),
        ("SELL GOLD 4149 TP 4115 SL 4165", ["4115"]),  # SL on the same line is not a TP
        ("sell gold 4149\nSL 4165\nTP 4115", ["4115"]),
    ],
)
def test_main_parse_collects_all_tps(text, expected):
    signal = _parse(text)
    assert signal.take_profits == [Decimal(t) for t in expected]
    assert signal.take_profit == Decimal(expected[0])


def test_followup_tp_list_slots_and_final_target():
    _, setup = _run(["SELL GOLD 4190 4195", "TP1 4180\nTP2 4170"])
    assert setup.tps == [Decimal("4180"), Decimal("4170")]
    setup.apply(extract_followup("TP 3 : 4160"), "TP 3 : 4160", NOW)
    assert setup.tps == [Decimal("4180"), Decimal("4170"), Decimal("4160")]
    setup.apply(extract_followup("je vise les 4150"), "je vise les 4150", NOW)
    assert setup.tps[-1] == Decimal("4150") and len(setup.tps) == 4
    setup.apply(extract_followup("je vise les 4175"), "je vise les 4175", NOW)  # nearer final target
    assert setup.tps == [Decimal("4180"), Decimal("4175")]


def test_ladder_legs_spacing_and_scale_out_tps():
    _, setup = _run(["SELL GOLD 4190 4195", "TP 4180 | 4170 | 4160"])
    assert setup.legs() == [Decimal("4190"), Decimal("4192.5"), Decimal("4195")]
    assert [setup.leg_tp(i) for i in range(3)] == [Decimal("4180"), Decimal("4170"), Decimal("4160")]
    assert not setup.leg_reached(1, Decimal("4191"), Decimal("1"))
    assert setup.leg_reached(1, Decimal("4191.5"), Decimal("1"))

    _, many = _run(["SELL GOLD 4190 4196", "TP 4180 | 4170 | 4160 | 4150 | 4140"])
    assert len(many.legs()) == 4  # capped at 4 legs

    _, buy = _run(["BUY GOLD 4190 4195", "TP 4200 | 4210 | 4220"])
    assert buy.legs() == [Decimal("4195"), Decimal("4192.5"), Decimal("4190")]

    _, single = _run(["SELL GOLD 4190", "TP 4180 | 4170"])
    assert single.legs() == [Decimal("4190"), Decimal("4190")]

    _, no_tp = _run(["SELL GOLD 4190 4195"])
    assert len(no_tp.legs()) == 1


def test_leg_count_frozen_after_first_fill_and_reentry_is_single_order():
    _, setup = _run(["SELL GOLD 4190 4195"])
    setup.positions.append("leg1")
    setup.planned_legs = len(setup.legs())  # main freezes at first fill
    setup.apply(extract_followup("TP 4180 | 4170 | 4160"), "TP 4180 | 4170 | 4160", NOW)
    assert len(setup.legs()) == 1 and setup.next_leg() is None  # later TPs don't add risk
    setup.reopen()
    assert setup.next_leg() == 0 and len(setup.legs()) == 1


def test_break_even_guard_only_accepts_profit_locking_sl():
    _, setup = _run(["SELL GOLD 4190 4195"])
    assert setup.locks_profit(Decimal("4185"), Decimal("4190"))
    assert setup.locks_profit(Decimal("4190"), Decimal("4190"))
    assert not setup.locks_profit(Decimal("4200"), Decimal("4190"))


@pytest.mark.asyncio
async def test_main_ladder_fills_legs_progressively_then_tp1_moves_rest_to_break_even():
    from types import SimpleNamespace

    from main import ApplicationManager

    app = ApplicationManager()
    app._validate_live_price = AsyncMock(return_value=True)
    app._order_volume = AsyncMock(return_value=0.1)
    app._apply_position_levels = AsyncMock()
    opened = []

    async def fake_open(signal, volume=None, allow_reuse=False):
        opened.append((signal.entry_price, signal.stop_loss, signal.take_profit, allow_reuse))
        return SimpleNamespace(mt5_ticket=len(opened), open_price=float(signal.entry_price))

    app._handle_parsed_signal = fake_open
    _, setup = _run(["SELL GOLD 4190 4195\nSL 4200\nTP 4180 | 4170 | 4160"])

    await app._maybe_fire_setup(setup, Decimal("4190.3"))  # leg 1 (complete -> live check)
    await app._maybe_fire_setup(setup, Decimal("4191"))    # leg 2 level 4192.5 not reached
    assert len(opened) == 1
    await app._maybe_fire_setup(setup, Decimal("4195.2"))  # jump: legs 2 and 3 due together
    assert [o[2] for o in opened] == [Decimal("4180"), Decimal("4170"), Decimal("4160")]
    assert [o[3] for o in opened] == [False, True, True]  # extra legs reuse the executed signal
    assert not setup.armed and setup.planned_legs == 3

    app.position_manager = SimpleNamespace(get_close_reason=AsyncMock(side_effect=["tp", None, None]))
    assert await app._track_setup_legs(setup) is False
    assert setup.be_done
    be_calls = [c.args for c in app._apply_position_levels.await_args_list]
    assert be_calls == [(setup.positions[1], Decimal("4195.2"), None), (setup.positions[2], Decimal("4195.2"), None)]


ZONE_POST = """Première BUY ZONE autour de 4174–4178

➡️ Deuxième BUY ZONE autour de 4158–4162 pour un potentiel renfort si le marché vient la chercher

🏓 Au-dessus on garde 4188 → 4195 → 4219 comme zones de réaction / objectifs potentiels

Pas de renfort automatique on attend la réaction du prix dans la zone💙"""


def test_zone_post_parsed_and_normal_signals_are_not_zones():
    from src.correlation_engine.setup_aggregator import extract_zone_hint

    symbol, zones, targets = extract_zone_hint(ZONE_POST)
    assert symbol == "GOLD"
    assert zones == [
        (ParsedAction.BUY, Decimal("4174"), Decimal("4178")),
        (ParsedAction.BUY, Decimal("4158"), Decimal("4162")),
    ]
    assert targets == [Decimal("4188"), Decimal("4195"), Decimal("4219")]
    for text in ["BUY GOLD 4174", "sell gold 4149\nSL 4165\nTP 4115", "SELL GOLD 4190 4195", "on est dans la zone 4174"]:
        assert extract_zone_hint(text) is None


@pytest.mark.asyncio
async def test_main_routes_zone_post_to_watch_and_regular_signals_unchanged():
    from main import ApplicationManager

    app = ApplicationManager()
    app.regex_parser = RegexParser()
    app._validate_live_price = AsyncMock(return_value=True)
    app._handle_parsed_signal = AsyncMock()
    app._start_setup = MagicMock()
    app._start_zone_watch = MagicMock()

    meta = {"telegram_chat_id": 1, "sender": "s", "timestamp": NOW}
    await app._on_telegram_message({**meta, "telegram_message_id": 1, "raw_text": ZONE_POST})
    app._start_zone_watch.assert_called_once()
    app._handle_parsed_signal.assert_not_awaited()
    app._start_setup.assert_not_called()

    await app._on_telegram_message({**meta, "telegram_message_id": 2, "raw_text": "sell gold 4149\nSL 4165\nTP 4115"})
    app._handle_parsed_signal.assert_awaited_once()  # instant path, as before
    await app._on_telegram_message({**meta, "telegram_message_id": 3, "raw_text": "SELL GOLD 4190 4195"})
    app._start_setup.assert_called_once()  # setup path, as before
    assert app._start_zone_watch.call_count == 1


@pytest.mark.asyncio
async def test_confirmed_zone_becomes_complete_setup_with_sweep_sl_and_post_targets():
    from main import ApplicationManager
    from src.correlation_engine.rejection_detector import Phase, RejectionState
    from src.correlation_engine.setup_aggregator import ZoneEntry, ZoneWatch

    app = ApplicationManager()
    app._watch_setup = AsyncMock()
    state = RejectionState(sell=False, low=Decimal("4174"), high=Decimal("4178"),
                           phase=Phase.CONFIRMED, extreme=Decimal("4172.2"))
    zone = ZoneEntry(ParsedAction.BUY, Decimal("4174"), Decimal("4178"), state)
    watch = ZoneWatch(1, 7, "s", NOW, "GOLD", [zone], [Decimal("4219"), Decimal("4188"), Decimal("4170")], NOW)
    await app._on_zone_confirmed(watch, 0, zone, Decimal("0.5"))
    setup = app._setup_aggregator.get(1)
    assert setup.action == ParsedAction.BUY and setup.message_id == 7 and setup.fire_now
    assert setup.sl == Decimal("4171.7")
    assert setup.tps == [Decimal("4188"), Decimal("4219")]  # wrong-side 4170 dropped, nearest first
    assert setup.is_complete and watch.fills == 1


@pytest.mark.asyncio
async def test_zone_followups_update_watch_from_live_test_messages():
    from main import ApplicationManager

    app = ApplicationManager()
    app.regex_parser = RegexParser()
    app._watch_zone = AsyncMock()
    app._validate_live_price = AsyncMock(return_value=True)
    app._handle_parsed_signal = AsyncMock()
    app.llm_parser = MagicMock(parse_with_llm=AsyncMock(return_value=None))

    meta = {"telegram_chat_id": -1004311007519, "sender": "s", "timestamp": NOW}
    await app._on_telegram_message({**meta, "telegram_message_id": 57, "raw_text": "gold buy zone 4124-4128"})
    watch = app._setup_aggregator.get_zone(-1004311007519)
    assert watch is not None and watch.targets == []

    await app._on_telegram_message({**meta, "telegram_message_id": 58, "raw_text": "targets are 4145, 4160 ,4176"})
    assert watch.targets == [Decimal("4145"), Decimal("4160"), Decimal("4176")]
    await app._on_telegram_message({**meta, "telegram_message_id": 59, "raw_text": "invalidation 4130"})
    assert watch.sl is None  # above a BUY zone: wrong side
    await app._on_telegram_message({**meta, "telegram_message_id": 60, "raw_text": "invalidation sous 4118"})
    assert watch.sl == Decimal("4118")
    app.llm_parser.parse_with_llm.assert_not_awaited()
    app._validate_live_price.assert_not_awaited()
    app._handle_parsed_signal.assert_not_awaited()

    await app._on_telegram_message({**meta, "telegram_message_id": 61, "raw_text": "on annule la zone"})
    assert app._setup_aggregator.get_zone(-1004311007519) is None


@pytest.mark.asyncio
async def test_unlinked_modify_is_ignored_without_live_price_check():
    from main import ApplicationManager
    from src.signal_parser import ParsedSignal

    app = ApplicationManager()
    app.regex_parser = RegexParser()
    app._validate_live_price = AsyncMock(return_value=False)
    modify = ParsedSignal(parsed_action=ParsedAction.MODIFY, symbol="GOLD", take_profit=Decimal("4145"))
    app.llm_parser = MagicMock(parse_with_llm=AsyncMock(return_value=modify))
    await app._on_telegram_message(
        {"telegram_chat_id": 5, "telegram_message_id": 1, "sender": "s", "timestamp": NOW, "raw_text": "targets are 4145"}
    )
    app._validate_live_price.assert_not_awaited()


@pytest.mark.asyncio
async def test_confirmed_zone_uses_author_invalidation_as_sl():
    from main import ApplicationManager
    from src.correlation_engine.rejection_detector import Phase, RejectionState
    from src.correlation_engine.setup_aggregator import ZoneEntry, ZoneWatch

    app = ApplicationManager()
    app._watch_setup = AsyncMock()
    state = RejectionState(sell=False, low=Decimal("4124"), high=Decimal("4128"),
                           phase=Phase.CONFIRMED, extreme=Decimal("4122.4"))
    zone = ZoneEntry(ParsedAction.BUY, Decimal("4124"), Decimal("4128"), state)
    watch = ZoneWatch(1, 57, "s", NOW, "GOLD", [zone], [Decimal("4145")], NOW, sl=Decimal("4118"))
    await app._on_zone_confirmed(watch, 0, zone, Decimal("0.5"))
    assert app._setup_aggregator.get(1).sl == Decimal("4118")


def _zone_app(price: str):
    from types import SimpleNamespace

    from main import ApplicationManager

    app = ApplicationManager()
    app.regex_parser = RegexParser()
    app._watch_zone = AsyncMock()
    app._watch_setup = AsyncMock()
    app._validate_live_price = AsyncMock(return_value=True)
    app._order_volume = AsyncMock(return_value=0.1)
    app._apply_position_levels = AsyncMock()
    app.llm_parser = MagicMock(parse_with_llm=AsyncMock(return_value=None))
    app.mt5_connection_manager = SimpleNamespace(get_current_price=AsyncMock(return_value=Decimal(price)))
    opened = []

    async def fake_open(signal, volume=None, allow_reuse=False):
        opened.append((signal.stop_loss, signal.take_profit, allow_reuse))
        return SimpleNamespace(mt5_ticket=len(opened), open_price=float(signal.entry_price))

    app._handle_parsed_signal = fake_open
    return app, opened


ZONE_CHAT = -1004311007519


async def _send(app, message_id: int, text: str):
    meta = {"telegram_chat_id": ZONE_CHAT, "sender": "s", "timestamp": NOW}
    await app._on_telegram_message({**meta, "telegram_message_id": message_id, "raw_text": text})


@pytest.mark.asyncio
async def test_live_sequence_buy_now_takes_one_early_leg_and_zone_confirmation_adds_the_rest():
    from src.correlation_engine.rejection_detector import Phase

    app, opened = _zone_app("4132.74")
    await _send(app, 59, "gold buy zone 4124-4130")
    await _send(app, 60, "targets are 4145, 4160 ,4176")
    await _send(app, 61, "invalidation 30 mn candle close below 4121")
    watch = app._setup_aggregator.get_zone(ZONE_CHAT)
    assert (watch.sl, watch.close_tf) == (Decimal("4121"), 30)

    await _send(app, 62, "gold buy now")
    setup = app._setup_aggregator.get(ZONE_CHAT)
    assert watch.linked_setup is setup and app._setup_aggregator.get_zone(ZONE_CHAT) is watch  # zone kept
    assert setup.planned_legs == 3 and setup.hold_at == 1
    assert setup.close_invalidation == (Decimal("4121"), 30) and setup.sl == Decimal("4118.0")  # protective
    await app._maybe_fire_setup(setup, Decimal("4132.74"))
    assert opened == [(Decimal("4118.0"), Decimal("4145"), False)]  # ONE leg, TP1
    assert not setup.armed

    await _send(app, 63, "gold buy now")  # second "now" doesn't add another early leg
    assert app._setup_aggregator.get(ZONE_CHAT) is setup

    zone = watch.zones[0]
    zone.state.phase, zone.state.extreme = Phase.CONFIRMED, Decimal("4123.1")
    await app._on_zone_confirmed(watch, 0, zone, Decimal("0.5"))
    assert setup.armed and setup.hold_at is None
    await app._maybe_fire_setup(setup, Decimal("4129.5"))
    assert [o[1] for o in opened] == [Decimal("4145"), Decimal("4160"), Decimal("4176")]
    assert [o[2] for o in opened] == [False, True, True]
    assert all(o[0] == Decimal("4118.0") for o in opened)  # author's invalidation kept, not sweep SL


@pytest.mark.asyncio
async def test_buy_now_with_single_target_gives_one_extra_zone_leg():
    app, _ = _zone_app("4131")
    await _send(app, 1, "gold buy zone 4124-4130")
    await _send(app, 2, "TP 4145")
    await _send(app, 3, "buy now")
    setup = app._setup_aggregator.get(ZONE_CHAT)
    assert setup.planned_legs == 2 and setup.tps == [Decimal("4145")]
    assert setup.sl is None  # no invalidation yet: early leg uses the default SL, sweep SL comes later


@pytest.mark.asyncio
async def test_buy_now_far_from_zone_or_opposite_side_is_not_linked():
    app, _ = _zone_app("4150")  # 20 above the zone, max 10
    await _send(app, 1, "gold buy zone 4124-4130")
    await _send(app, 2, "gold buy now")
    assert app._setup_aggregator.get(ZONE_CHAT) is None
    app.mt5_connection_manager.get_current_price = AsyncMock(return_value=Decimal("4131"))
    await _send(app, 3, "gold sell now")
    assert app._setup_aggregator.get(ZONE_CHAT) is None
    assert app._setup_aggregator.get_zone(ZONE_CHAT).linked_setup is None


def test_fixed_invalidation_stays_a_hard_sl():
    followup = extract_followup("invalidation 4121")
    assert followup.sl == Decimal("4121") and followup.sl_close_tf is None
    _, setup = _run(["SELL GOLD 4190 4195", "SL 4200"])
    assert setup.sl == Decimal("4200") and setup.close_invalidation is None


@pytest.mark.asyncio
async def test_m30_close_beyond_invalidation_closes_all_legs_but_wick_does_not():
    from types import SimpleNamespace

    from main import ApplicationManager

    app = ApplicationManager()
    _, setup = _run(["BUY GOLD 4124 4130", "invalidation clôture M30 sous 4121"])
    assert setup.close_invalidation == (Decimal("4121"), 30)
    setup.positions = [SimpleNamespace(mt5_ticket=1), SimpleNamespace(mt5_ticket=2)]
    app._setup_aggregator.adopt(setup)
    app.position_manager = SimpleNamespace(close_position_full=AsyncMock(return_value=SimpleNamespace(success=True)))
    bar = lambda t, low, close: (t, Decimal("4125"), Decimal("4126"), Decimal(low), Decimal(close), 10)  # noqa: E731
    rates = [bar(0, "4123", "4124"), bar(1800, "4122", "4123")]
    app.mt5_connection_manager = SimpleNamespace(get_closed_rates=AsyncMock(return_value=rates))
    assert await app._check_close_invalidation(setup) is False  # baseline
    rates.append(bar(3600, "4119.5", "4122"))  # wick below 4121, close above
    assert await app._check_close_invalidation(setup) is False
    rates.append(bar(5400, "4119", "4120.4"))  # close below 4121
    assert await app._check_close_invalidation(setup) is True
    assert app.position_manager.close_position_full.await_count == 2
    assert app._setup_aggregator.get(setup.chat_id) is None


@pytest.mark.asyncio
async def test_blind_first_leg_takes_half_risk_and_late_targets_split_the_rest():
    from types import SimpleNamespace

    from main import ApplicationManager

    app = ApplicationManager()
    app._order_volume = AsyncMock(return_value=0.1)
    app._apply_position_levels = AsyncMock()
    opened = []

    async def fake_open(signal, volume=None, allow_reuse=False):
        opened.append(signal.take_profit)
        return SimpleNamespace(mt5_ticket=len(opened), open_price=float(signal.entry_price))

    app._handle_parsed_signal = fake_open
    _, setup = _run(["buy gold 4136 4139"])  # live 18:04: no SL, no TP

    await app._maybe_fire_setup(setup, Decimal("4139.85"))
    await app._maybe_fire_setup(setup, Decimal("4136"))  # no targets yet: no extra leg
    assert opened == [None] and setup.blind and setup.armed

    setup.apply(extract_followup("targets are 4155 4165 4176 4190"), "targets", NOW)
    await app._maybe_fire_setup(setup, Decimal("4136"))
    assert opened == [None, Decimal("4165"), Decimal("4176"), Decimal("4190")]
    assert [c.args[1] for c in app._order_volume.await_args_list] == [2, 6, 6, 6]  # 1/2 + 3 x 1/6
    assert setup.planned_legs == 4 and setup.leg_tp(0) == Decimal("4155")


@pytest.mark.asyncio
async def test_complete_four_tp_signal_still_opens_four_equal_legs():
    from types import SimpleNamespace

    from main import ApplicationManager

    app = ApplicationManager()
    app._validate_live_price = AsyncMock(return_value=True)
    app._order_volume = AsyncMock(return_value=0.1)
    app._apply_position_levels = AsyncMock()
    app._handle_parsed_signal = AsyncMock(side_effect=lambda *_, **__: SimpleNamespace(mt5_ticket=1, open_price=1.0))
    _, setup = _run(["BUY GOLD 4136 4139\nSL 4128\nTP 4155 | 4165 | 4176 | 4190"])

    await app._maybe_fire_setup(setup, Decimal("4139"))
    await app._maybe_fire_setup(setup, Decimal("4136"))
    assert [c.args[1] for c in app._order_volume.await_args_list] == [4, 4, 4, 4]
    assert not setup.blind and setup.planned_legs == 4


SIGNAL_1671 = (
    "**SELL  GOLD →  4142 - 4145 **\n\n**TP 4137.7 zone prise de profit BE **\n**TP 4133.9 zone prise de profit **\n"
    "**TP 4124.8 zone prise de profit **\n**TP 4114.4 zone prise de profit **\n\n"
    "**L’intégralité des TP doivent représenter maximum 5% **\n\n**Objectif journalier retour sur **\n\n"
    "**❌**** __SL / INVALIDATION  4145 - 4150 __**\n\n**__➡️__****__ Validé uniquement par une clôture M30__**\n\n"
    "**👀**** Attention aux mèches **"
)
SIGNAL_1698 = (
    "**SELL  GOLD →  4153 - 4156 **\n\n**TP 4148 zone prise de profit BE **\n**TP 4142 zone prise de profit **\n"
    "**TP 4132.6  zone prise de profit **\n**TP 4114.4 zone prise de profit **\n\n"
    "**❌**** __SL / INVALIDATION 4158 4168  __****__➡️__****__ Validé uniquement par une clôture M30__**"
)
SIGNAL_1719 = (
    "**🚨**** SELL    XAUUSD**\n\n**📉**** ENTRY  4177 **\n\n**✖️**** SL:4187**\n\n**❌**** TP  4167**\n"
    "**❌**** TP 4153**\n**❌**** TP 4143**\n**❌**** OBJECTIF 4110 - 4120**"
)


def test_live_sl_range_with_m30_close_checks_near_edge_and_hard_sl_far_edge():
    _, setup = _run([SIGNAL_1671])
    assert setup.close_invalidation == (Decimal("4145"), 30) and setup.sl == Decimal("4150")
    assert setup.tps == [Decimal("4137.7"), Decimal("4133.9"), Decimal("4124.8"), Decimal("4114.4")]
    _, setup = _run([SIGNAL_1698])
    assert setup.close_invalidation == (Decimal("4158"), 30) and setup.sl == Decimal("4168")


def test_sl_range_without_candle_close_is_a_hard_sl_at_far_edge_and_single_sl_unchanged():
    _, setup = _run(["SELL GOLD 4142 - 4145\nSL 4147 - 4150\nTP 4130"])
    assert setup.sl == Decimal("4150") and setup.close_invalidation is None
    _, setup = _run(["SELL GOLD 4142 - 4145\nSL 4150 - 2017\nTP 4130"])  # typo far edge ignored
    assert setup.sl == Decimal("4150")
    _, setup = _run(["BUY GOLD 4120\nSL 4110\nTP 4130 4140"])
    assert setup.sl == Decimal("4110") and setup.close_invalidation is None


def test_objectif_range_becomes_last_tp_nearest_edge():
    signal = _parse(SIGNAL_1719)
    assert signal.stop_loss == Decimal("4187")
    assert signal.take_profits == [Decimal("4167"), Decimal("4153"), Decimal("4143"), Decimal("4120")]


def test_potential_sell_is_a_zone_watch_not_an_order():
    hint = extract_zone_hint("**SELL POTENTIELLE 4129.36 ****🔝**")
    assert hint is not None
    _, zones, targets = hint
    assert zones == [(ParsedAction.SELL, Decimal("4129.36"), Decimal("4129.36"))] and targets == []


@pytest.mark.parametrize("text", ["Clôture dns 5", "clôture dans 5 min", "Attendez la clôture M30", "__clôture H1__"])
def test_candle_close_remarks_are_not_trade_closes(text):
    from src.signal_parser.patterns import is_candle_close_remark

    assert is_candle_close_remark(text)


@pytest.mark.parametrize("text", ["Je clôture", "Clôture de la position", "clôture tout maintenant"])
def test_real_close_orders_still_close(text):
    from src.signal_parser.patterns import is_candle_close_remark

    assert not is_candle_close_remark(text)


@pytest.mark.asyncio
async def test_main_complete_signal_with_candle_close_sl_goes_through_setup():
    from main import ApplicationManager

    app = ApplicationManager()
    app.regex_parser = RegexParser()
    app._validate_live_price = AsyncMock(return_value=True)
    app._handle_parsed_signal = AsyncMock()
    app._start_setup = MagicMock()
    meta = {"telegram_chat_id": 1, "telegram_message_id": 5, "sender": "s", "timestamp": NOW}
    await app._on_telegram_message({**meta, "raw_text": "SELL GOLD 4149\nSL 4165 clôture M30\nTP 4115"})
    app._start_setup.assert_called_once()
    app._handle_parsed_signal.assert_not_awaited()
