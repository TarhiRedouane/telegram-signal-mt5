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

    # Preferred broker lookup order for price/tick retrieval.
    _BROKER_LOOKUP_CANDIDATES: ClassVar[dict[str, tuple[str, ...]]] = {
        "GOLD": ("GOLD", "XAUUSD", "XAU/USD", "XAUUSD.", "XAUUSD#"),
        "XAUUSD": ("XAUUSD", "GOLD", "XAU/USD", "XAUUSD.", "XAUUSD#"),
    }

    def __init__(self) -> None:
        alias_map: dict[str, str] = {}
        for canonical, aliases in self._CANONICAL_ALIASES.items():
            for alias in aliases:
                alias_map[alias.upper()] = canonical
        self._alias_to_canonical = alias_map

    def get_default_symbol(self) -> str:
        """Symbol assumed when a post names none (phase-1 profile: all enabled symbols are gold)."""
        return next(iter(self._CANONICAL_ALIASES))

    def get_enabled_aliases(self) -> list[str]:
        """Return all symbol aliases accepted by the current execution profile."""
        return list(self._alias_to_canonical.keys())

    def get_broker_lookup_candidates(self, symbol_text: str) -> list[str]:
        """Return broker symbol candidates for price/tick lookup."""
        canonical_symbol = self.canonicalize_symbol(symbol_text)

        if canonical_symbol and canonical_symbol in self._BROKER_LOOKUP_CANDIDATES:
            return list(self._BROKER_LOOKUP_CANDIDATES[canonical_symbol])

        symbol_upper = (symbol_text or "").upper().strip()
        if not symbol_upper:
            return []

        # Generic fallback for non-profile symbols.
        candidates = [
            symbol_upper,
            symbol_upper.replace("/", ""),
            f"{symbol_upper}.",
            f"{symbol_upper}#",
        ]
        deduped: list[str] = []
        for candidate in candidates:
            if candidate and candidate not in deduped:
                deduped.append(candidate)
        return deduped

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

    def get_default_sl_distance(self, symbol: str) -> Decimal | None:
        """Return fallback SL distance (price units) when a setup never states one."""
        if self.canonicalize_symbol(symbol) in {"GOLD", "XAUUSD"}:
            return settings.gold_default_sl_distance
        return None

    def get_entry_tolerance(self, symbol: str) -> Decimal | None:
        """Return price tolerance around a setup entry zone."""
        if self.canonicalize_symbol(symbol) in {"GOLD", "XAUUSD"}:
            return settings.gold_entry_tolerance
        return None

    def get_close_sl_buffer(self, symbol: str) -> Decimal | None:
        """Protective SL distance beyond a candle-close invalidation level."""
        if self.canonicalize_symbol(symbol) in {"GOLD", "XAUUSD"}:
            return settings.gold_close_sl_buffer
        return None

    def get_zone_now_max_distance(self, symbol: str) -> Decimal | None:
        """Max price distance from a zone for "buy/sell now" to join that zone's scenario."""
        if self.canonicalize_symbol(symbol) in {"GOLD", "XAUUSD"}:
            return settings.gold_zone_now_max_distance
        return None

    def get_rejection_params(self, symbol: str) -> tuple[Decimal, Decimal] | None:
        """(max sweep beyond zone edge, SL buffer beyond sweep extreme) for zone-rejection entries."""
        if self.canonicalize_symbol(symbol) in {"GOLD", "XAUUSD"}:
            return settings.gold_max_sweep, settings.gold_sl_buffer
        return None


symbol_policy = SymbolPolicy()
