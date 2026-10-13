from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from main import ApplicationManager
from src.correlation_engine.setup_aggregator import SetupAggregator
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
    fake_mt5.order_send.return_value = SimpleNamespace(
        retcode=fake_mt5.TRADE_RETCODE_DONE, order=123456
    )

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
    # No `price` on the result and MagicMock positions: never stored as a fill (story 20.4)
    assert result.fill_price is None
    fake_mt5.order_send.assert_called_once()
    request = fake_mt5.order_send.call_args[0][0]
    assert request["action"] == fake_mt5.TRADE_ACTION_DEAL
    assert request["symbol"] == "Gold"
    assert request["type"] == fake_mt5.ORDER_TYPE_SELL
    assert request["price"] == 4100.0
    assert request["volume"] == 0.1


def _fill_test_manager(order_result, positions=None):
    """PositionManager + fake MT5 where order_send returns `order_result` (story 20.4)."""
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
    fake_mt5.TRADE_RETCODE_DONE = 100
    fake_mt5.TRADE_RETCODE_DONE_PARTIAL = 10
    fake_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=4100.0, ask=4101.0)
    fake_mt5.order_send.return_value = order_result
    if isinstance(positions, Exception):
        fake_mt5.positions_get.side_effect = positions
    else:
        fake_mt5.positions_get.return_value = positions
    signal = SimpleNamespace(
        parsed_action=ParsedAction.BUY,
        symbol="GOLD",
        entry_price=4100.0,
        stop_loss=4088.0,
        take_profit=4125.0,
    )
    return PositionManager(connection_manager=connection_manager), fake_mt5, signal


@pytest.mark.asyncio
async def test_open_market_order_returns_the_deal_price_as_fill():
    manager, fake_mt5, signal = _fill_test_manager(
        SimpleNamespace(retcode=100, order=555, price=4101.3)
    )
    with patch("src.mt5_executor.position_manager.mt5", fake_mt5):
        result = await manager.open_market_order(signal, volume=0.1)

    assert result.success is True
    assert result.fill_price == 4101.3
    fake_mt5.positions_get.assert_not_called()


@pytest.mark.asyncio
async def test_open_market_order_reads_position_open_price_when_deal_price_is_zero():
    manager, fake_mt5, signal = _fill_test_manager(
        SimpleNamespace(retcode=100, order=555, price=0.0),
        positions=[SimpleNamespace(ticket=555, price_open=4101.8)],
    )
    with patch("src.mt5_executor.position_manager.mt5", fake_mt5):
        result = await manager.open_market_order(signal, volume=0.1)

    assert result.success is True
    assert result.fill_price == 4101.8
    fake_mt5.positions_get.assert_called_once_with(ticket=555)


@pytest.mark.asyncio
@pytest.mark.parametrize("positions", [None, [], RuntimeError("terminal busy")])
async def test_open_market_order_stays_filled_when_fill_price_is_unknown(positions):
    manager, fake_mt5, signal = _fill_test_manager(
        SimpleNamespace(retcode=100, order=555, price=0.0), positions=positions
    )
    with patch("src.mt5_executor.position_manager.mt5", fake_mt5):
        result = await manager.open_market_order(signal, volume=0.1)

    assert result.success is True
    assert result.ticket == 555
    assert result.fill_price is None


def _execution_app(order_result):
    application = ApplicationManager.__new__(ApplicationManager)
    application.mt5_connection_manager = MagicMock()
    application._signal_repository = AsyncMock()
    application._signal_repository.find_by_message_id.return_value = SimpleNamespace(
        id=3, status="PENDING"
    )
    application._signal_repository.claim_signal_for_execution.return_value = True
    application._risk_guard_refusal = AsyncMock(return_value=None)
    application.position_manager = AsyncMock()
    application.position_manager.open_market_order.return_value = order_result
    application._position_repository = AsyncMock()
    application.logger = MagicMock()
    signal = SimpleNamespace(
        telegram_message_id=40,
        telegram_chat_id=-100,
        parsed_action=ParsedAction.BUY,
        symbol="GOLD",
        entry_price=4100.0,
        stop_loss=4088.0,
        take_profit=4125.0,
    )
    return application, signal


@pytest.mark.asyncio
async def test_execution_stores_the_fill_price_as_open_price():
    application, signal = _execution_app(
        SimpleNamespace(success=True, ticket=555, fill_price=4101.3)
    )

    await application._execute_signal_on_mt5(signal, volume=0.1)

    kwargs = application._position_repository.create.await_args.kwargs
    assert kwargs["open_price"] == 4101.3
    application.logger.warning.assert_not_called()


@pytest.mark.asyncio
async def test_execution_falls_back_to_the_signal_price_with_a_warning():
    application, signal = _execution_app(SimpleNamespace(success=True, ticket=555, fill_price=None))

    await application._execute_signal_on_mt5(signal, volume=0.1)

    kwargs = application._position_repository.create.await_args.kwargs
    assert kwargs["open_price"] == 4100.0
    assert "fill price unknown" in application.logger.warning.call_args.args[0]


