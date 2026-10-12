"""Story 20.1: a new trade is never time-matched as a follow-up of an open position (review F20)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.correlation_engine.correlator import CorrelationEngine, TelegramMessage
from src.correlation_engine.time_matcher import TimeMatcher
from src.signal_parser.regex_parser import RegexParser

pytestmark = pytest.mark.unit

CHAT = -100123
NEW_TRADES = [
    "JE VENDS GOLD 4150 SL 4160 TP 4130",
    "J'ACHÈTE GOLD 4150 SL 4140 TP 4170",
    "je vendrai gold 4150 SL 4160 TP 4130",
    "ACHAT GOLD 4150 SL 4140 TP 4170",
    "SELL GOLD 4150 SL 4160 TP 4130",  # English path, already excluded before 20.1: keep it that way
]
FOLLOWUPS = ["BE", "SL 4165", "passe en break even", "on ferme", "TP1 touché"]


def _open_sell_matcher(now: datetime) -> TimeMatcher:
    """TimeMatcher over stub repos: one open SELL signalled 3 min before `now` in CHAT."""
    signal = SimpleNamespace(
        id=1,
        telegram_message_id=500,
        telegram_chat_id=CHAT,
        symbol="GOLD",
        timestamp=now - timedelta(minutes=3),
        raw_text="SELL GOLD 4160 SL 4170 TP 4140",
        parsed_action="SELL",
    )
    position = SimpleNamespace(id=7, signal_id=1, is_open=True, status="OPEN", mt5_ticket=9001)
    signal_repo = MagicMock(
        get_recent_signals=AsyncMock(return_value=[signal]),
        get_by_id=AsyncMock(return_value=signal),
    )
    position_repo = MagicMock(get_positions_by_signal=AsyncMock(return_value=[position]))
    return TimeMatcher(signal_repo, position_repo)


def _message(text: str, now: datetime) -> TelegramMessage:
    return TelegramMessage(
        telegram_message_id=501,
        telegram_chat_id=CHAT,
        sender="s",
        timestamp=now,
        raw_text=text,
    )


@pytest.mark.parametrize("text", NEW_TRADES)
async def test_new_trade_is_not_time_matched(text):
    now = datetime.now(UTC).replace(tzinfo=None)
    assert await _open_sell_matcher(now).find_matches(_message(text, now), window_minutes=5) == []


@pytest.mark.parametrize("text", FOLLOWUPS)
async def test_followup_still_time_matched(text):
    now = datetime.now(UTC).replace(tzinfo=None)
    matches = await _open_sell_matcher(now).find_matches(_message(text, now), window_minutes=5)
    assert [m["position"].id for m in matches] == [7]


async def test_router_executes_french_signal_instead_of_correlating_it():
    from main import ApplicationManager

    now = datetime.now(UTC).replace(tzinfo=None)
    engine = CorrelationEngine(MagicMock(), None)  # real engine, no reply tracer
    engine.time_matcher = _open_sell_matcher(now)
    engine.correlation_repo = MagicMock(link_messages=AsyncMock(return_value=1))

    app = ApplicationManager()
    app.regex_parser = RegexParser()
    app._correlation_engine = engine
    app._validate_live_price = AsyncMock(return_value=True)
    app._handle_parsed_signal = AsyncMock()
    app._handle_correlated_message = AsyncMock()

    await app._on_telegram_message(
        {
            "telegram_chat_id": CHAT,
            "telegram_message_id": 501,
            "sender": "s",
            "timestamp": now,
            "raw_text": NEW_TRADES[0],
        }
    )

    # The real matcher was consulted (not bypassed), and declined the new trade
    assert engine.time_matcher._match_attempts == 1
    app._handle_correlated_message.assert_not_awaited()
    app._handle_parsed_signal.assert_awaited_once()
