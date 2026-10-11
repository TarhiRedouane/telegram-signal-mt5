from decimal import Decimal

import pytest

from src.correlation_engine.rejection_detector import (
    Candle,
    Phase,
    RejectionParams,
    RejectionState,
    advance,
)

pytestmark = pytest.mark.unit

PARAMS = RejectionParams(max_sweep=Decimal("3"))
BASE = 1_000_000_020  # multiple of 60 and 180


def c(minute: int, o, h, lo, cl, v=100, step=60) -> Candle:
    return Candle(BASE + minute * step, *(Decimal(str(x)) for x in (o, h, lo, cl)), v)


def m3(index: int, o, h, lo, cl, v=100) -> Candle:
    return c(index, o, h, lo, cl, v, step=180)


def _buy_zone() -> RejectionState:
    state = RejectionState(sell=False, low=Decimal("4174"), high=Decimal("4178"))
    history = [m3(-i, 4180, 4181, 4179, 4180, 100) for i in range(20, 0, -1)]
    return advance(state, [c(-1, 4181, 4182, 4180, 4181)], history, PARAMS)  # baseline


def _m3_history():
    return [m3(-i, 4180, 4181, 4179, 4180, 100) for i in range(20, 0, -1)]


def test_sweep_reclaim_displacement_confirms_with_sl_below_sweep():
    state = _buy_zone()
    m1 = [
        c(0, 4179, 4179.5, 4175, 4175.5),  # into zone, no sweep yet
        c(1, 4175.5, 4176, 4172.2, 4173),  # sweep below 4174, closes outside
        c(2, 4173, 4175.6, 4172.8, 4175.2),  # reintegration close back inside
    ]
    advance(state, m1, _m3_history(), PARAMS)
    assert state.phase == Phase.RECLAIMED and state.extreme == Decimal("4172.2")
    # M3 candle starting at minute 3: strong bullish close above structure 4176, big volume
    confirm = m3(1, 4175.2, 4179, 4175, 4178.8, 180)
    advance(state, [*m1, c(3, 4175.2, 4177, 4175, 4176.8)], [*_m3_history(), confirm], PARAMS)
    assert state.phase == Phase.CONFIRMED


def test_wick_without_reintegration_is_invalid():
    state = _buy_zone()
    m1 = [c(0, 4176, 4176.5, 4172, 4173)] + [c(i, 4173, 4173.5, 4172.5, 4173) for i in (1, 2, 3)]
    advance(state, m1, _m3_history(), PARAMS)
    assert state.phase == Phase.INVALID and "reintegration" in state.reason


def test_too_deep_sweep_breaks_zone():
    state = _buy_zone()
    advance(state, [c(0, 4176, 4176.5, 4169, 4170)], _m3_history(), PARAMS)
    assert state.phase == Phase.INVALID


def test_bounce_without_sweep_never_trades():
    state = _buy_zone()
    m1 = [c(0, 4179, 4179, 4174.5, 4175), c(1, 4175, 4182, 4175, 4181.5)]
    advance(state, m1, [*_m3_history(), m3(0, 4179, 4182, 4174.5, 4181.5, 500)], PARAMS)
    assert state.phase == Phase.WAIT_ZONE


def test_weak_candle_after_reclaim_then_timeout():
    state = _buy_zone()
    m1 = [c(0, 4176, 4176.5, 4172.5, 4175)]  # wick out and back in -> reclaimed at once
    advance(state, m1, _m3_history(), PARAMS)
    assert state.phase == Phase.RECLAIMED
    doji = [m3(i, 4175, 4176, 4174.5, 4175.1, 50) for i in range(0, 3)]
    advance(state, m1, _m3_history() + doji, PARAMS)
    assert state.phase == Phase.INVALID and "displacement" in state.reason


def test_history_before_watch_is_ignored():
    state = RejectionState(sell=False, low=Decimal("4174"), high=Decimal("4178"))
    advance(state, [c(0, 4176, 4176.5, 4172, 4175)], _m3_history(), PARAMS)  # baseline only
    assert state.phase == Phase.WAIT_ZONE


def test_sell_zone_mirrored():
    state = RejectionState(sell=True, low=Decimal("4220"), high=Decimal("4224"))
    advance(
        state,
        [c(-1, 4219, 4219.5, 4218, 4219)],
        [m3(-i, 4219, 4220, 4218, 4219) for i in range(20, 0, -1)],
        PARAMS,
    )
    m1 = [
        c(0, 4221, 4226.1, 4220.5, 4225),
        c(1, 4225, 4225.4, 4222, 4223.2),
    ]  # sweep above, back in
    history = [m3(-i, 4219, 4220, 4218, 4219) for i in range(20, 0, -1)]
    advance(state, m1, history, PARAMS)
    assert state.phase == Phase.RECLAIMED and state.extreme == Decimal("4226.1")
    advance(state, m1, [*history, m3(1, 4223, 4223.2, 4219, 4219.3, 300)], PARAMS)
    assert state.phase == Phase.CONFIRMED