@pytest.mark.asyncio
async def test_modify_position_levels_preserves_unspecified_level():
    connection = MagicMock()
    connection.connected = True

    connection_manager = MagicMock()
    open_ctx = AsyncMock()
    open_ctx.__aenter__.return_value = connection
    open_ctx.__aexit__.return_value = False
    connection_manager.get_connection.return_value = open_ctx

    fake_mt5 = MagicMock()
    fake_mt5.TRADE_ACTION_SLTP = 6
    fake_mt5.TRADE_RETCODE_DONE = 100
    fake_mt5.positions_get.return_value = [
        SimpleNamespace(ticket=123456, symbol="Gold", sl=4160.0, tp=4115.0, magic=17)
    ]
    fake_mt5.order_send.return_value = SimpleNamespace(retcode=fake_mt5.TRADE_RETCODE_DONE)
    manager = PositionManager(connection_manager=connection_manager)

    with patch("src.mt5_executor.position_manager.mt5", fake_mt5):
        result = await manager.modify_position_levels(ticket=123456, new_sl=4162.0)

    assert result.success is True
    request = fake_mt5.order_send.call_args.args[0]
    assert request["symbol"] == "Gold"
    assert request["sl"] == 4162.0
    assert request["tp"] == 4115.0


@pytest.mark.asyncio
async def test_signal_execution_claim_blocks_replays_and_retries_legacy_action_rejection():
    database = DatabaseManager("sqlite+aiosqlite:///:memory:")
    await database.initialize()
    repository = SignalRepository(database)

    try:
        signal_id = await repository.save_signal(
            {
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
            }
        )

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
        telegram_chat_id=-100,
        parsed_action=ParsedAction.SELL,
        symbol="GOLD",
    )

    await application._execute_signal_on_mt5(signal)

    application._signal_repository.claim_signal_for_execution.assert_awaited_once_with(3)
    assert application.position_manager is None


@pytest.mark.asyncio
async def test_telegram_callback_correlates_before_attempting_new_signal_parse():
    application = ApplicationManager.__new__(ApplicationManager)
    application._seen_message_keys = set()
    application._messages_today = 0
    application._last_message_time = None
    application._parsed_signals_today = 0
    application.logger = MagicMock()
    application.regex_parser = MagicMock()
    application.regex_parser.parse.return_value = None
    application.llm_parser = None
    application.console_dashboard = None
    application._correlation_engine = AsyncMock()
    application._correlation_engine.correlate_message.return_value = object()
    application._handle_correlated_message = AsyncMock()
    application._setup_aggregator = SetupAggregator()

    metadata = {
        "telegram_chat_id": -100123,
        "telegram_message_id": 51,
        "sender": "signal_provider",
        "timestamp": datetime.now(),
        "raw_text": "Je vise les 4110",
        "reply_to_message_id": 50,
    }

    await application._on_telegram_message(metadata)

    application._correlation_engine.correlate_message.assert_awaited_once()
    application._handle_correlated_message.assert_awaited_once()
    application.regex_parser.parse.assert_not_called()


@pytest.mark.asyncio
async def test_correlated_llm_target_updates_mt5_tp_and_database():
    from src.signal_parser.context_analyzer import ContextAnalysisResult, ContextAnalyzer

    application = ApplicationManager.__new__(ApplicationManager)
    application._signal_repository = AsyncMock()
    application._signal_repository.get_by_id.return_value = SimpleNamespace(
        id=7,
        telegram_message_id=50,
        telegram_chat_id=-100123,
        sender="signal_provider",
        timestamp=datetime.now(),
        raw_text="SELL GOLD 4147 - 4153",
        parsed_action="SELL",
        entry_price=4150.0,
    )
    application._position_repository = AsyncMock()
    application.mt5_connection_manager = MagicMock()
    application.position_manager = AsyncMock()
    application.position_manager.modify_position_levels.return_value = SimpleNamespace(success=True)
    application.llm_parser = MagicMock()
    application.logger = MagicMock()
    position = SimpleNamespace(
        id=10,
        signal_id=7,
        mt5_ticket=123456,
        open_price=4150.0,
        current_sl=4162.0,
        current_tp=None,
    )
    analysis = ContextAnalysisResult(
        action="modify",
        target_position="123456",
        parameters={"new_tp": 4110.0, "new_sl": None},
        confidence=0.91,
        reasoning="SELL target identified from French follow-up",
        correlation_id="test-correlation",
        analysis_latency_ms=1,
    )
    metadata = {
        "telegram_message_id": 51,
        "telegram_chat_id": -100123,
        "sender": "signal_provider",
        "timestamp": datetime.now(),
        "raw_text": "Je vise les 4110",
    }

    with patch.object(
        ContextAnalyzer,
        "analyze_update_context",
        new_callable=AsyncMock,
        return_value=analysis,
    ):
        await application._handle_correlated_message(metadata, position)

    application.position_manager.modify_position_levels.assert_awaited_once_with(
        ticket=123456,
        new_sl=None,
        new_tp=4110.0,
    )
    application._position_repository.update_position_mt5_data.assert_awaited_once_with(
        10,
        {"current_tp": 4110.0},
    )
