"""
Settings management with environment variable loading.
Follows coding standards: use python-dotenv for secure credential handling.
"""

import os
from decimal import Decimal
from pathlib import Path
from typing import Optional, List
import re

from dotenv import load_dotenv
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings


class Settings:
    """Application settings loaded from environment variables."""

    def __init__(self, env_file: Optional[str] = None):
        """
        Initialize settings with environment variables.

        Args:
            env_file: Path to .env file (optional, defaults to .env in project root)
        """
        if env_file is None:
            # Look for .env in project root
            env_file = Path(__file__).parent.parent / ".env"

        # Load environment variables from file
        load_dotenv(env_file)

        # Telegram API Configuration
        self.telegram_api_id: str = os.getenv("TELEGRAM_API_ID", "")
        self.telegram_api_hash: str = os.getenv("TELEGRAM_API_HASH", "")
        self.phone_number: str = os.getenv("PHONE_NUMBER", "")
        self.telegram_groups: str = os.getenv("TELEGRAM_GROUPS", "")

        # OpenAI Configuration
        self.openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
        self.openai_model_variant: str = os.getenv("OPENAI_MODEL_VARIANT", "gpt-5-mini")
        self.llm_reasoning_level: str = os.getenv("LLM_REASONING_LEVEL", "minimal")
        self.llm_verbosity: str = os.getenv("LLM_VERBOSITY", "low")
        self.llm_rate_limit_rpm: int = int(os.getenv("LLM_RATE_LIMIT_RPM", "100"))
        self.llm_cache_ttl_hours: int = int(os.getenv("LLM_CACHE_TTL_HOURS", "24"))
        self.llm_max_retries: int = int(os.getenv("LLM_MAX_RETRIES", "3"))

        # MetaTrader5 Configuration
        self.mt5_login: str = os.getenv("MT5_LOGIN", "")
        self.mt5_password: str = os.getenv("MT5_PASSWORD", "")
        self.mt5_server: str = os.getenv("MT5_SERVER", "")
        self.mt5_path: str = os.getenv("MT5_PATH", "")
        self.mt5_portable: bool = os.getenv("MT5_PORTABLE", "false").lower() == "true"
        # "demo" (default) or "real": the bot refuses a terminal logged in to the other kind
        self.mt5_account_mode: str = os.getenv("MT5_ACCOUNT_MODE", "demo").lower()
        self.mt5_timeout_seconds: int = int(os.getenv("MT5_TIMEOUT_SECONDS", "60"))
        self.mt5_max_connections: int = int(os.getenv("MT5_MAX_CONNECTIONS", "5"))
        self.mt5_reconnect_delay: int = int(os.getenv("MT5_RECONNECT_DELAY", "5"))

        # Signal validation
        self.gold_price_min: Decimal = Decimal(os.getenv("GOLD_PRICE_MIN", "2000"))
        self.gold_price_max: Decimal = Decimal(os.getenv("GOLD_PRICE_MAX", "10000"))
        self.gold_max_price_deviation: Decimal = Decimal(
            os.getenv("GOLD_MAX_PRICE_DEVIATION", "100")
        )
        # Price units: 10.0 = 100 pips at 0.10/pip
        self.gold_default_sl_distance: Decimal = Decimal(
            os.getenv("GOLD_DEFAULT_SL_DISTANCE", "10.0")
        )
        # Single signals: SL never closer than this to the market (2.0 = 20 pips), unless the
        # post's own stop is tighter
        self.gold_min_sl_distance: Decimal = Decimal(os.getenv("GOLD_MIN_SL_DISTANCE", "2.0"))
        self.gold_entry_tolerance: Decimal = Decimal(os.getenv("GOLD_ENTRY_TOLERANCE", "1.0"))
        # Zone rejection: deepest sweep beyond the zone edge still counted as liquidity grab,
        # and SL buffer beyond the sweep extreme (price units)
        self.gold_max_sweep: Decimal = Decimal(os.getenv("GOLD_MAX_SWEEP", "3.0"))
        self.gold_sl_buffer: Decimal = Decimal(os.getenv("GOLD_SL_BUFFER", "0.5"))
        # Candle-close invalidation ("clôture M30 sous 4121"): hard SL this far beyond the level
        # so wicks don't stop out; the trade is closed on an actual candle close beyond it
        self.gold_close_sl_buffer: Decimal = Decimal(os.getenv("GOLD_CLOSE_SL_BUFFER", "3.0"))
        # "buy now" joins a zone scenario only if price is this close to the zone
        self.gold_zone_now_max_distance: Decimal = Decimal(
            os.getenv("GOLD_ZONE_NOW_MAX_DISTANCE", "10.0")
        )

        # Zone hints ("BUY ZONE 4174-4178"): trade only after a confirmed rejection on candles
        self.rejection_reclaim_candles: int = int(os.getenv("REJECTION_RECLAIM_CANDLES", "3"))  # M1
        self.rejection_confirm_candles: int = int(os.getenv("REJECTION_CONFIRM_CANDLES", "3"))  # M3
        self.zone_watch_hours: int = int(os.getenv("ZONE_WATCH_HOURS", "8"))
        # "buy now" within this many minutes of the zone post = same scenario (one early leg)
        self.zone_now_window_minutes: int = int(os.getenv("ZONE_NOW_WINDOW_MINUTES", "30"))

        # Multi-message setup aggregation
        self.setup_idle_timeout_seconds: int = int(os.getenv("SETUP_IDLE_TIMEOUT_SECONDS", "300"))
        self.setup_price_poll_seconds: int = int(os.getenv("SETUP_PRICE_POLL_SECONDS", "5"))
        self.setup_max_legs: int = int(os.getenv("SETUP_MAX_LEGS", "4"))
        self.setup_max_lifetime_seconds: int = int(os.getenv("SETUP_MAX_LIFETIME_SECONDS", "86400"))

        # Risk-based lot sizing: % of balance risked per signal, split across legs
        self.risk_percent: Decimal = Decimal(os.getenv("RISK_PERCENT", "3.0"))

        # Risk guard: checked before every order (instant, setup leg, zone, reinforcement)
        self.max_open_risk_percent: Decimal = Decimal(os.getenv("MAX_OPEN_RISK_PERCENT", "6.0"))
        self.max_open_legs: int = int(os.getenv("MAX_OPEN_LEGS", "6"))
        self.daily_loss_limit_percent: Decimal = Decimal(
            os.getenv("DAILY_LOSS_LIMIT_PERCENT", "6.0")
        )
        self.trading_enabled: bool = os.getenv("TRADING_ENABLED", "true").lower() == "true"
        # Kill switch without restart: create this file to block new entries, delete it to resume
        self.kill_switch_file = Path(os.getenv("KILL_SWITCH_FILE", "data/STOP_TRADING"))

        # Application Settings
        self.dev_mode: bool = os.getenv("DEV_MODE", "false").lower() == "true"
        self.log_level: str = os.getenv("LOG_LEVEL", "INFO")

        # Bot instance (BOT_INSTANCE=raise): one broker profile, own data/logs/session/database
        self.instance: str = os.getenv("BOT_INSTANCE", "").strip().lower()
        if self.instance:
            self._apply_broker_profile(self.instance)

        # Paths
        self.data_dir = Path("data") / self.instance if self.instance else Path("data")
        self.log_dir = Path("logs") / self.instance if self.instance else Path("logs")
        self.session_file = self.data_dir / "telegram.session"

        # Ensure directories exist
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def _apply_broker_profile(self, key: str) -> None:
        """MT5 account of this instance from config/brokers.json (+ BROKER_<KEY>_PASSWORD in .env)."""
        from config.brokers import get_broker, password_var

        profile = get_broker(key)
        password = os.getenv(password_var(key), "")
        if not password:
            raise ValueError(f"Set {password_var(key)} in .env (password of broker '{key}')")
        self.mt5_path = profile["mt5_path"]
        self.mt5_portable = bool(profile.get("portable", False))
        self.mt5_account_mode = profile["mode"]
        self.mt5_login = str(profile["login"])
        self.mt5_server = profile["server"]
        self.mt5_password = password

    def validate_telegram_config(self) -> bool:
        """
        Validate that required Telegram configuration is present.

        Returns:
            True if all required Telegram settings are configured
        """
        required_settings = [
            self.telegram_api_id,
            self.telegram_api_hash,
            self.phone_number,
        ]
        return all(setting.strip() for setting in required_settings)

    def get_telegram_api_id_int(self) -> int:
        """
        Get Telegram API ID as integer.

        Returns:
            API ID as integer

        Raises:
            ValueError: If API ID is not a valid integer
        """
        try:
            return int(self.telegram_api_id)
        except ValueError as e:
            raise ValueError(
                f"Invalid TELEGRAM_API_ID: must be an integer, got '{self.telegram_api_id}'"
            ) from e

    def get_telegram_groups(self) -> List[str]:
        """
        Get list of Telegram groups from environment variable.

        Returns:
            List of group usernames/IDs, empty list if none configured
        """
        if not self.telegram_groups.strip():
            return []

        # Split by comma and clean up whitespace
        groups = [group.strip() for group in self.telegram_groups.split(",")]
        return [group for group in groups if group]

    def validate_group_format(self, group: str) -> bool:
        """
        Validate Telegram group username/ID format.

        Args:
            group: Group username or ID to validate

        Returns:
            True if format is valid
        """
        # Check for @username format
        if group.startswith("@"):
            # Username should be 5-32 characters, alphanumeric + underscores
            username = group[1:]
            return bool(re.match(r"^[a-zA-Z0-9_]{5,32}$", username))

        # Check for numeric ID format
        if group.isdigit() or (group.startswith("-") and group[1:].isdigit()):
            return True

        return False

    def validate_telegram_groups(self) -> tuple[bool, List[str]]:
        """
        Validate all configured Telegram groups.

        Returns:
            Tuple of (all_valid, invalid_groups)
        """
        groups = self.get_telegram_groups()
        if not groups:
            return True, []  # No groups configured is valid

        invalid_groups = []
        for group in groups:
            if not self.validate_group_format(group):
                invalid_groups.append(group)

        return len(invalid_groups) == 0, invalid_groups

    def validate_openai_config(self) -> bool:
        """
        Validate that required OpenAI configuration is present.

        Returns:
            True if OpenAI API key is configured
        """
        return bool(self.openai_api_key.strip())

    def get_openai_model(self) -> str:
        """
        Get full OpenAI model name with variant.

        Returns:
            Model name formatted for OpenAI API
        """
        return self.openai_model_variant

    def validate_mt5_config(self) -> bool:
        """
        Validate that required MT5 configuration is present.

        Returns:
            True if all required MT5 settings are configured
        """
        required_settings = [
            self.mt5_login.strip(),
            self.mt5_password.strip(),
            self.mt5_server.strip(),
        ]
        return all(required_settings)

    def get_mt5_login_int(self) -> int:
        """
        Get MT5 login as integer.

        Returns:
            Login as integer

        Raises:
            ValueError: If login is not a valid positive integer
        """
        try:
            login = int(self.mt5_login)
            if login <= 0:
                raise ValueError("MT5 login must be a positive integer")
            return login
        except ValueError as e:
            raise ValueError(
                f"Invalid MT5_LOGIN: must be a positive integer, got '{self.mt5_login}'"
            ) from e

    def get_mt5_settings(self) -> "MT5Settings":
        """
        Get MT5 settings as Pydantic model with validation.

        Returns:
            Validated MT5Settings instance

        Raises:
            ValidationError: If MT5 configuration is invalid
        """
        return MT5Settings(
            login=self.get_mt5_login_int(),
            password=self.mt5_password,
            server=self.mt5_server,
            path=self.mt5_path or None,
            portable=self.mt5_portable,
            account_mode=self.mt5_account_mode,
            timeout_seconds=self.mt5_timeout_seconds,
            max_connections=self.mt5_max_connections,
            reconnect_delay=self.mt5_reconnect_delay,
        )


