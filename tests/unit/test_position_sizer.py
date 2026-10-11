from decimal import Decimal

import pytest

from src.risk_manager.position_sizer import leg_volume

pytestmark = pytest.mark.unit

D = Decimal


def _gold(balance: str, sl_distance: str, legs: int = 1, risk: str = "3") -> Decimal:
    # Gold, 100 oz contract: tick 0.01 = $1 per lot
    return leg_volume(
        balance=D(balance),
        risk_percent=D(risk),
        sl_distance=D(sl_distance),
        tick_size=D("0.01"),
        tick_value=D("1"),
        volume_min=D("0.01"),
        volume_step=D("0.01"),
        volume_max=D("100"),
        legs=legs,
    )


@pytest.mark.parametrize(
    ("balance", "sl", "legs", "expected"),
    [
        ("1000", "10", 1, "0.03"),  # $1000, SL 100 pips, one TP
        ("10000", "10", 1, "0.3"),  # $10000, one TP
        ("10000", "10", 3, "0.1"),  # $10000, 3 TPs -> 0.1 per leg
        ("1000", "5", 1, "0.06"),  # SL 50 pips -> bigger lot, same $ risk
    ],
)
def test_user_examples(balance, sl, legs, expected):
    assert _gold(balance, sl, legs) == D(expected)


def test_floor_to_step_and_min_clamp():
    assert _gold("1000", "7") == D("0.04")  # 30 / 700 = 0.0428 -> floored, never over budget
    assert _gold("100", "10") == D("0.01")  # budget too small -> broker minimum
