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

_ACTION_WORD = re.compile(r"\b(?:buy|sell|achat|vente|long|short|achete|vends?)\b", re.IGNORECASE)
_PRICE = re.compile(r"(?<!\d)\d{4}(?:[.,]\d{1,2})?(?!\d)")


def _to_decimal(text: str) -> Decimal:
    return Decimal(text.replace(",", "."))


def looks_like_new_trade(text: str) -> bool:
    """Action word + enabled symbol alias + price, all as whole words."""
    aliases = "|".join(re.escape(a) for a in symbol_policy.get_enabled_aliases())
    has_symbol = re.search(rf"(?<![\w/])(?:{aliases})(?![\w/])", text, re.IGNORECASE)
    return bool(_ACTION_WORD.search(text) and has_symbol and _PRICE.search(text))


@dataclass
class Followup:
    """Levels/intents extracted from one follow-up message."""
    cancel: bool = False
    sl: Decimal | None = None
    tp: Decimal | None = None
    entry: Decimal | None = None

    @property
    def empty(self) -> bool:
        return not self.cancel and self.sl is None and self.tp is None and self.entry is None


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
    return result


@dataclass
class PendingSetup:
    """Setup collected across messages; `position` is set once the order is filled."""
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
    position: Any = None

    @property
    def is_complete(self) -> bool:
        return self.sl is not None and self.tp is not None

    def _sl_ok(self, sl: Decimal, reference: Decimal) -> bool:
        return sl > reference if self.action == ParsedAction.SELL else sl < reference

    def _tp_ok(self, tp: Decimal, reference: Decimal) -> bool:
        return tp < reference if self.action == ParsedAction.SELL else tp > reference

    def apply(self, followup: Followup, text: str, now: datetime) -> list[str]:
        """Apply extracted levels with direction sanity; return names of changed fields."""
        changed: list[str] = []
        if followup.entry is not None:
            self.low = self.high = followup.entry
            changed.append("entry")
        sl_ref = self.high if self.action == ParsedAction.SELL else self.low
        tp_ref = self.low if self.action == ParsedAction.SELL else self.high
        if followup.sl is not None:
            if self._sl_ok(followup.sl, sl_ref):
                self.sl = followup.sl
                changed.append("sl")
            else:
                logger.warning(f"Ignoring wrong-side SL {followup.sl} for {self.action.value} setup")
        if followup.tp is not None:
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

    def start(self, signal: ParsedSignal, now: datetime) -> PendingSetup:
        if signal.telegram_chat_id in self._setups:
            logger.info(f"Replacing pending setup in chat {signal.telegram_chat_id}")
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
        )
        self._setups[setup.chat_id] = setup
        return setup

    def get(self, chat_id: int) -> PendingSetup | None:
        return self._setups.get(chat_id)

    def drop(self, setup: PendingSetup, reason: str) -> None:
        if self._setups.get(setup.chat_id) is setup:
            del self._setups[setup.chat_id]
            logger.info(
                f"Setup dropped chat={setup.chat_id} message_id={setup.message_id} "
                f"filled={setup.position is not None}: {reason}"
            )
