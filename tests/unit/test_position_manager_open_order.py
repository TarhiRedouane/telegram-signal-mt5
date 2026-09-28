from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import ApplicationManager
from src.database.models import ParserType, SignalStatus
from src.database.repository import DatabaseManager, SignalRepository
from src.mt5_executor.position_manager import PositionManager
from src.signal_parser import ParsedAction


@pytest.mark.asyncio
async def test_open_market_order_submits_mt5_buy_request():
    connection = MagicMock()
    connection.connected = True

    connection_manager = MagicMock()
    connection_manager._build_price_lookup_candidates.return_value = ["GOLD"]
    connection_manager._resolve_broker_symbol_name.return_value = "Gold"
    open_ctx = AsyncMock()
    open_ctx.__aenter__.return_value = connection
    open_ctx.__aexit__.return_value = False
    connection_manager.get_connection.return_value = open_ctx

    fake_mt5 = MagicMock()
    fake_mt5.TRADE_ACTION_DEAL = 1
    fake_mt5.ORDER_TYPE_BUY = 0
    fake_mt5.ORDER_TYPE_SELL = 1
    fake_mt5.ORDER_TIME_GTC = 0
    fake_mt5.ORDER_FILLING_IOC = 0
    fake_mt5.TRADE_RETCODE_DONE = 100
    fake_mt5.symbol_select.return_value = True
    fake_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=4100.0, ask=4101.0)
    fake_mt5.order_send.return_value = SimpleNamespace(retcode=fake_mt5.TRADE_RETCODE_DONE, order=123456)

    signal = SimpleNamespace(
        id=7,
        telegram_message_id=42,
        telegram_chat_id=-100123,
        sender="signal_user",
        raw_text="BUY GOLD at 4100",
        parsed_action=ParsedAction.SELL,
        symbol="GOLD",
        entry_price=4100.0,
        stop_loss=4088.0,
        take_profit=4125.0,
        confidence_score=0.98,
        parser_type="REGEX",
        status="PENDING",
    )

    manager = PositionManager(connection_manager=connection_manager)

    with patch("src.mt5_executor.position_manager.mt5", fake_mt5):
        result = await manager.open_market_order(signal, volume=0.1)

    assert result.success is True
    assert result.ticket == 123456
    fake_mt5.order_send.assert_called_once()
    request = fake_mt5.order_send.call_args[0][0]
    assert request["action"] == fake_mt5.TRADE_ACTION_DEAL
    assert request["symbol"] == "Gold"
    assert request["type"] == fake_mt5.ORDER_TYPE_SELL
    assert request["price"] == 4100.0
    assert request["volume"] == 0.1


@pytest.mark.asyncio
async def test_signal_execution_claim_blocks_replays_and_retries_legacy_action_rejection():
    database = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    repository = SignalRepository(database)

    try:
        signal_id = await repository.save_signal({
            "telegram_message_id": 1001,
            "telegram_chat_id": -1001,
            "sender": "signal_user",
            "timestamp": datetime.now(),
            "raw_text": "SELL GOLD",
            "parsed_action": "SELL",
            "symbol": "GOLD",
            "entry_price": 4100.0,
            "confidence_score": 0.95,
            "parser_type": ParserType.REGEX,
            "status": SignalStatus.PENDING,
        })

        assert await repository.claim_signal_for_execution(signal_id) is True
        assert await repository.claim_signal_for_execution(signal_id) is False

        await repository.update_signal_status(
            signal_id,
            SignalStatus.REJECTED,
            rejection_reason="Unsupported action for MT5 order: PARSEDACTION.SELL",
        )
        assert await repository.claim_signal_for_execution(signal_id) is True

        await repository.update_signal_status(
            signal_id,
            SignalStatus.REJECTED,
            rejection_reason="Market closed",
        )
        assert await repository.claim_signal_for_execution(signal_id) is False
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_application_skips_mt5_when_signal_was_already_claimed():
    application = ApplicationManager.__new__(ApplicationManager)
    application.mt5_connection_manager = MagicMock()
    application._signal_repository = AsyncMock()
    application._signal_repository.find_by_message_id.return_value = SimpleNamespace(id=3)
    application._signal_repository.claim_signal_for_execution.return_value = False
    application.position_manager = None
    application.logger = MagicMock()
    signal = SimpleNamespace(
        telegram_message_id=40,
        parsed_action=ParsedAction.SELL,
        symbol="GOLD",
    )

    await application._execute_signal_on_mt5(signal)

    application._signal_repository.claim_signal_for_execution.assert_awaited_once_with(3)
    assert application.position_manager is None
