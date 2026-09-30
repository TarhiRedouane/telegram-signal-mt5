"""Regex patterns for French trading signal parsing."""

import re
from typing import ClassVar

from .symbol_policy import symbol_policy


class FrenchSignalPatterns:
    """French trading signal regex patterns."""

    # Class-level caches for compiled patterns
    _compiled_patterns_cache: ClassVar[dict[str, list[re.Pattern]] | None] = None
    _main_patterns_cache: ClassVar[list[re.Pattern] | None] = None

    # French action patterns (buy/sell)
    BUY_ACTIONS: ClassVar[list[str]] = [
        r"JE\s+BUY",
        r"BUY",
        r"ACHETER",
        r"J'ACH(?:È|E|Ãˆ)TE",
        r"LONG"
    ]

    SELL_ACTIONS: ClassVar[list[str]] = [
        r"JE\s+VEND[S]?",
        r"SELL",
        r"SHORT",
        r"VENDRE",
        r"JE\s+VENDS"
    ]

    # Instrument patterns are driven by enabled symbol aliases.
    INSTRUMENTS: ClassVar[list[str]] = [
        re.escape(alias)
        for alias in sorted(symbol_policy.get_enabled_aliases(), key=len, reverse=True)
    ]

    # Price patterns with various formats
    PRICE_PATTERNS: ClassVar[list[str]] = [
        r"@\s*(\d{4}(?:\.\d{1,2})?)",  # @ 3362 or @ 3362.50
        r"(\d{4}(?:\.\d{1,2})?)",      # 3362 or 3362.50
    ]

    # Stop loss patterns
    SL_PATTERNS: ClassVar[list[str]] = [
        r"SL\s*:?\s*(\d{4}(?:\.\d{1,2})?)",
        r"STOP\s*:?\s*(\d{4}(?:\.\d{1,2})?)",
        r"STOP\s+LOSS\s*:?\s*(\d{4}(?:\.\d{1,2})?)"
    ]

    # Take profit patterns
    TP_PATTERNS: ClassVar[list[str]] = [
        r"TP\s*:?\s*(\d{4}(?:\.\d{1,2})?)",
        r"TARGET\s*:?\s*(\d{4}(?:\.\d{1,2})?)",
        r"TAKE\s+PROFIT\s*:?\s*(\d{4}(?:\.\d{1,2})?)"
    ]

    # Multi-message setup follow-ups (matched against original-case text, IGNORECASE)
    _LEVEL = r"(?<!\d)(\d{4}(?:[.,]\d{1,2})?)"
    ENTRY_RANGE_SUFFIX: ClassVar[re.Pattern] = re.compile(
        r"\s*(?:-|–|/|À|A)\s*(\d{4}(?:\.\d{1,2})?)(?!\d)", re.IGNORECASE
    )
    FOLLOWUP_TP: ClassVar[re.Pattern] = re.compile(
        r"\b(?:TP\d?|TARGET|TAKE\s+PROFIT|OBJECTIFS?|CIBLES?|VIS(?:E|ER|ONS|EZ))\b[^\n]{0,30}?" + _LEVEL,
        re.IGNORECASE,
    )
    FOLLOWUP_SL: ClassVar[re.Pattern] = re.compile(
        r"\b(?:SL|STOP(?:\s+LOSS)?|INVALIDATION)\b[^\n]{0,100}?" + _LEVEL,
        re.IGNORECASE,
    )
    FOLLOWUP_ENTRY: ClassVar[re.Pattern] = re.compile(
        r"\b(?:ENTR(?:ER|E|ÉE|EE|ONS|EZ)|RENTR(?:ER|E|ONS)|SELL|BUY|VEND\w*|ACH[EÈ]T\w*)\b[^\n]{0,20}?"
        + _LEVEL,
        re.IGNORECASE,
    )
    FOLLOWUP_NOW: ClassVar[re.Pattern] = re.compile(r"(?<!NOT\s)(?<!PAS\s)\b(?:NOW|MAINTENANT)\b", re.IGNORECASE)
    FOLLOWUP_EXTRA_TP: ClassVar[re.Pattern] = re.compile(r"\bTP\s*[2-9](?!\d)", re.IGNORECASE)
    SETUP_CANCEL: ClassVar[re.Pattern] = re.compile(
        r"\b(?:ANNUL\w*|CANCEL\w*|OUBLI\w*|LAISSE[SZ]?\s+TOMBER|PAS\s+DE\s+TRADE|"
        r"SETUP\s+(?:INVALID\w*|ANNUL\w*))",
        re.IGNORECASE,
    )

    @classmethod
    def get_compiled_patterns(cls) -> dict[str, list[re.Pattern]]:
        """Get all compiled regex patterns."""
        # Note: Patterns are compiled once per process and reused
        if cls._compiled_patterns_cache is None:
            cls._compiled_patterns_cache = {
                'buy_actions': [re.compile(pattern, re.IGNORECASE) for pattern in cls.BUY_ACTIONS],
                'sell_actions': [re.compile(pattern, re.IGNORECASE) for pattern in cls.SELL_ACTIONS],
                'instruments': [re.compile(pattern, re.IGNORECASE) for pattern in cls.INSTRUMENTS],
                'prices': [re.compile(pattern, re.IGNORECASE) for pattern in cls.PRICE_PATTERNS],
                'stop_losses': [re.compile(pattern, re.IGNORECASE) for pattern in cls.SL_PATTERNS],
                'take_profits': [re.compile(pattern, re.IGNORECASE) for pattern in cls.TP_PATTERNS]
            }
        return cls._compiled_patterns_cache

    @classmethod
    def build_main_patterns(cls) -> list[re.Pattern]:
        """Build comprehensive signal patterns combining all elements."""
        # Cache the main patterns similarly to get_compiled_patterns
        if cls._main_patterns_cache is None:
            patterns = []

            # Build combined patterns for different signal formats
            for action_patterns in [cls.BUY_ACTIONS, cls.SELL_ACTIONS]:
                for action_pattern in action_patterns:
                    for instrument in cls.INSTRUMENTS:
                        # Pattern: ACTION INSTRUMENT PRICE
                        pattern = f"({action_pattern})\\s+({instrument})\\s+(\\d{{4}}(?:\\.\\d{{1,2}})?)"
                        patterns.append(re.compile(pattern, re.IGNORECASE))

                        # Pattern: ACTION INSTRUMENT @ PRICE
                        pattern = f"({action_pattern})\\s+({instrument})\\s*@\\s*(\\d{{4}}(?:\\.\\d{{1,2}})?)"
                        patterns.append(re.compile(pattern, re.IGNORECASE))

            cls._main_patterns_cache = patterns

        return cls._main_patterns_cache
