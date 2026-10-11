"""Risk-based lot size: a fixed % of balance is lost if the SL is hit, split across legs."""

import logging
from decimal import ROUND_FLOOR, Decimal

logger = logging.getLogger(__name__)


def leg_volume(
    balance: Decimal,
    risk_percent: Decimal,
    sl_distance: Decimal,
    tick_size: Decimal,
    tick_value: Decimal,
    volume_min: Decimal,
    volume_step: Decimal,
    volume_max: Decimal,
    legs: int,
) -> Decimal:
    """Lots for one leg; floored to the broker step so risk never exceeds the budget (except volume_min)."""
    loss_per_lot = sl_distance / tick_size * tick_value
    if loss_per_lot <= 0 or legs < 1:
        return volume_min
    budget = balance * risk_percent / 100 / legs
    lots = (budget / loss_per_lot / volume_step).to_integral_value(
        rounding=ROUND_FLOOR
    ) * volume_step
    if lots < volume_min:
        logger.warning(
            f"Risk budget {budget:.2f} too small for SL distance {sl_distance}; using minimum lot {volume_min}"
        )
        return volume_min
    return min(lots, volume_max)
