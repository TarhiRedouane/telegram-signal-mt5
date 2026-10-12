"""Story 20.3 (F19): Telegram message ids are per chat, so signals are keyed by (chat, message)."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from src.correlation_engine.correlator import CorrelationEngine, TelegramMessage
from src.database.models import ParsedAction, ParserType, PositionStatus, SignalStatus
from src.database.repository import DatabaseManager, RepositoryFactory

CHAT_A = -100111
CHAT_B = -100222
MESSAGE_ID = 500


def _signal(chat_id: int, status: SignalStatus = SignalStatus.PENDING) -> dict:
    return {
        "telegram_message_id": MESSAGE_ID,
        "telegram_chat_id": chat_id,
        "sender": "provider",
        "timestamp": datetime.now(timezone.utc).replace(tzinfo=None),
        "raw_text": "SELL GOLD 4150 SL 4160 TP 4130",
        "parsed_action": ParsedAction.SELL,
        "symbol": "GOLD",
        "entry_price": 4150.0,
        "stop_loss": 4160.0,
        "take_profit": 4130.0,
        "confidence_score": 0.95,
        "parser_type": ParserType.REGEX,
        "status": status,
    }


@pytest.fixture
async def database():
    manager = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await manager.initialize()
    yield manager
    await manager.close()


async def test_same_message_id_in_another_chat_is_a_new_signal(database):
    signals = RepositoryFactory(database).get_signal_repository()
    a_id = await signals.save_signal(_signal(CHAT_A, SignalStatus.EXECUTED))

    b_id = await signals.save_signal(_signal(CHAT_B))

    assert b_id != a_id
    b_row = await signals.find_by_message_id(MESSAGE_ID, chat_id=CHAT_B)
    assert b_row is not None and b_row.id == b_id
    assert await signals.claim_signal_for_execution(b_id)


async def test_lookup_in_another_chat_finds_nothing(database):
    signals = RepositoryFactory(database).get_signal_repository()
    await signals.save_signal(_signal(CHAT_A))

    assert await signals.find_by_message_id(MESSAGE_ID, chat_id=CHAT_B) is None
    assert await signals.find_by_message_id(MESSAGE_ID, chat_id=CHAT_A) is not None


async def test_same_chat_same_message_stays_idempotent(database):
    signals = RepositoryFactory(database).get_signal_repository()
    first = await signals.save_signal(_signal(CHAT_A))

    assert await signals.save_signal(_signal(CHAT_A)) == first
    assert await signals.count() == 1


async def test_reply_in_chat_b_never_resolves_chat_a_position(database):
    repositories = RepositoryFactory(database)
    a_id = await repositories.get_signal_repository().save_signal(
        _signal(CHAT_A, SignalStatus.EXECUTED)
    )
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await repositories.get_position_repository().create(
        signal_id=a_id,
        mt5_ticket=9001,
        open_time=now - timedelta(minutes=1),
        open_price=4150.0,
        volume=0.01,
        status=PositionStatus.OPEN,
    )
    engine = CorrelationEngine(database)
    engine.reply_tracer = AsyncMock()
    engine.reply_tracer.trace_reply_chain = AsyncMock(return_value=MESSAGE_ID)
    engine.time_matcher.find_matches = AsyncMock(return_value=[])
    reply = TelegramMessage(
        telegram_message_id=MESSAGE_ID + 1,
        telegram_chat_id=CHAT_B,
        sender="provider",
        timestamp=now,
        raw_text="BE",
        reply_to_message_id=MESSAGE_ID,
    )

    assert await engine.correlate_message(reply) is None
    assert await engine.get_position_by_message_id(MESSAGE_ID, chat_id=CHAT_B) is None
    assert await engine.get_position_by_message_id(MESSAGE_ID, chat_id=CHAT_A) is not None


async def _signal_indexes(manager: DatabaseManager) -> dict[str, str]:
    async with manager.engine.connect() as conn:
        rows = await conn.execute(
            text("SELECT name, sql FROM sqlite_master WHERE type='index' AND tbl_name='signals'")
        )
        return {name: sql or "" for name, sql in rows}


async def test_startup_upgrades_old_global_unique_index_and_keeps_rows(tmp_path, caplog):
    url = f"sqlite+aiosqlite:///{tmp_path / 'old.db'}"
    old = DatabaseManager(url)
    await old.initialize()
    async with old.engine.begin() as conn:  # rebuild the pre-20.3 schema
        await conn.execute(text("DROP INDEX IF EXISTS uq_signals_chat_message"))
        await conn.execute(text("DROP INDEX ix_signals_telegram_message_id"))
        await conn.execute(
            text(
                "CREATE UNIQUE INDEX ix_signals_telegram_message_id "
                "ON signals (telegram_message_id)"
            )
        )
    a_id = await RepositoryFactory(old).get_signal_repository().save_signal(_signal(CHAT_A))
    await old.close()

    upgraded = DatabaseManager(url)
    with caplog.at_level("INFO"):
        await upgraded.initialize()
    try:
        indexes = await _signal_indexes(upgraded)
        assert "UNIQUE" not in indexes["ix_signals_telegram_message_id"].upper()
        assert "UNIQUE" in indexes["uq_signals_chat_message"].upper()
        assert "index upgraded" in caplog.text
        signals = RepositoryFactory(upgraded).get_signal_repository()
        assert (await signals.find_by_message_id(MESSAGE_ID, chat_id=CHAT_A)).id == a_id
        assert await signals.save_signal(_signal(CHAT_B)) != a_id
    finally:
        await upgraded.close()

    caplog.clear()
    again = DatabaseManager(url)  # idempotent: second start changes nothing
    with caplog.at_level("INFO"):
        await again.initialize()
    await again.close()
    assert "index upgraded" not in caplog.text
