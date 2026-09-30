"""Aggregate a trade setup announced over several Telegram messages into one order."""

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from src.signal_parser import ParsedAction, ParsedSignal
from src.signal_parser.patterns import FrenchSignalPatterns
from src.signal_parser.symbol_policy import symbol_policy

logger = logging.getLogger(__name__)

_ACTION_WORD = re.compile(
    r"\b(?:buy|sell|achat|vente|long|short|ach[eè]te[sz]?|vend[sz]?)\b", re.IGNORECASE
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
    tp: Decimal | None = None
    entry: Decimal | None = None
    entry_high: Decimal | None = None  # "SELL → 4197 - 4200" restates a zone, not a single price
    now: bool = False

    @property
    def empty(self) -> bool:
        return (
            not self.cancel and not self.now
            and self.sl is None and self.tp is None and self.entry is None
        )


def extract_followup(text: str) -> Followup:
    """Regex-only extraction of cancel / SL / TP / entry from a follow-up message."""
    if FrenchSignalPatterns.SETUP_CANCEL.search(text):
        return Followup(cancel=True)
    result = Followup()
    for attr, pattern in (
        ("sl", FrenchSignalPatterns.FOLLOWUP_SL),
        ("tp", FrenchSignalPatterns.FOLLOWUP_TP),
        ("entry", FrenchSignalPatterns.FOLLOWUP_ENTRY),
    ):
        match = pattern.search(text)
        if match:
            setattr(result, attr, _to_decimal(match.group(1)))
            if attr == "entry":
                bound = FrenchSignalPatterns.ENTRY_RANGE_SUFFIX.match(text, match.end())
                if bound:
                    result.entry_high = _to_decimal(bound.group(1))
    if result.tp is not None and FrenchSignalPatterns.FOLLOWUP_EXTRA_TP.search(text):
        logger.info(f"Ignoring extra take-profit {result.tp}; MT5 position keeps TP1")
        result.tp = None
    result.now = bool(FrenchSignalPatterns.FOLLOWUP_NOW.search(text))
    return result


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
    tp: Decimal | None = None
    raw_texts: list[str] = field(default_factory=list)
    last_update: datetime = field(default_factory=datetime.now)
    positions: list[Any] = field(default_factory=list)
    armed: bool = True  # waiting to open an order for the current entry
    fire_now: bool = False

    @property
    def is_complete(self) -> bool:
        return self.sl is not None and self.tp is not None

    def rearm(self, signal: ParsedSignal, now: datetime) -> list[str]:
        """Same-side signal in the same setup: new entry, keep collected SL/TP unless restated."""
        self.message_id = signal.telegram_message_id
        self.timestamp = signal.timestamp or now
        self.low = signal.entry_price
        self.high = signal.entry_price_high or signal.entry_price
        self.raw_texts.append(signal.raw_text)
        self.fire_now = bool(FrenchSignalPatterns.FOLLOWUP_NOW.search(signal.raw_text))
        self.armed = True
        self.last_update = now
        changed = ["entry"]
        if signal.stop_loss is not None and signal.stop_loss != self.sl:
            self.sl = signal.stop_loss
            changed.append("sl")
        if signal.take_profit is not None and signal.take_profit != self.tp:
            self.tp = signal.take_profit
            changed.append("tp")
        return changed

    def _sl_ok(self, sl: Decimal, reference: Decimal) -> bool:
        return sl > reference if self.action == ParsedAction.SELL else sl < reference

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
        if followup.tp is not None and followup.tp != self.tp:
            if self._tp_ok(followup.tp, tp_ref):
                self.tp = followup.tp
                changed.append("tp")
            else:
                logger.warning(f"Ignoring wrong-side TP {followup.tp} for {self.action.value} setup")
        if changed:
            self.raw_texts.append(text)
            self.last_update = now
        return changed

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

    def fire_levels(self, price: Decimal) -> tuple[Decimal | None, Decimal | None] | None:
        """SL/TP to send at `price`; None if the setup is already invalidated at this price."""
        sl = self.sl
        if sl is None:
            distance = symbol_policy.get_default_sl_distance(self.symbol)
            if distance is None:
                return None
            sl = price + distance if self.action == ParsedAction.SELL else price - distance
        if not self._sl_ok(sl, price):
            return None
        tp = self.tp if self.tp is not None and self._tp_ok(self.tp, price) else None
        return sl, tp


class SetupAggregator:
    """One pending setup per chat."""

    # ponytail: in-memory; pending setups lost on restart — persist to DB if restarts mid-setup matter

    def __init__(self) -> None:
        self._setups: dict[int, PendingSetup] = {}

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
            tp=signal.take_profit,
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
