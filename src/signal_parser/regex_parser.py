"""Regex-based signal parser for standard French trading formats."""

import decimal
import hashlib
import re
from datetime import datetime
from decimal import Decimal

from config.logging_config import (
    get_contextual_logger,
    set_correlation_id,
    set_service_context,
)

from . import ParsedAction, ParsedSignal, ValidationResult
from .patterns import FrenchSignalPatterns
from .symbol_policy import symbol_policy


class RegexParser:
    """Fast regex-based parser for standard French trading signal formats."""

    def __init__(self) -> None:
        """Initialize regex parser with compiled patterns."""
        self.logger = get_contextual_logger(__name__)
        self.patterns = FrenchSignalPatterns.get_compiled_patterns()
        self.main_patterns = FrenchSignalPatterns.build_main_patterns()

    def parse(self, text: str) -> ParsedSignal | None:
        """
        Parse trading signal from French text using regex patterns.

        Args:
            text: Raw message text to parse

        Returns:
            ParsedSignal object if successful, None if no match
        """
        if not text or not text.strip():
            return None

        # Set service context for logging
        set_service_context("RegexParser", "parse")

        # Generate correlation ID for this parse attempt
        correlation_id = set_correlation_id()

        # Clean and normalize text
        cleaned_text = text.strip().upper()

        # Log parse attempt start
        self.logger.info(
            "Signal parse attempt started",
            extra_fields={
                "signal_data": {
                    "raw_text_hash": self._hash_text(text),
                    "text_length": len(text),
                    "parser": "REGEX"
                }
            }
        )

        # Try main combined patterns first (most specific)
        for pattern in self.main_patterns:
            match = pattern.search(cleaned_text)
            if match:
                try:
                    signal = self._extract_signal_from_match(match, text, correlation_id)
                    if signal:
                        # Validate the extracted signal
                        validation = self._validate_signal(signal)
                        if validation.is_valid:
                            self.logger.info(
                                "Signal parsed successfully",
                                extra_fields={
                                    "signal_data": {
                                        "action": signal.parsed_action.value,
                                        "symbol": signal.symbol,
                                        "entry": str(signal.entry_price),
                                        "confidence": signal.confidence_score,
                                        "parser": "REGEX",
                                        "raw_text_hash": self._hash_text(text)
                                    }
                                }
                            )
                            return signal
                        else:
                            self.logger.warning(
                                "Signal validation failed",
                                extra_fields={
                                    "signal_data": {
                                        "validation_errors": validation.errors,
                                        "parser": "REGEX",
                                        "raw_text_hash": self._hash_text(text)
                                    }
                                }
                            )
                except Exception as e:
                    self.logger.error(
                        f"Error extracting signal from regex match: {e}",
                        extra_fields={
                            "signal_data": {
                                "parser": "REGEX",
                                "raw_text_hash": self._hash_text(text),
                                "error": str(e)
                            }
                        }
                    )

        # Try component-based parsing as fallback
        fallback_signal = self._try_component_parsing(cleaned_text, text, correlation_id)
        if fallback_signal:
            return fallback_signal

        # Log parse failure
        self.logger.info(
            "Parse failed - no patterns matched",
            extra_fields={
                "signal_data": {
                    "parser": "REGEX",
                    "raw_text_hash": self._hash_text(text),
                    "patterns_attempted": len(self.main_patterns) + 1  # +1 for component parsing
                }
            }
        )

        return None

    def _extract_signal_from_match(self, match: re.Match, original_text: str, _correlation_id: str) -> ParsedSignal | None:
        """Extract ParsedSignal from regex match."""
        try:
            groups = match.groups()
            if len(groups) < 3:
                return None

            # Extract action
            action_text = groups[0].strip()
            parsed_action = self._parse_action(action_text)
            if not parsed_action:
                return None

            # Extract symbol and normalize
            symbol_text = groups[1].strip()
            normalized_symbol = self._normalize_symbol(symbol_text)

            # Extract price
            price_text = groups[2].strip()
            try:
                entry_price = Decimal(price_text)
            except (ValueError, TypeError, decimal.InvalidOperation):
                return None

            # Try to extract SL/TP from the full text
            stop_loss = self._extract_stop_loss(original_text)
            take_profit = self._extract_take_profit(original_text)

            entry_price, entry_price_high = self._entry_range(match, entry_price)

            # Create signal object
            signal = ParsedSignal(
                telegram_message_id=0,  # Will be set by caller
                telegram_chat_id=0,     # Will be set by caller
                sender="",              # Will be set by caller
                timestamp=datetime.now(),
                raw_text=original_text,
                parsed_action=parsed_action,
                symbol=normalized_symbol,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                confidence_score=0.95,  # High confidence for regex matches
                parser_type="REGEX",
                status="PENDING",
                entry_price_high=entry_price_high,
            )

            return signal

        except Exception as e:
            self.logger.error(f"Error extracting signal from match: {e}")
            return None

    def _try_component_parsing(self, cleaned_text: str, original_text: str, _correlation_id: str) -> ParsedSignal | None:
        """Try parsing by extracting components individually."""
        try:
            # Extract action
            action = None
            for pattern in self.patterns['buy_actions']:
                if pattern.search(cleaned_text):
                    action = ParsedAction.BUY
                    break

            if not action:
                for pattern in self.patterns['sell_actions']:
                    if pattern.search(cleaned_text):
                        action = ParsedAction.SELL
                        break

            if not action:
                return None

            # Extract instrument
            symbol = None
            for pattern in self.patterns['instruments']:
                match = pattern.search(cleaned_text)
                if match:
                    symbol = self._normalize_symbol(match.group(0))
                    break

            if not symbol:
                return None

            # Extract price
            entry_price = None
            entry_price_high = None
            for pattern in self.patterns['prices']:
                match = pattern.search(cleaned_text)
                if match:
                    try:
                        entry_price = Decimal(match.group(1) if match.groups() else match.group(0))
                        entry_price, entry_price_high = self._entry_range(match, entry_price)
                        break
                    except (ValueError, TypeError, decimal.InvalidOperation):
                        continue

            if not entry_price:
                return None

            # Extract SL/TP
            stop_loss = self._extract_stop_loss(original_text)
            take_profit = self._extract_take_profit(original_text)

            # Create signal
            signal = ParsedSignal(
                telegram_message_id=0,
                telegram_chat_id=0,
                sender="",
                timestamp=datetime.now(),
                raw_text=original_text,
                parsed_action=action,
                symbol=symbol,
                entry_price=entry_price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                confidence_score=0.90,  # Slightly lower confidence for component parsing
                parser_type="REGEX",
                status="PENDING",
                entry_price_high=entry_price_high,
            )

            # Validate before returning
            validation = self._validate_signal(signal)
            if validation.is_valid:
                self.logger.info(
                    "Signal parsed via component extraction",
                    extra_fields={
                        "signal_data": {
                            "action": signal.parsed_action.value,
                            "symbol": signal.symbol,
                            "entry": str(signal.entry_price),
                            "confidence": signal.confidence_score,
                            "parser": "REGEX_COMPONENT",
                            "raw_text_hash": self._hash_text(original_text)
                        }
                    }
                )
                return signal
            else:
                self.logger.warning(
                    "Component-parsed signal validation failed",
                    extra_fields={
                        "signal_data": {
                            "validation_errors": validation.errors,
                            "parser": "REGEX_COMPONENT",
                            "raw_text_hash": self._hash_text(original_text)
                        }
                    }
                )

        except Exception as e:
            self.logger.error(f"Error in component parsing: {e}")

        return None

    @staticmethod
    def _entry_range(match: re.Match, entry_price: Decimal) -> tuple[Decimal, Decimal | None]:
        """Entry zone "4197 - 4200" right after the matched price -> (low, high); else (entry, None)."""
        range_match = FrenchSignalPatterns.ENTRY_RANGE_SUFFIX.match(match.string, match.end())
        if not range_match:
            return entry_price, None
        bound = Decimal(range_match.group(1))
        return min(entry_price, bound), max(entry_price, bound)

    def _parse_action(self, action_text: str) -> ParsedAction | None:
        """Parse action from text."""
        action_upper = action_text.upper()

        # Check buy patterns
        for pattern in FrenchSignalPatterns.BUY_ACTIONS:
            if re.search(pattern, action_upper, re.IGNORECASE):
                return ParsedAction.BUY

        # Check sell patterns
        for pattern in FrenchSignalPatterns.SELL_ACTIONS:
            if re.search(pattern, action_upper, re.IGNORECASE):
                return ParsedAction.SELL

        return None

    def _normalize_symbol(self, symbol_text: str) -> str:
        """Normalize symbol to standard format."""
        canonical_symbol = symbol_policy.canonicalize_symbol(symbol_text)
        if canonical_symbol:
            return canonical_symbol
        return symbol_text.upper().strip()

    def _extract_stop_loss(self, text: str) -> Decimal | None:
        """Extract stop loss from text."""
        for pattern in self.patterns['stop_losses']:
            match = pattern.search(text)
            if match:
                try:
                    return Decimal(match.group(1))
                except (ValueError, TypeError, decimal.InvalidOperation):
                    continue
        return None

    def _extract_take_profit(self, text: str) -> Decimal | None:
        """Extract take profit from text."""
        for pattern in self.patterns['take_profits']:
            match = pattern.search(text)
            if match:
                try:
                    return Decimal(match.group(1))
                except (ValueError, TypeError, decimal.InvalidOperation):
                    continue
        return None

    def _validate_signal(self, signal: ParsedSignal) -> ValidationResult:
        """Validate parsed signal according to symbol policy and trading specs."""
        errors = []
        warnings = []

        canonical_symbol = symbol_policy.canonicalize_symbol(signal.symbol)
        if canonical_symbol is None:
            errors.append(
                f"Unsupported symbol '{signal.symbol}'. "
                f"Enabled symbols: {', '.join(symbol_policy.get_enabled_canonical_symbols())}"
            )
            return ValidationResult(is_valid=False, errors=errors, warnings=warnings)

        # Ensure downstream layers always receive canonical symbols.
        signal.symbol = canonical_symbol

        # Validate symbol-specific entry-price range.
        price_error = symbol_policy.validate_entry_price(signal.symbol, signal.entry_price)
        if price_error:
            errors.append(price_error)

        # Validate SL logic
        if signal.stop_loss:
            if signal.parsed_action == ParsedAction.BUY:
                if signal.stop_loss >= signal.entry_price:
                    errors.append("SL for BUY order must be below entry price")
                elif signal.entry_price - signal.stop_loss > Decimal('5.0'):
                    warnings.append("SL distance > 500 pips, high risk")
                elif signal.entry_price - signal.stop_loss < Decimal('0.05'):
                    warnings.append("SL distance < 5 pips, very tight")
            elif signal.parsed_action == ParsedAction.SELL:
                if signal.stop_loss <= signal.entry_price:
                    errors.append("SL for SELL order must be above entry price")
                elif signal.stop_loss - signal.entry_price > Decimal('5.0'):
                    warnings.append("SL distance > 500 pips, high risk")
                elif signal.stop_loss - signal.entry_price < Decimal('0.05'):
                    warnings.append("SL distance < 5 pips, very tight")

        # Validate TP logic
        if signal.take_profit:
            if signal.parsed_action == ParsedAction.BUY:
                if signal.take_profit <= signal.entry_price:
                    errors.append("TP for BUY order must be above entry price")
                elif signal.take_profit - signal.entry_price < Decimal('0.10'):
                    warnings.append("TP distance < 10 pips, minimum recommended")
            elif signal.parsed_action == ParsedAction.SELL:
                if signal.take_profit >= signal.entry_price:
                    errors.append("TP for SELL order must be below entry price")
                elif signal.entry_price - signal.take_profit < Decimal('0.10'):
                    warnings.append("TP distance < 10 pips, minimum recommended")

        return ValidationResult(
            is_valid=len(errors) == 0,
            errors=errors,
            warnings=warnings
        )

    def _hash_text(self, text: str) -> str:
        """Hash text for logging without exposing content."""
        return hashlib.sha256(text.encode('utf-8')).hexdigest()[:16]