class MT5Settings(BaseSettings):
    """MetaTrader5 connection settings with validation."""

    login: int = Field(..., description="MT5 account login number")
    password: str = Field(..., description="MT5 account password")
    server: str = Field(..., description="MT5 broker server name")
    path: Optional[str] = Field(None, description="MT5 installation path")
    portable: bool = Field(
        False, description="Terminal runs in /portable mode (data inside its folder)"
    )
    account_mode: str = Field("demo", description="Expected account kind: demo or real")
    timeout_seconds: int = Field(60, ge=10, le=300, description="Connection timeout")
    max_connections: int = Field(5, ge=1, le=10, description="Connection pool size")
    reconnect_delay: int = Field(5, ge=1, le=60, description="Base reconnection delay")

    model_config = {"env_prefix": "MT5_", "case_sensitive": False}

    @field_validator("account_mode")
    @classmethod
    def validate_account_mode(cls, v):
        if v not in ("demo", "real"):
            raise ValueError(f"MT5_ACCOUNT_MODE must be 'demo' or 'real', got '{v}'")
        return v

    @field_validator("login")
    @classmethod
    def validate_login(cls, v):
        """Validate MT5 login is a positive integer."""
        if v <= 0:
            raise ValueError("MT5 login must be a positive integer")
        return v

    @field_validator("password")
    @classmethod
    def validate_password(cls, v):
        """Validate MT5 password is not empty."""
        if not v or not v.strip():
            raise ValueError("MT5 password cannot be empty")
        return v.strip()

    @field_validator("server")
    @classmethod
    def validate_server(cls, v):
        """Validate MT5 server name format."""
        if not v or not v.strip():
            raise ValueError("MT5 server cannot be empty")
        # Basic server name validation
        if not re.match(r"^[a-zA-Z0-9\-_.]+$", v.strip()):
            raise ValueError("MT5 server name contains invalid characters")
        return v.strip()


# Global settings instance
settings = Settings()
