"""Story 20.7: a complete single-message signal is sized and risk-checked on the live price (review F35).

Balance 10 000, risk 3 %, tick 0.01 = $1 per lot: a 10-point stop is a 0.30 lot, a 25-point stop 0.12.
"""

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from config.settings import settings
from src.risk_manager import risk_guard
from src.signal_parser.regex_parser import RegexParser

pytestmark = pytest.mark.unit

D = Decimal
CHAT = -100777
SELL = "SELL GOLD 4150\nSL 4160\nTP 4130"
BUY = "BUY GOLD 4150\nSL 4140\nTP 4170"


def _app(monkeypatch, tmp_path, *prices):
    """App wired to fakes: `get_current_price` returns `prices` in order (the last one repeats)."""
    from main import ApplicationManager

    monkeypatch.setattr(settings, "kill_switch_file", tmp_path / "STOP_TRADING")
    monkeypatch.setattr(settings, "trading_enabled", True)
    monkeypatch.setattr(settings, "risk_percent", D("3"))
    monkeypatch.setattr(settings, "max_open_risk_percent", D("6"))
    monkeypatch.setattr(settings, "max_open_legs", 6)
    monkeypatch.setattr(settings, "daily_loss_limit_percent", D("6"))
    # 20 pips at 0.10/pip; set here so a tuned .env cannot change the expected numbers
    monkeypatch.setattr(settings, "gold_min_sl_distance", D("2.0"), raising=False)

    app = ApplicationManager()
    app.logger = MagicMock()
    app.regex_parser = RegexParser()
    app._database_ready = True
    app.mt5_connection_manager = MagicMock()
    quotes = [None if p is None else D(p) for p in prices]
    app.mt5_connection_manager.get_current_price = AsyncMock(side_effect=quotes + quotes[-1:] * 10)
    app._signal_repository = SimpleNamespace(
        save_signal=AsyncMock(),
        find_by_message_id=AsyncMock(return_value=SimpleNamespace(id=7, status="PENDING")),
        claim_signal_for_execution=AsyncMock(return_value=True),
        update_signal_status=AsyncMock(),
    )
    app._position_repository = SimpleNamespace(
        create=AsyncMock(return_value=SimpleNamespace(id=1, mt5_ticket=99))
    )
    app.position_manager = SimpleNamespace(
        get_sizing_inputs=AsyncMock(
            return_value={
                "balance": D("10000"),
                "tick_size": D("0.01"),
                "tick_value": D("1"),
                "volume_min": D("0.01"),
                "volume_step": D("0.01"),
                "volume_max": D("100"),
            }
        ),
        get_risk_snapshot=AsyncMock(
            return_value={
                "balance": D("10000"),
                "equity": D("10000"),
                "open_legs": 0,
                "open_risk": D(0),
                "unprotected": 0,
                "realized_today": D(0),
                "tick_size": D("0.01"),
                "tick_value": D("1"),
            }
        ),
        open_market_order=AsyncMock(
            return_value=SimpleNamespace(
                success=True, ticket=99, fill_price=float(prices[-1] or 0), error_message=None
            )
        ),
    )
    return app


async def _post(app, text, message_id=1):
    await app._on_telegram_message(
        {
            "telegram_chat_id": CHAT,
            "telegram_message_id": message_id,
            "sender": "s",
            "timestamp": datetime.now(UTC),
            "raw_text": text,
        }
    )


def _sent_volume(app) -> float:
    return app.position_manager.open_market_order.await_args.kwargs["volume"]


def _sent_signal(app):
    return app.position_manager.open_market_order.await_args.args[0]


