"""Account-wide limits checked before every new order: kill switch, open legs, open risk, daily loss."""

from decimal import Decimal
from typing import Any


def order_risk(sl_distance: Decimal | None, volume: Decimal, tick_size: Decimal, tick_value: Decimal) -> Decimal | None:
    """Money lost if this order's SL is hit; None when it has no SL or the specs are missing."""
    if sl_distance is None or not tick_size:
        return None
    return abs(sl_distance) / tick_size * tick_value * volume


def refusal(snapshot: dict[str, Any] | None, new_risk: Decimal | None, settings: Any) -> str | None:
    """Reason to refuse the new order, or None when it fits every limit.

    `snapshot` (PositionManager.get_risk_snapshot): balance, equity, open_legs, open_risk, unprotected,
    realized_today. Positions (or the new order) without a usable SL count as one standard
    RISK_PERCENT trade."""
    if not settings.trading_enabled:
        return "trading disabled (TRADING_ENABLED=false)"
    if settings.kill_switch_file.exists():
        return f"kill switch on ({settings.kill_switch_file})"
    if snapshot is None:
        return "account exposure unavailable from MT5"
    if snapshot.get("trade_allowed") is False:
        return "AutoTrading is OFF in the MT5 terminal (turn on the Algo Trading button, Ctrl+E)"

    balance = snapshot["balance"]
    if balance <= 0:
        return f"balance {balance} not positive"
    standard = balance * settings.risk_percent / 100

    if snapshot["open_legs"] >= settings.max_open_legs:
        return f"{snapshot['open_legs']} legs open (max {settings.max_open_legs})"

    # Today's loss = closed deals today + floating P&L, against the balance at the start of the day
    day_start = balance - snapshot["realized_today"]
    day_loss = -(snapshot["realized_today"] + snapshot["equity"] - balance)
    if day_start > 0 and day_loss / day_start * 100 >= settings.daily_loss_limit_percent:
        return (
            f"daily loss {day_loss / day_start * 100:.2f}% reached limit "
            f"{settings.daily_loss_limit_percent}% (blocked until next server day)"
        )

    total = snapshot["open_risk"] + snapshot["unprotected"] * standard + (standard if new_risk is None else new_risk)
    if total / balance * 100 > settings.max_open_risk_percent:
        return (
            f"open risk would be {total / balance * 100:.2f}% of balance "
            f"(max {settings.max_open_risk_percent}%)"
        )
    return None
