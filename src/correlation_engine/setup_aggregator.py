"""Aggregate a trade setup announced over several Telegram messages into one order."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from config.settings import settings
from src.correlation_engine.rejection_detector import RejectionState
from src.signal_parser import ParsedAction, ParsedSignal
from src.signal_parser.patterns import FrenchSignalPatterns, close_timeframe, sl_far, tp_levels
from src.signal_parser.symbol_policy import symbol_policy

logger = logging.getLogger(__name__)

_ACTION_WORD = re.compile(
    r"\b(?:buy|sell|achat|vente|long|short|ach[eè]t\w*|vend(?!redi)\w*)\b", re.IGNORECASE
)
_PRICE = re.compile(r"(?<!\d)\d{4}(?:[.,]\d{1,2})?(?!\d)")


def _to_decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", "."))


def has_action_word(text: str) -> bool:
    return bool(_ACTION_WORD.search(text))


def _named_symbol(text: str) -> str | None:
    """Canonical symbol of the first enabled alias named as a whole word."""
    aliases = "|".join(re.escape(a) for a in symbol_policy.get_enabled_aliases())
    match = re.search(rf"(?<![\w/])(?:{aliases})(?![\w/])", text, re.IGNORECASE)
    return symbol_policy.canonicalize_symbol(match.group(0)) if match else None


def looks_like_new_trade(text: str) -> bool:
    """Action word + enabled symbol alias + price, all as whole words."""
    return bool(has_action_word(text) and _named_symbol(text) and _PRICE.search(text))


_BARE_PRICE = re.compile(
    r"\s*(\d{4}(?:[.,]\d{1,2})?)(?:\s*[-\u2013]\s*(\d{4}(?:[.,]\d{1,2})?))?\s*"
)


@dataclass
class Followup:
    """Levels/intents extracted from one follow-up message."""

    cancel: bool = False
    sl: Decimal | None = None
    tps: list[Decimal] = field(default_factory=list)  # "TP a | b | c": replaces the TP list
    tp_slots: dict[int, Decimal] = field(default_factory=dict)  # "TP2 : 4128" -> {1: 4128}
    final_tp: Decimal | None = None  # "je vise les 4050": last target
    entry: Decimal | None = None
    entry_high: Decimal | None = None  # "SELL → 4197 - 4200" restates a zone, not a single price
    now: bool = False
    sl_close_tf: int | None = (
        None  # SL is a candle-close level ("clôture M30 sous X"), not a hard stop
    )
    sl_far: Decimal | None = None  # "SL / INVALIDATION 4145 - 4150": far edge = hard SL

    @property
    def empty(self) -> bool:
        return (
            not self.cancel
            and not self.now
            and self.sl is None
            and self.entry is None
            and not self.tps
            and not self.tp_slots
            and self.final_tp is None
        )


def extract_followup(text: str) -> Followup:
    """Regex-only extraction of cancel / SL / TPs / entry from a follow-up message."""
    if FrenchSignalPatterns.SETUP_CANCEL.search(text):
        return Followup(cancel=True)
    result = Followup()
    result.tps, result.tp_slots = tp_levels(text)
    for attr, pattern in (
        ("sl", FrenchSignalPatterns.FOLLOWUP_SL),
        ("final_tp", FrenchSignalPatterns.FOLLOWUP_FINAL_TP),
        ("entry", FrenchSignalPatterns.FOLLOWUP_ENTRY),
    ):
        match = pattern.search(text)
        if match:
            setattr(result, attr, _to_decimal(match.group(1)))
            if attr == "entry":
                bound = FrenchSignalPatterns.ENTRY_RANGE_SUFFIX.match(text, match.end())
                if bound:
                    result.entry_high = _to_decimal(bound.group(1))
    result.now = bool(FrenchSignalPatterns.FOLLOWUP_NOW.search(text))
    if result.sl is not None:
        result.sl_close_tf = close_timeframe(text)
        result.sl_far = sl_far(text)
    return result


_BUY_WORD = re.compile(r"\b(?:buy|achat|long|ach[eè]t\w*)\b", re.IGNORECASE)
_SELL_WORD = re.compile(r"\b(?:sell|vente|short|vend(?!redi)\w*)\b", re.IGNORECASE)


def action_side(text: str) -> ParsedAction | None:
    """BUY or SELL when the text names exactly one side."""
    buy, sell = bool(_BUY_WORD.search(text)), bool(_SELL_WORD.search(text))
    if buy == sell:
        return None
    return ParsedAction.BUY if buy else ParsedAction.SELL


_EDGE_NOISE = re.compile(r"^\W+|\W+$")  # "  SELL GOLD NOW !!", "sell gold now 🔥"
_SIDE = rf"(?:{_BUY_WORD.pattern}|{_SELL_WORD.pattern})"


def priceless_opener(text: str) -> tuple[ParsedAction, str] | None:
    """(side, symbol) when the whole message is the short order "[je|j'] sell gold [now]".

    Whole-message match: result posts and commentary ("Gold sell setup closed in profit") have
    unbounded phrasing, the order form is short and fixed (story 20.13)."""
    core = _EDGE_NOISE.sub("", text)
    aliases = "|".join(re.escape(a) for a in symbol_policy.get_enabled_aliases())
    order = (
        rf"(?:je\s+|j['\u2019])?(?:{_SIDE}\s+(?:{aliases})|(?:{aliases})\s+{_SIDE})"
        r"(?:\s+(?:now|maintenant))?"
    )
    if not re.fullmatch(order, core, re.IGNORECASE):
        return None
    side, symbol = action_side(core), _named_symbol(core)
    return (side, symbol) if side and symbol else None


def bare_price(text: str) -> tuple[Decimal, Decimal] | None:
    """(low, high) when the message is only a price ("4146") or a range ("4146-4150")."""
    match = _BARE_PRICE.fullmatch(text)
    if match is None:
        return None
    a = _to_decimal(match.group(1))
    b = _to_decimal(match.group(2)) if match.group(2) else a
    return min(a, b), max(a, b)


@dataclass
class ZoneEntry:
    action: ParsedAction
    low: Decimal
    high: Decimal
    state: RejectionState
    touched: bool = False

    def sl_ok(self, sl: Decimal) -> bool:
        return sl > self.high if self.action == ParsedAction.SELL else sl < self.low


@dataclass
class ZoneWatch:
    """Zone hint ("BUY ZONE 4174–4178"): trade only after a confirmed rejection in a zone."""

    chat_id: int
    message_id: int
    sender: str
    timestamp: datetime
    symbol: str
    zones: list[ZoneEntry]  # first = main zone, next = reinforcement zones
    targets: list[Decimal]
    created: datetime
    raw_text: str = ""
    fills: int = 0  # zones that already led to an order
    sl: Decimal | None = None  # author's invalidation; replaces the sweep-based SL when valid
    close_tf: int | None = None  # `sl` is a candle-close level on this timeframe (minutes)
    last_heartbeat: datetime | None = None
    linked_setup: "PendingSetup | None" = None  # early "buy now" leg waiting for this zone

    def distance(self, zone: ZoneEntry, price: Decimal) -> Decimal:
        return max(zone.low - price, price - zone.high, Decimal(0))

    def apply(self, followup: Followup) -> list[str]:
        """Targets / SL sent after the zone post ("targets are 4145, 4160", "invalidation 4118")."""
        changed = []
        targets = list(followup.tps) if followup.tps else list(self.targets)
        for index, level in sorted(followup.tp_slots.items()):
            if index < len(targets):
                targets[index] = level
            else:
                targets.append(level)
        if followup.final_tp is not None and followup.final_tp not in targets:
            targets.append(followup.final_tp)
        if targets != self.targets:
            self.targets = targets
            changed.append("targets")
        if followup.sl is not None and (followup.sl, followup.sl_close_tf) != (
            self.sl,
            self.close_tf,
        ):
            if any(zone.sl_ok(followup.sl) for zone in self.zones):
                self.sl, self.close_tf = followup.sl, followup.sl_close_tf
                changed.append("sl")
            else:
                logger.warning(f"Ignoring wrong-side zone SL {followup.sl}")
        return changed


def _zone_symbol(text: str) -> str:
    """Symbol named in the text, else the policy default (zone posts often omit it)."""
    for alias in sorted(symbol_policy.get_enabled_aliases(), key=len, reverse=True):
        if re.search(rf"(?<![\w/]){re.escape(alias)}(?![\w/])", text, re.IGNORECASE):
            return symbol_policy.canonicalize_symbol(alias) or symbol_policy.get_default_symbol()
    return symbol_policy.get_default_symbol()


def extract_zone_hint(
    text: str,
) -> tuple[str, list[tuple[ParsedAction, Decimal, Decimal]], list[Decimal]] | None:
    """(symbol, [(action, low, high)...], reaction targets) for a zone post, else None."""
    zones = []
    for match in FrenchSignalPatterns.ZONE_HINT.finditer(text):
        word = (match.group(1) or match.group(2)).upper()
        action = ParsedAction.BUY if word in ("BUY", "ACHAT") else ParsedAction.SELL
        a = _to_decimal(match.group(3))
        b = _to_decimal(match.group(4)) if match.group(4) else a  # single level: zero-width zone
        zones.append((action, min(a, b), max(a, b)))
    if not zones:
        return None
    symbol = _zone_symbol(text)
    targets: list[Decimal] = []
    for line in FrenchSignalPatterns.ZONE_TARGET_LINE.finditer(text):
        if not FrenchSignalPatterns.ZONE_HINT.search(line.group(0)):
            targets += [_to_decimal(m) for m in _PRICE.findall(line.group(0))]
    return symbol, zones, targets


def _signal_tps(signal: ParsedSignal) -> list[Decimal]:
    if signal.take_profits:
        return list(signal.take_profits)
    return [signal.take_profit] if signal.take_profit is not None else []


@dataclass
class PendingSetup:
    """Setup collected across messages; each fill (initial + re-entries) lands in `positions`."""

    chat_id: int
    message_id: int
    sender: str
    timestamp: datetime
    action: ParsedAction
    symbol: str
    low: Decimal | None  # None: "sell gold now", entry not given yet
    high: Decimal | None
    sl: Decimal | None = None
    tps: list[Decimal] = field(default_factory=list)  # nearest first; leg i closes at tps[i]
    raw_texts: list[str] = field(default_factory=list)
    last_update: datetime = field(default_factory=datetime.now)
    positions: list[Any] = field(default_factory=list)
    armed: bool = True  # waiting to open legs for the current entry
    fire_now: bool = False
    # "sell gold now": leg 1 waits for the entry, then fires only close to it or better (entry_gap)
    await_entry: bool = False
    ladder: bool = True  # False after a re-entry: one more order, no new ladder
    leg_base: int = 0  # positions opened before the current entry
    planned_legs: int | None = None  # frozen at first fill so later TPs can't add risk
    blind: bool = (
        False  # leg 1 fired before any TP: it took half the risk, later TPs split the rest
    )
    be_done: bool = False
    be_pending: bool = False  # TP1 hit, break-even not yet accepted by MT5 on every open leg
    close_pending: bool = False  # invalidation hit, some leg not yet closed
    # Consecutive MT5 failures per ("be" | "close", ticket); a key present = that leg still needs a retry
    retry_counts: dict[tuple[str, int], int] = field(default_factory=dict)
    first_fill_at: datetime | None = None
    hold_at: int | None = (
        None  # stop opening legs at this many positions (early leg waits for its zone)
    )
    close_invalidation: tuple[Decimal, int] | None = (
        None  # (level, timeframe min): exit on candle close
    )
    close_check_time: int | None = None  # last candle time checked for close invalidation
    sl_wait_logged: bool = False  # "leg waiting, too close to the SL" already logged
    # Opposite "now" far from an open trade (story 20.14): one order, half risk, default TP
    counter: bool = False
    counter_wait_logged: bool = False  # "counter-trade waiting, too close to the open trade" logged

    def set_sl(self, level: Decimal, close_tf: int | None, far: Decimal | None = None) -> bool:
        """Hard SL at `level`, or for a candle-close invalidation a protective SL beyond it.
        SL range (`far`): candle close checked at the near edge, hard SL at the far edge.
        Returns True when SL or invalidation changed."""
        sell = self.action == ParsedAction.SELL
        if far is not None and not (far > level if sell else far < level):
            far = None  # reversed or typo ("4203 - 2017"): keep the first level only
        invalidation = (level, close_tf) if close_tf else None
        sl = far if far is not None else level
        if invalidation:
            buffer = symbol_policy.get_close_sl_buffer(self.symbol) or Decimal(0)
            sl = max(sl, level + buffer) if sell else min(sl, level - buffer)
        if (sl, invalidation) == (self.sl, self.close_invalidation):
            return False
        self.sl, self.close_invalidation = sl, invalidation
        return True

    def closed_beyond_invalidation(self, close: Decimal) -> bool:
        if self.close_invalidation is None:
            return False
        level = self.close_invalidation[0]
        return close > level if self.action == ParsedAction.SELL else close < level

    @property
    def tp(self) -> Decimal | None:
        return self.tps[0] if self.tps else None

    @property
    def is_complete(self) -> bool:
        return self.sl is not None and bool(self.tps)

    def reopen(self) -> None:
        """New entry after fills: a single extra order, not a new ladder."""
        self.armed = True
        self.ladder = False
        self.leg_base = len(self.positions)
        self.planned_legs = None
        self.blind = False

    def rearm(self, signal: ParsedSignal, now: datetime) -> list[str]:
        """Same-side signal in the same setup: new entry, keep collected SL/TP unless restated."""
        self.message_id = signal.telegram_message_id
        self.timestamp = signal.timestamp or now
        self.low = signal.entry_price
        self.high = signal.entry_price_high or signal.entry_price
        self.raw_texts.append(signal.raw_text)
        self.fire_now = bool(FrenchSignalPatterns.FOLLOWUP_NOW.search(signal.raw_text))
        if self.positions:
            self.reopen()
        self.armed = True
        self.last_update = now
        changed = ["entry"]
        if signal.stop_loss is not None and self.set_sl(
            signal.stop_loss, close_timeframe(signal.raw_text), sl_far(signal.raw_text)
        ):
            changed.append("sl")
        tps = _signal_tps(signal)
        if tps and tps != self.tps:
            self.tps = tps
            changed.append("tp")
        return changed

    def legs(self) -> list[Decimal]:
        """Entry price per leg: one per TP (max setup_max_legs), spread across the zone from first touch."""
        if not self.ladder:
            n = 1
        else:
            n = self.planned_legs or max(
                2 if self.blind else 1, min(len(self.tps), settings.setup_max_legs)
            )
        low, high = self.low, self.high
        if low is None or high is None:
            return []  # entry not given yet: callers check await_entry first
        if n == 1 or low == high:
            first = low if self.action == ParsedAction.SELL else high
            return [first] * n
        step = (high - low) / (n - 1)
        levels = [low + step * i for i in range(n)]
        return levels if self.action == ParsedAction.SELL else levels[::-1]

    def next_leg(self) -> int | None:
        """Index (within the current entry) of the next leg to open, None when all are open."""
        k = len(self.positions) - self.leg_base
        return k if k < len(self.legs()) else None

    def leg_reached(self, k: int, price: Decimal, tolerance: Decimal) -> bool:
        level = self.legs()[k]
        return (
            price >= level - tolerance
            if self.action == ParsedAction.SELL
            else price <= level + tolerance
        )

    def leg_tp(self, index: int) -> Decimal | None:
        """TP for the position at `index` in `positions` (scale-out: worst entry -> TP1)."""
        return self.tps[min(index, len(self.tps) - 1)] if self.tps else None

    def _sl_ok(self, sl: Decimal, reference: Decimal) -> bool:
        return sl > reference if self.action == ParsedAction.SELL else sl < reference

    def locks_profit(self, sl: Decimal, entry: Decimal) -> bool:
        """SL at or beyond break-even for a position opened at `entry`."""
        return not self._sl_ok(sl, entry)

    def _tp_ok(self, tp: Decimal, reference: Decimal) -> bool:
        return tp < reference if self.action == ParsedAction.SELL else tp > reference

    def apply(self, followup: Followup, text: str, now: datetime) -> list[str]:
        """Apply extracted levels with direction sanity; return names of changed fields."""
        changed: list[str] = []
        if followup.entry is not None:
            bound = followup.entry_high or followup.entry
            self.low, self.high = min(followup.entry, bound), max(followup.entry, bound)
            changed.append("entry")
        if followup.now and not self.fire_now:
            self.fire_now = True
            changed.append("now")
        # No entry yet ("sell gold now"): keep levels, fire_levels checks them at the fill price
        sl_ref = self.high if self.action == ParsedAction.SELL else self.low
        tp_ref = self.low if self.action == ParsedAction.SELL else self.high
        if followup.sl is not None:
            if sl_ref is None or self._sl_ok(followup.sl, sl_ref):
                if self.set_sl(followup.sl, followup.sl_close_tf, followup.sl_far):
                    changed.append("sl")
            else:
                logger.warning(
                    f"Ignoring wrong-side SL {followup.sl} for {self.action.value} setup"
                )
        tps = self._merge_tps(followup)
        valid = [tp for tp in tps if tp_ref is None or self._tp_ok(tp, tp_ref)]
        if len(valid) < len(tps):
            logger.warning(
                f"Ignoring wrong-side TPs {sorted(set(tps) - set(valid))} for {self.action.value} setup"
            )
        valid = sorted(set(valid), reverse=self.action == ParsedAction.SELL)
        if valid and valid != self.tps:
            self.tps = valid
            changed.append("tp")
        if changed:
            self.raw_texts.append(text)
            self.last_update = now
        return changed

    def _merge_tps(self, followup: Followup) -> list[Decimal]:
        """Explicit list replaces; "TP2 : x" sets one slot; "je vise x" makes x the final target."""
        tps = list(followup.tps) if followup.tps else list(self.tps)
        for index, level in sorted(followup.tp_slots.items()):
            if index < len(tps):
                tps[index] = level
            else:
                tps.append(level)
        final = followup.final_tp
        if final is not None and final not in tps:
            closer = (
                (lambda tp: tp > final)
                if self.action == ParsedAction.SELL
                else (lambda tp: tp < final)
            )
            tps = [tp for tp in tps if closer(tp)] + [final]
        return tps

    def in_entry_zone(self, price: Decimal, tolerance: Decimal) -> bool:
        if self.low is None or self.high is None:
            return False
        return self.low - tolerance <= price <= self.high + tolerance

    def entry_gap(self, price: Decimal) -> Decimal | None:
        """await_entry leg 1: how much better than the entry `price` is (<= 0: close), or None
        outside the band (worse by more than the entry tolerance, better by more than the
        default SL distance) or while the entry is unknown."""
        legs = self.legs()
        tolerance = symbol_policy.get_entry_tolerance(self.symbol)
        cap = symbol_policy.get_default_sl_distance(self.symbol)
        if not legs or tolerance is None or cap is None:
            return None
        better = price - legs[0] if self.action == ParsedAction.SELL else legs[0] - price
        return better if -tolerance <= better <= cap else None

    def is_expired(self, now: datetime, timeout_seconds: int) -> bool:
        return now - self.last_update > timedelta(seconds=timeout_seconds)

    def to_signal(self, entry: Decimal, sl: Decimal | None, tp: Decimal | None) -> ParsedSignal:
        return ParsedSignal(
            telegram_message_id=self.message_id,
            telegram_chat_id=self.chat_id,
            sender=self.sender,
            timestamp=self.timestamp,
            raw_text="\n".join(self.raw_texts),
            parsed_action=self.action,
            symbol=self.symbol,
            entry_price=entry,
            stop_loss=sl,
            take_profit=tp,
            confidence_score=0.9,
            parser_type="REGEX_AGGREGATED",
            status="PENDING",
        )

    def fire_levels(
        self, price: Decimal, index: int = 0
    ) -> tuple[Decimal | None, Decimal | None] | None:
        """SL/TP for the position at `index` opened at `price`; None if already invalidated there."""
        sl = self.sl
        if sl is None:
            distance = symbol_policy.get_default_sl_distance(self.symbol)
            if distance is None:
                return None
            sl = price + distance if self.action == ParsedAction.SELL else price - distance
        if not self._sl_ok(sl, price):
            return None
        tp = self.leg_tp(index)
        return sl, tp if tp is not None and self._tp_ok(tp, price) else None


class SetupAggregator:
    """One pending setup per chat, plus held setups: open trades still managed while a
    counter-trade is the chat's setup (story 20.14)."""

    # ponytail: in-memory; pending setups lost on restart — persist to DB if restarts mid-setup matter

    def __init__(self) -> None:
        self._setups: dict[int, PendingSetup] = {}
        self._zones: dict[int, ZoneWatch] = {}
        self._held: dict[int, list[PendingSetup]] = {}

    def adopt(self, setup: PendingSetup) -> None:
        """Register a setup built elsewhere (confirmed zone rejection) as the chat's setup."""
        self._setups[setup.chat_id] = setup

    def start_zone(self, watch: ZoneWatch) -> None:
        if watch.chat_id in self._zones:
            logger.info(f"Replacing zone watch in chat {watch.chat_id}")
        self._zones[watch.chat_id] = watch

    def get_zone(self, chat_id: int) -> ZoneWatch | None:
        return self._zones.get(chat_id)

    def drop_zone(self, watch: ZoneWatch, reason: str) -> None:
        if self._zones.get(watch.chat_id) is watch:
            del self._zones[watch.chat_id]
            logger.info(
                f"Zone watch dropped chat={watch.chat_id} message_id={watch.message_id}: {reason}"
            )

    def start(self, signal: ParsedSignal, now: datetime) -> tuple[PendingSetup, list[str] | None]:
        """Return (setup, None) for a new setup, or (setup, changed) when re-arming a live one."""
        existing = self._setups.get(signal.telegram_chat_id)
        if (
            existing is not None
            and existing.action == signal.parsed_action
            and existing.symbol == signal.symbol
        ):
            return existing, existing.rearm(signal, now)
        if existing is not None:
            logger.info(
                f"Replacing setup in chat {signal.telegram_chat_id} (different side/symbol)"
            )
        setup = PendingSetup(
            chat_id=signal.telegram_chat_id,
            message_id=signal.telegram_message_id,
            sender=signal.sender,
            timestamp=signal.timestamp or now,
            action=signal.parsed_action,
            symbol=signal.symbol,
            low=signal.entry_price,
            high=signal.entry_price_high or signal.entry_price,
            tps=_signal_tps(signal),
            raw_texts=[signal.raw_text],
            last_update=now,
            fire_now=bool(FrenchSignalPatterns.FOLLOWUP_NOW.search(signal.raw_text)),
        )
        if signal.stop_loss is not None:
            setup.set_sl(
                signal.stop_loss, close_timeframe(signal.raw_text), sl_far(signal.raw_text)
            )
        self._setups[setup.chat_id] = setup
        return setup, None

    def get(self, chat_id: int) -> PendingSetup | None:
        return self._setups.get(chat_id)

    def hold(self, setup: PendingSetup) -> None:
        """Keep managing `setup` (break-even, invalidation) without it being the chat's setup."""
        if self._setups.get(setup.chat_id) is setup:
            del self._setups[setup.chat_id]
        self._held.setdefault(setup.chat_id, []).append(setup)

    def held(self, chat_id: int) -> list[PendingSetup]:
        return self._held.get(chat_id, [])

    def is_watched(self, setup: PendingSetup) -> bool:
        """The chat's setup or a held one: its watcher keeps running."""
        return self._setups.get(setup.chat_id) is setup or any(
            held is setup for held in self.held(setup.chat_id)
        )

    def drop(self, setup: PendingSetup, reason: str) -> None:
        held = self.held(setup.chat_id)
        if self._setups.get(setup.chat_id) is setup:
            del self._setups[setup.chat_id]
        elif any(h is setup for h in held):
            self._held[setup.chat_id] = [h for h in held if h is not setup]
        else:
            return
        logger.info(
            f"Setup dropped chat={setup.chat_id} message_id={setup.message_id} "
            f"positions={len(setup.positions)}: {reason}"
        )