async def test_sell_is_sized_on_the_live_distance(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4135")  # market 15 points below the post's entry
    await _post(app, SELL)

    # SL 4160 is 25 points from the market: 300 / (25 / 0.01 * 1) = 0.12, not the 0.30 of a 10-point stop
    assert _sent_volume(app) == pytest.approx(0.12)
    assert _sent_signal(app).entry_price == D("4135")


async def test_risk_guard_gets_the_live_distance(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4135")
    calls = []
    real = risk_guard.order_risk

    def spy(sl_distance, volume, tick_size, tick_value):
        calls.append((abs(sl_distance), volume))
        return real(sl_distance, volume, tick_size, tick_value)

    monkeypatch.setattr(risk_guard, "order_risk", spy)
    await _post(app, SELL)

    assert calls == [(D("25"), D("0.12"))]


async def test_buy_mirrors_sell(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4165")  # market 15 points above the post's entry
    await _post(app, BUY)

    assert _sent_volume(app) == pytest.approx(0.12)  # SL 4140 is 25 points below 4165
    assert _sent_signal(app).entry_price == D("4165")


async def test_signal_at_market_is_sized_as_before(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4150")  # market == the post's entry
    await _post(app, SELL)

    assert _sent_volume(app) == pytest.approx(0.30)
    signal = _sent_signal(app)
    assert (signal.entry_price, signal.stop_loss, signal.take_profit) == (
        D("4150"),
        D("4160"),
        D("4130"),
    )
    assert not any(
        "live price" in str(c) and "sized" in str(c) for c in app.logger.info.call_args_list
    )


async def test_log_names_both_prices_when_they_differ(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4135")
    await _post(app, SELL)

    lines = [str(c) for c in app.logger.info.call_args_list if "sized on live price" in str(c)]
    assert len(lines) == 1
    assert "4135" in lines[0] and "4150" in lines[0] and "message_id=1" in lines[0]


@pytest.mark.parametrize(
    ("text", "price"),
    [(SELL, "4160"), (SELL, "4165"), (BUY, "4140"), (BUY, "4135")],
    ids=["sell-at-sl", "sell-past-sl", "buy-at-sl", "buy-past-sl"],
)
async def test_market_at_or_beyond_sl_sends_and_saves_nothing(monkeypatch, tmp_path, text, price):
    app = _app(monkeypatch, tmp_path, price)
    await _post(app, text)

    app.position_manager.open_market_order.assert_not_awaited()
    app._signal_repository.save_signal.assert_not_awaited()
    assert any("at or beyond SL" in str(c) for c in app.logger.warning.call_args_list)


async def test_unreadable_second_price_read_refuses_the_signal(monkeypatch, tmp_path):
    app = _app(
        monkeypatch, tmp_path, "4135", None
    )  # live check reads 4135, the sizing read gets nothing
    await _post(app, SELL)

    app.position_manager.open_market_order.assert_not_awaited()
    app._signal_repository.save_signal.assert_not_awaited()
    app.logger.warning.assert_called()


async def test_incomplete_signal_keeps_the_authors_zone(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4135")
    app._watch_setup = AsyncMock()  # no watcher task: only the setup it starts is checked
    await _post(app, "SELL GOLD 4150")

    setup = app._setup_aggregator.get(CHAT)
    assert setup is not None
    assert (setup.low, setup.high) == (D("4150"), D("4150"))
    app.position_manager.open_market_order.assert_not_awaited()


async def test_llm_break_even_sl_is_refused_without_raising(monkeypatch, tmp_path):
    """LLM signals can carry stop_loss="break_even" (llm_parser.py): not a price, so it cannot be sized."""
    app = _app(monkeypatch, tmp_path, "4135")
    signal = RegexParser().parse(SELL)
    signal.stop_loss = "break_even"
    app.regex_parser = SimpleNamespace(parse=MagicMock(return_value=signal))
    await _post(app, SELL)  # must not raise into the Telegram handler / polling loop

    app.position_manager.open_market_order.assert_not_awaited()
    app._signal_repository.save_signal.assert_not_awaited()
    assert any("Signal refused" in str(c) for c in app.logger.warning.call_args_list)


async def test_missing_mt5_manager_refuses_the_signal(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4135")
    app._validate_live_price = AsyncMock(return_value=True)  # reach the sizing step without MT5
    app.mt5_connection_manager = None
    await _post(app, SELL)

    app.position_manager.open_market_order.assert_not_awaited()
    app._signal_repository.save_signal.assert_not_awaited()
    assert any("Signal refused" in str(c) for c in app.logger.warning.call_args_list)


# --- Minimum stop (v0.4): never under 20 pips (2.0) from the market unless the post's own stop is tighter

SELL_15_PIPS = "SELL GOLD 4150\nSL 4151.5\nTP 4130"  # the author chose a 1.5 stop


def _spy_order_risk(monkeypatch) -> list:
    calls = []
    real = risk_guard.order_risk

    def spy(sl_distance, volume, tick_size, tick_value):
        calls.append(abs(sl_distance))
        return real(sl_distance, volume, tick_size, tick_value)

    monkeypatch.setattr(risk_guard, "order_risk", spy)
    return calls


@pytest.mark.parametrize(
    ("text", "price", "sl", "volume", "distance"),
    [
        (SELL, "4158.5", "4160.5", 1.50, "2.0"),  # 1.5 left -> moved out to 2.0
        (BUY, "4141.5", "4139.5", 1.50, "2.0"),
        (SELL_15_PIPS, "4150.8", "4152.3", 2.00, "1.5"),  # 0.7 left -> the author's 1.5
        (SELL_15_PIPS, "4150", "4151.5", 2.00, "1.5"),  # exactly the author's stop: unchanged
        (SELL, "4158", "4160", 1.50, "2"),  # exactly 2.0 left: unchanged
        (SELL, "4135", "4160", 0.12, "25"),  # far: unchanged (AC 1)
    ],
    ids=["sell-moved", "buy-moved", "author-15-moved", "author-15-kept", "at-minimum", "far"],
)
async def test_stop_is_never_under_the_minimum(
    monkeypatch, tmp_path, text, price, sl, volume, distance
):
    app = _app(monkeypatch, tmp_path, price)
    risk_distances = _spy_order_risk(monkeypatch)
    await _post(app, text)

    signal = _sent_signal(app)
    assert signal.stop_loss == D(sl)
    assert _sent_volume(app) == pytest.approx(volume)
    assert risk_distances == [D(distance)]
    assert signal.take_profit == (D("4170") if text == BUY else D("4130"))  # TP never moved


async def test_moved_sl_is_logged_and_unmoved_is_not(monkeypatch, tmp_path):
    app = _app(monkeypatch, tmp_path, "4158.5")
    await _post(app, SELL)
    moved = [str(c) for c in app.logger.info.call_args_list if "SL moved" in str(c)]
    assert len(moved) == 1
    assert "4160" in moved[0] and "4160.5" in moved[0] and "2.0" in moved[0]

    app = _app(monkeypatch, tmp_path, "4135")
    await _post(app, SELL)
    assert not any("SL moved" in str(c) for c in app.logger.info.call_args_list)


async def test_no_policy_minimum_means_no_move(monkeypatch, tmp_path):
    from src.signal_parser.symbol_policy import symbol_policy

    app = _app(monkeypatch, tmp_path, "4159.5")  # 0.5 left
    monkeypatch.setattr(symbol_policy, "get_min_sl_distance", lambda _symbol: None, raising=False)
    await _post(app, SELL)

    assert _sent_signal(app).stop_loss == D("4160")
    assert _sent_volume(app) == pytest.approx(6.00)  # 300 / (0.5 / 0.01)


async def test_moved_sl_is_rounded_to_broker_precision_everywhere(monkeypatch, tmp_path):
    """Live mid prices carry float noise (logs: 4197.129999999999): the moved SL must not."""
    app = _app(monkeypatch, tmp_path, "4158.499999999999")
    await _post(app, SELL)

    assert _sent_signal(app).stop_loss == D("4160.5")  # MT5 order
    saved = app._signal_repository.save_signal.await_args.args[0]
    assert saved["stop_loss"] == 4160.5  # signals row
    created = app._position_repository.create.await_args.kwargs
    assert created["current_sl"] == 4160.5  # positions row
