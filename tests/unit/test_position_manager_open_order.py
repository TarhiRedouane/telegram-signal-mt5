from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.mt5_executor.position_manager import PositionManager


@pytest.mark.asyncio
async def test_open_market_order_submits_mt5_buy_request():
    connection = MagicMock()
    connection.connected = True

    connection_manager = MagicMock()
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
    fake_mt5.symbol_info_tick.return_value = SimpleNamespace(bid=4100.0, ask=4101.0)
    fake_mt5.order_send.return_value = SimpleNamespace(retcode=fake_mt5.TRADE_RETCODE_DONE, order=123456)

    signal = SimpleNamespace(
        id=7,
        telegram_message_id=42,
        telegram_chat_id=-100123,
        sender="signal_user",
        raw_text="BUY GOLD at 4100",
        parsed_action="BUY",
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
    assert request["symbol"] == "GOLD"
    assert request["type"] == fake_mt5.ORDER_TYPE_BUY
    assert request["volume"] == 0.1
