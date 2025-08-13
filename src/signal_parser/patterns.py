"""Regex patterns for French trading signal parsing."""

import re
from typing import ClassVar


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
        r"J'ACHÈTE",
        r"LONG"
    ]

    SELL_ACTIONS: ClassVar[list[str]] = [
        r"JE\s+VEND[S]?",
        r"SELL",
        r"SHORT",
        r"VENDRE",
        r"JE\s+VENDS"
    ]

    # Instrument patterns (gold variations)
    INSTRUMENTS: ClassVar[list[str]] = [
        r"GOLD",
        r"XAUUSD",
        r"OR",  # French for gold
        r"XAU/USD",
        r"XAU"
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
