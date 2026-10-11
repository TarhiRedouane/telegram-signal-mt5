"""Zone rejection on closed candles: ZONE -> sweep (liquidity) -> reintegration -> displacement.

Written for a BUY zone; a SELL zone is evaluated on price-mirrored candles so both share one path.
Candle times are MT5 server epochs; only compared with each other, never with wall clock.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import NamedTuple

M3_SECONDS = 180


class Candle(NamedTuple):
    time: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    def mirrored(self) -> "Candle":
        return Candle(self.time, -self.open, -self.low, -self.high, -self.close, self.volume)


class Phase(Enum):
    WAIT_ZONE = "wait_zone"
    SWEPT = "swept"
    RECLAIMED = "reclaimed"
    CONFIRMED = "confirmed"
    INVALID = "invalid"


@dataclass(frozen=True)
class RejectionParams:
    max_sweep: Decimal  # deepest wick beyond the zone edge still counted as a liquidity grab
    reclaim_candles: int = 3  # M1 candles allowed to close back inside the zone
    confirm_candles: int = 3  # M3 candles allowed to show displacement after the reclaim
    min_body_ratio: Decimal = Decimal("0.5")
    volume_lookback: int = 20


@dataclass
class RejectionState:
    sell: bool
    low: Decimal  # zone bounds as given (real prices)
    high: Decimal
    phase: Phase = Phase.WAIT_ZONE
    extreme: Decimal | None = None  # sweep low (BUY) / sweep high (SELL), real price
    since_sweep: int = 0
    reclaim_time: int | None = None
    highs: list[tuple[int, Decimal]] = field(
        default_factory=list
    )  # M1 highs since sweep (mirrored)
    last_m1: int | None = None
    last_m3: int | None = None
    reason: str = ""

    @property
    def done(self) -> bool:
        return self.phase in (Phase.CONFIRMED, Phase.INVALID)


def advance(
    state: RejectionState, m1: list[Candle], m3: list[Candle], params: RejectionParams
) -> RejectionState:
    """Feed closed candles (oldest first); already-seen candles are skipped. The first call only
    sets the baseline: nothing that closed before the watch started counts."""
    if state.last_m1 is None:
        state.last_m1 = m1[-1].time if m1 else 0
        state.last_m3 = m3[-1].time if m3 else 0
        return state
    sign = Decimal(-1) if state.sell else Decimal(1)
    flip = (lambda c: c.mirrored()) if state.sell else (lambda c: c)
    low, high = (-state.high, -state.low) if state.sell else (state.low, state.high)
    floor = low - params.max_sweep

    for candle in (flip(c) for c in m1 if c.time > state.last_m1):
        state.last_m1 = candle.time
        if state.done:
            continue
        if state.phase == Phase.WAIT_ZONE:
            if candle.high < low or candle.low >= low:  # no sweep: beyond the zone, or still inside
                continue
            state.extreme = candle.low * sign
            state.highs = [(candle.time, candle.high)]
            if candle.low < floor:
                _invalid(state, "sweep too deep")
            elif candle.close >= low:
                state.phase, state.reclaim_time = Phase.RECLAIMED, candle.time  # wick out, back in
            else:
                state.phase, state.since_sweep = Phase.SWEPT, 0
            continue
        state.extreme = min(state.extreme * sign, candle.low) * sign
        state.highs.append((candle.time, candle.high))
        if candle.low < floor:
            _invalid(state, "sweep too deep / accepted beyond zone")
        elif state.phase == Phase.SWEPT:
            state.since_sweep += 1
            if candle.close >= low:
                state.phase, state.reclaim_time = Phase.RECLAIMED, candle.time
            elif state.since_sweep >= params.reclaim_candles:
                _invalid(state, "no reintegration after sweep")
        elif candle.close < low:  # lost the reclaim: needs a new reintegration
            state.phase, state.since_sweep, state.reclaim_time = Phase.SWEPT, 0, None

    if state.phase == Phase.RECLAIMED:
        _confirm(state, [flip(c) for c in m3], high, params)
    return state


def _confirm(
    state: RejectionState, m3: list[Candle], high: Decimal, params: RejectionParams
) -> None:
    """Displacement: an M3 candle after the reclaim closes above structure (or the zone top),
    with a strong body and above-average tick volume."""
    reclaim = state.reclaim_time or 0
    window = [c for c in m3 if c.time + M3_SECONDS > reclaim]
    for candle in (c for c in window if c.time > (state.last_m3 or 0)):
        state.last_m3 = candle.time
        index = m3.index(candle)
        history = m3[max(0, index - params.volume_lookback) : index]
        avg_volume = sum(c.volume for c in history) / len(history) if history else 0
        prior_highs = [h for t, h in state.highs if t < candle.time]
        threshold = min(high, max(prior_highs)) if prior_highs else high
        rng = candle.high - candle.low
        if (
            candle.close > threshold
            and rng > 0
            and candle.close - candle.open >= rng * params.min_body_ratio
            and candle.volume >= avg_volume
        ):
            state.phase = Phase.CONFIRMED
            return
    if len(window) >= params.confirm_candles:
        _invalid(state, "no displacement after reintegration")


def _invalid(state: RejectionState, reason: str) -> None:
    state.phase, state.reason = Phase.INVALID, reason
