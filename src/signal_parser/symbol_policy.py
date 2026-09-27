"""Symbol policy and validation helpers for signal parsing and runtime checks."""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

from config.settings import settings

if TYPE_CHECKING:
    from decimal import Decimal


class SymbolPolicy:
    """Centralized symbol normalization and validation policy."""

    # Phase 1 profile: only GOLD/XAU aliases are enabled for execution.
    _CANONICAL_ALIASES: ClassVar[dict[str, tuple[str, ...]]] = {
        # Keep phase-1 behavior parity with the existing parser normalization.
        "GOLD": ("GOLD", "OR"),
        "XAUUSD": ("XAUUSD", "XAU/USD", "XAU"),
    }

    def __init__(self) -> None:
        alias_map: dict[str, str] = {}
        for canonical, aliases in self._CANONICAL_ALIASES.items():
            for alias in aliases:
                alias_map[alias.upper()] = canonical
        self._alias_to_canonical = alias_map

    def get_enabled_aliases(self) -> list[str]:
        """Return all symbol aliases accepted by the current execution profile."""
        return list(self._alias_to_canonical.keys())

    def get_enabled_canonical_symbols(self) -> list[str]:
        """Return canonical symbols enabled for execution."""
        return list(self._CANONICAL_ALIASES.keys())

    def canonicalize_symbol(self, symbol_text: str) -> str | None:
        """Map any supported alias to its canonical symbol."""
        if not symbol_text:
            return None
        return self._alias_to_canonical.get(symbol_text.upper().strip())

    def is_enabled_symbol(self, symbol_text: str) -> bool:
        """Check whether a symbol or alias is enabled in current profile."""
        return self.canonicalize_symbol(symbol_text) is not None

    def validate_entry_price(self, symbol: str, entry_price: Decimal) -> str | None:
        """Validate entry price with symbol-specific rules, returning an error if invalid."""
        canonical_symbol = self.canonicalize_symbol(symbol)
        if canonical_symbol is None:
            return (
                f"Unsupported symbol '{symbol}'. Enabled symbols: "
                f"{', '.join(self.get_enabled_canonical_symbols())}"
            )

        # Phase 1 keeps current GOLD range behavior for parity.
        if (
            canonical_symbol in {"GOLD", "XAUUSD"}
            and (entry_price < settings.gold_price_min or entry_price > settings.gold_price_max)
        ):
            return (
                f"GOLD price {entry_price} outside valid range "
                f"({settings.gold_price_min}-{settings.gold_price_max})"
            )

        return None

    def get_max_live_price_deviation(self, symbol: str) -> Decimal | None:
        """Return maximum allowed live-price deviation for the symbol."""
        canonical_symbol = self.canonicalize_symbol(symbol)
        if canonical_symbol in {"GOLD", "XAUUSD"}:
            return settings.gold_max_price_deviation
        return None


symbol_policy = SymbolPolicy()
