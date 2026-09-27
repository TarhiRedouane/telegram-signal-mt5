"""Unit tests for symbol policy and phase-1 symbol enablement behavior."""

from decimal import Decimal

from src.signal_parser.symbol_policy import symbol_policy


class TestSymbolPolicy:
    """Validate symbol policy mappings and constraints."""

    def test_canonicalize_symbol_maps_known_aliases(self):
        """Known aliases should map to their canonical symbol."""
        assert symbol_policy.canonicalize_symbol("OR") == "GOLD"
        assert symbol_policy.canonicalize_symbol("gold") == "GOLD"
        assert symbol_policy.canonicalize_symbol("XAU") == "XAUUSD"
        assert symbol_policy.canonicalize_symbol("xau/usd") == "XAUUSD"

    def test_canonicalize_symbol_rejects_unknown(self):
        """Unknown symbols should not be enabled in phase 1 profile."""
        assert symbol_policy.canonicalize_symbol("BTCUSD") is None
        assert symbol_policy.canonicalize_symbol("EURUSD") is None

    def test_validate_entry_price_for_supported_symbol(self):
        """GOLD/XAUUSD should use current range checks."""
        assert symbol_policy.validate_entry_price("GOLD", Decimal("3365")) is None
        assert symbol_policy.validate_entry_price("XAUUSD", Decimal("3365")) is None

    def test_validate_entry_price_rejects_unsupported(self):
        """Unsupported symbols should fail with clear reason."""
        error = symbol_policy.validate_entry_price("BTCUSD", Decimal("50000"))
        assert error is not None
        assert "Unsupported symbol" in error
