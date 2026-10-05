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
from src.signal_parser.patterns import FrenchSignalPatterns, tp_levels
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


def looks_like_new_trade(text: str) -> bool:
    """Action word + enabled symbol alias + price, all as whole words."""
    aliases = "|".join(re.escape(a) for a in symbol_policy.get_enabled_aliases())
    has_symbol = re.search(rf"(?<![\w/])(?:{aliases})(?![\w/])", text, re.IGNORECASE)
    return bool(has_action_word(text) and has_symbol and _PRICE.search(text))


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

    @property
    def empty(self) -> bool:
        return (
            not self.cancel and not self.now and self.sl is None and self.entry is None
            and not self.tps and not self.tp_slots and self.final_tp is None
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
    return result


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
    last_heartbeat: datetime | None = None

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
        if followup.sl is not None and followup.sl != self.sl:
            if any(zone.sl_ok(followup.sl) for zone in self.zones):
                self.sl = followup.sl
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


def extract_zone_hint(text: str) -> tuple[str, list[tuple[ParsedAction, Decimal, Decimal]], list[Decimal]] | None:
    """(symbol, [(action, low, high)...], reaction targets) for a zone post, else None."""
    zones = []
    for match in FrenchSignalPatterns.ZONE_HINT.finditer(text):
        word = (match.group(1) or match.group(2)).upper()
        action = ParsedAction.BUY if word in ("BUY", "ACHAT") else ParsedAction.SELL
        a, b = _to_decimal(match.group(3)), _to_decimal(match.group(4))
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
    low: Decimal
    high: Decimal
    sl: Decimal | None = None
    tps: list[Decimal] = field(default_factory=list)  # nearest first; leg i closes at tps[i]
    raw_texts: list[str] = field(default_factory=list)
    last_update: datetime = field(default_factory=datetime.now)
    positions: list[Any] = field(default_factory=list)
    armed: bool = True  # waiting to open legs for the current entry
    fire_now: bool = False
    ladder: bool = True  # False after a re-entry: one more order, no new ladder
    leg_base: int = 0  # positions opened before the current entry
    planned_legs: int | None = None  # frozen at first fill so later TPs can't add risk
    be_done: bool = False
    first_fill_at: datetime | None = None

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
        if signal.stop_loss is not None and signal.stop_loss != self.sl:
            self.sl = signal.stop_loss
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
            n = self.planned_legs or max(1, min(len(self.tps), settings.setup_max_legs))
        if n == 1 or self.low == self.high:
            first = self.low if self.action == ParsedAction.SELL else self.high
            return [first] * n
        step = (self.high - self.low) / (n - 1)
        levels = [self.low + step * i for i in range(n)]
        return levels if self.action == ParsedAction.SELL else levels[::-1]

    def next_leg(self) -> int | None:
        """Index (within the current entry) of the next leg to open, None when all are open."""
        k = len(self.positions) - self.leg_base
        return k if k < len(self.legs()) else None

    def leg_reached(self, k: int, price: Decimal, tolerance: Decimal) -> bool:
        level = self.legs()[k]
        return price >= level - tolerance if self.action == ParsedAction.SELL else price <= level + tolerance

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
        sl_ref = self.high if self.action == ParsedAction.SELL else self.low
        tp_ref = self.low if self.action == ParsedAction.SELL else self.high
        if followup.sl is not None and followup.sl != self.sl:
            if self._sl_ok(followup.sl, sl_ref):
                self.sl = followup.sl
                changed.append("sl")
            else:
                logger.warning(f"Ignoring wrong-side SL {followup.sl} for {self.action.value} setup")
        tps = self._merge_tps(followup)
        valid = [tp for tp in tps if self._tp_ok(tp, tp_ref)]
        if len(valid) < len(tps):
            logger.warning(f"Ignoring wrong-side TPs {sorted(set(tps) - set(valid))} for {self.action.value} setup")
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
            closer = (lambda tp: tp > final) if self.action == ParsedAction.SELL else (lambda tp: tp < final)
            tps = [tp for tp in tps if closer(tp)] + [final]
        return tps

    def in_entry_zone(self, price: Decimal, tolerance: Decimal) -> bool:
        return self.low - tolerance <= price <= self.high + tolerance

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

    def fire_levels(self, price: Decimal, index: int = 0) -> tuple[Decimal | None, Decimal | None] | None:
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
    """One pending setup per chat."""

    # ponytail: in-memory; pending setups lost on restart — persist to DB if restarts mid-setup matter

    def __init__(self) -> None:
        self._setups: dict[int, PendingSetup] = {}
        self._zones: dict[int, ZoneWatch] = {}

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
            logger.info(f"Zone watch dropped chat={watch.chat_id} message_id={watch.message_id}: {reason}")

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
            logger.info(f"Replacing setup in chat {signal.telegram_chat_id} (different side/symbol)")
        setup = PendingSetup(
            chat_id=signal.telegram_chat_id,
            message_id=signal.telegram_message_id,
            sender=signal.sender,
            timestamp=signal.timestamp or now,
            action=signal.parsed_action,
            symbol=signal.symbol,
            low=signal.entry_price,
            high=signal.entry_price_high or signal.entry_price,
            sl=signal.stop_loss,
            tps=_signal_tps(signal),
            raw_texts=[signal.raw_text],
            last_update=now,
            fire_now=bool(FrenchSignalPatterns.FOLLOWUP_NOW.search(signal.raw_text)),
        )
        self._setups[setup.chat_id] = setup
        return setup, None

    def get(self, chat_id: int) -> PendingSetup | None:
        return self._setups.get(chat_id)

    def drop(self, setup: PendingSetup, reason: str) -> None:
        if self._setups.get(setup.chat_id) is setup:
            del self._setups[setup.chat_id]
            logger.info(
                f"Setup dropped chat={setup.chat_id} message_id={setup.message_id} "
                f"positions={len(setup.positions)}: {reason}"
            )
