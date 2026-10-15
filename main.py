#!/usr/bin/env python3
"""
Main application entry point for Telegram Signal EA.
Orchestrates the entire signal processing and trading pipeline with graceful shutdown handling.
"""

import asyncio
import json
import logging
import signal
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent / "src"))

from config.logging_config import get_logger, setup_logging
from config.settings import settings
from src.monitoring.health_checker import HealthMonitor
from src.monitoring.metrics_collector import MetricsCollector
from src.monitoring.console_dashboard import ConsoleDashboard
from src.mt5_executor.connection import MT5ConnectionManager
from src.mt5_executor.position_manager import PositionManager
from src.telegram_client.auth_manager import AuthManager
from src.telegram_client.message_handler import MessageHandler
from src.signal_parser.regex_parser import RegexParser
from src.signal_parser.llm_parser import LLMParser
from src.signal_parser.symbol_policy import symbol_policy
from src.database import (
    close_database,
    get_database_manager,
    get_position_repository,
    get_signal_repository,
    initialize_database,
)
from src.database.models import PositionStatus, SignalStatus
from src.correlation_engine.correlator import (
    CorrelationEngine,
    TelegramMessage as CorrelationMessage,
)
from src.correlation_engine.rejection_detector import (
    Candle,
    Phase,
    RejectionParams,
    RejectionState,
    advance,
)
from src.correlation_engine.setup_aggregator import (
    Followup,
    PendingSetup,
    SetupAggregator,
    ZoneEntry,
    ZoneWatch,
    action_side,
    extract_followup,
    extract_zone_hint,
    has_action_word,
    looks_like_new_trade,
)
from src.signal_parser import ParsedAction
from src.signal_parser.patterns import close_timeframe, is_candle_close_remark, sl_far
from src.risk_manager import risk_guard
from src.risk_manager.position_sizer import leg_volume
from src.utils.currency_config import currency_config
from src.utils.formatters import normalize_price
from decimal import Decimal
from telethon.utils import get_peer_id

TRADE_RETCODE_NO_CHANGES = 10025  # MetaTrader5.TRADE_RETCODE_NO_CHANGES ("No changes in request")
BE_RETRY_ERROR_AFTER = 10  # consecutive MT5 failures on one ticket before an ERROR log (once)


class ApplicationManager:
    """
    Main application manager with graceful shutdown capabilities.
    Manages all system components and handles shutdown procedures.
    """

    def __init__(self):
        self.logger = get_logger(__name__)
        self.shutdown_event = asyncio.Event()
        self.shutdown_timeout = 30  # seconds

        # System components (initialized during startup)
        self.telegram_client = None
        self.telegram_auth_manager = None
        self.message_handler: Optional[MessageHandler] = None
        self.regex_parser: Optional[RegexParser] = None
        self.llm_parser: Optional[LLMParser] = None
        self._telegram_event_filter = None
        self.mt5_connection = None
        self.mt5_connection_manager = None
        self.position_manager: Optional[PositionManager] = None
        self.health_monitor: Optional[HealthMonitor] = None
        self.metrics_collector: Optional[MetricsCollector] = None
        self.console_dashboard: Optional[ConsoleDashboard] = None
        self._database_ready = False
        self._signal_repository = None
        self._position_repository = None
        self._correlation_engine: Optional[CorrelationEngine] = None
        self._setup_aggregator = SetupAggregator()

        # Lightweight runtime telemetry for dashboard updates
        self._messages_today = 0
        self._parsed_signals_today = 0
        self._last_message_time = None
        self._seen_message_keys: set[str] = set()
        self._connected_group_entities: list[Any] = []

        # Component state for shutdown
        self._component_tasks: Dict[str, asyncio.Task] = {}
        self._queue_states: Dict[str, Any] = {}

        self.logger.info("Application manager initialized")

    async def startup(self) -> None:
        """Initialize and start all application components."""
        try:
            self.logger.info("Starting Telegram Signal EA application")

            # Do the interactive Telegram auth first so the user can complete the
            # verification flow before the monitor/dashboard starts.
            await self._initialize_runtime_components()

            # Launch the monitoring plane as soon as Telegram auth is complete.
            await self._initialize_monitoring_components()

            # Initialize persistence components in degraded-safe mode.
            await self._initialize_persistence_components()

            # Start Telegram group monitoring and message parsing.
            await self._initialize_telegram_monitoring()

            # MT5 startup can be slow; initialize it in background so the
            # dashboard is visible instead of appearing blocked.
            self._component_tasks["mt5_init"] = asyncio.create_task(
                self._initialize_mt5_connection()
            )

            self.logger.info("Application startup complete - ready to process signals")

        except Exception as e:
            self.logger.error(f"Error during startup: {e}", exc_info=True)
            raise

    async def _initialize_runtime_components(self) -> None:
        """Initialize core runtime dependencies that must be ready before monitoring."""
        self.logger.info("Initializing runtime components")

        if not settings.validate_telegram_config():
            raise ValueError(
                "Telegram configuration is incomplete. Check TELEGRAM_API_ID, TELEGRAM_API_HASH, and PHONE_NUMBER."
            )

        self.telegram_auth_manager = AuthManager()
        telegram_ready = await self.telegram_auth_manager.startup_authentication()
        if not telegram_ready:
            self.logger.warning(
                "Telegram authentication did not complete successfully during startup"
            )
            startup_reason = (
                self.telegram_auth_manager.last_startup_error or "Unknown Telegram startup error"
            )
            raise RuntimeError(
                f"Telegram startup failed: {startup_reason}. "
                "Check logs/app.log and ensure the Telegram session is not used by another process."
            )

        self.telegram_client = self.telegram_auth_manager.telegram_client
        self.logger.info("Telegram runtime initialized")

    async def _initialize_telegram_monitoring(self) -> None:
        """Connect to configured groups and register Telegram message handlers."""
        if self.telegram_client is None or self.telegram_client.client is None:
            self.logger.warning("Telegram client unavailable; skipping message monitoring setup")
            return

        try:
            successful_groups: list[str] = []
            max_attempts = 3

            for attempt in range(1, max_attempts + 1):
                connection_results = await self.telegram_client.connect_to_groups()
                successful_groups = [
                    group for group, result in connection_results.items() if result.get("success")
                ]

                if successful_groups:
                    break

                if attempt < max_attempts:
                    wait_seconds = float(attempt)
                    self.logger.warning(
                        "No Telegram groups connected (attempt "
                        f"{attempt}/{max_attempts}); retrying in {wait_seconds:.1f}s"
                    )
                    await asyncio.sleep(wait_seconds)

            if not successful_groups:
                self.logger.warning(
                    "No Telegram groups connected after retries. "
                    "If logs show 'database is locked', ensure only one app instance is running."
                )
                return

            self.message_handler = MessageHandler()
            self.regex_parser = RegexParser()
            if settings.validate_openai_config():
                try:
                    from src.database.cache import llm_cache

                    await llm_cache.initialize()
                    self.llm_parser = LLMParser()
                except Exception as e:
                    self.logger.warning(f"LLM parser init skipped: {e}")
            self.message_handler.set_message_callback(self._on_telegram_message)

            group_entities = self.telegram_client.get_connected_group_entities()
            self._connected_group_entities = group_entities
            self._telegram_event_filter = self.message_handler.create_event_handler(group_entities)
            self.telegram_client.client.add_event_handler(
                self.message_handler.handle_new_message, self._telegram_event_filter
            )
            self.telegram_client.client.add_event_handler(
                self.message_handler.handle_new_message,
                self.message_handler.create_edit_event_handler(group_entities),
            )

            # Fallback path: poll connected groups so we still ingest messages
            # when Telegram push events are delayed or missed.
            self._component_tasks["telegram_polling"] = asyncio.create_task(
                self._poll_telegram_groups()
            )
            self.logger.info("Telegram polling fallback task scheduled")

            self.logger.info(
                f"Telegram monitoring active for {len(successful_groups)} group(s): {successful_groups}"
            )
        except Exception as e:
            self.logger.error(f"Failed to initialize Telegram monitoring: {e}", exc_info=True)

    async def _initialize_persistence_components(self) -> None:
        """Initialize database and repositories needed for signal persistence."""
        try:
            await initialize_database(run_migrations=False)
            self._signal_repository = get_signal_repository()
            self._position_repository = get_position_repository()
            self._correlation_engine = CorrelationEngine(
                get_database_manager(),
                self.telegram_client,
            )
            self._database_ready = True
            self.logger.info("Persistence components initialized")
        except Exception as e:
            self._database_ready = False
            self._signal_repository = None
            self.logger.warning(
                f"Persistence initialization failed, continuing in degraded mode: {e}"
            )

    async def _on_telegram_message(self, metadata: Dict[str, Any]) -> None:
        """Parse incoming Telegram messages and push actionable signals to dashboard."""
        message_key = (
            f"{metadata.get('telegram_chat_id', 0)}_"
            f"{metadata.get('telegram_message_id', 0)}_{metadata.get('edit_date')}"
        )
        if message_key in self._seen_message_keys:
            return

        self._seen_message_keys.add(message_key)
        if len(self._seen_message_keys) > 5000:
            # Keep memory bounded while preserving recent dedupe keys.
            self._seen_message_keys = set(list(self._seen_message_keys)[-2500:])

        self._messages_today += 1
        self._last_message_time = metadata.get("timestamp")

        raw_text = metadata.get("raw_text", "")
        preview = raw_text.replace("\n", " ").replace("\r", " ")[:120]
        edited = metadata.get("edit_date") is not None
        self.logger.info(
            f"Message {'edited' if edited else 'received'} chat={metadata.get('telegram_chat_id')} "
            f"message_id={metadata.get('telegram_message_id')} text='{preview}'"
        )

        chat_id = int(metadata.get("telegram_chat_id", 0))
        zone_hint = extract_zone_hint(raw_text)
        if zone_hint is not None:
            # "BUY ZONE 4174–4178": no order now, wait for a confirmed rejection in the zone
            self._start_zone_watch(metadata, *zone_hint)
            return
        pending_setup = self._setup_aggregator.get(chat_id)
        zone_watch = self._setup_aggregator.get_zone(chat_id)
        side = action_side(raw_text)
        if (
            zone_watch is not None
            and side is not None
            and extract_followup(raw_text).now
            and any(zone.action == side for zone in zone_watch.zones)
        ):
            # "buy now" next to a waiting zone = same scenario: one early leg, the zone keeps the rest
            await self._zone_now_leg(zone_watch, side)
            return
        if zone_watch is not None and not looks_like_new_trade(raw_text):
            followup = extract_followup(raw_text)
            if followup.cancel:
                self._setup_aggregator.drop_zone(zone_watch, "cancelled by author")
                if pending_setup is None:
                    return
            elif pending_setup is None and self._apply_zone_followup(zone_watch, followup):
                return

        if (
            edited
            and pending_setup is not None
            and pending_setup.message_id == int(metadata.get("telegram_message_id", 0))
        ):
            await self._handle_setup_edit(pending_setup, raw_text)
            return
        if (
            edited
            and self._signal_repository is not None
            and await self._signal_repository.find_by_message_id(
                int(metadata.get("telegram_message_id", 0)), chat_id=chat_id
            )
        ):
            # ponytail: edits of executed signals are not applied to their positions yet
            self.logger.warning(
                f"Edit of already-processed signal message_id={metadata.get('telegram_message_id')} "
                "ignored; adjust the position manually if levels changed"
            )
            return
        if pending_setup is not None and not looks_like_new_trade(raw_text):
            await self._handle_setup_followup(pending_setup, raw_text)
            return

        started_at = asyncio.get_running_loop().time()
        if self._correlation_engine is not None:
            timestamp = metadata.get("timestamp") or datetime.now(UTC)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
            correlation_message = CorrelationMessage(
                telegram_message_id=int(metadata.get("telegram_message_id", 0)),
                telegram_chat_id=int(metadata.get("telegram_chat_id", 0)),
                sender=str(metadata.get("sender", "unknown")),
                timestamp=timestamp,
                raw_text=raw_text,
                reply_to_message_id=metadata.get("reply_to_message_id"),
            )
            correlated_position = await self._correlation_engine.correlate_message(
                correlation_message
            )
            if correlated_position is not None:
                await self._handle_correlated_message(metadata, correlated_position)
                latency_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)
                success_rate = (self._parsed_signals_today / self._messages_today) * 100
                if self.console_dashboard is not None:
                    self.console_dashboard.set_message_stats(
                        messages_today=self._messages_today,
                        last_message_time=self._last_message_time,
                        success_rate=round(success_rate, 2),
                        processing_latency_ms=latency_ms,
                    )
                return

        parsed_signal = self.regex_parser.parse(raw_text) if self.regex_parser else None

        if parsed_signal is None and self.llm_parser is not None:
            try:
                parsed_signal = await self.llm_parser.parse_with_llm(raw_text)
            except Exception as e:
                self.logger.warning(f"LLM fallback parse failed: {e}")
            # No buy/sell word -> direction unknown; never open a trade on the LLM's guess.
            if (
                parsed_signal is not None
                and parsed_signal.parsed_action in (ParsedAction.BUY, ParsedAction.SELL)
                and not has_action_word(raw_text)
            ):
                self.logger.warning(
                    f"LLM {parsed_signal.parsed_action.value} discarded, no buy/sell word "
                    f"chat={metadata.get('telegram_chat_id')} message_id={metadata.get('telegram_message_id')}"
                )
                parsed_signal = None
            if (
                parsed_signal is not None
                and parsed_signal.parsed_action == ParsedAction.CLOSE
                and is_candle_close_remark(raw_text)
            ):
                self.logger.info(
                    f"LLM CLOSE discarded, candle-close remark not a trade close: {raw_text[:60]!r}"
                )
                parsed_signal = None

        latency_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)

        if parsed_signal:
            parsed_signal.telegram_message_id = int(metadata.get("telegram_message_id", 0))
            parsed_signal.telegram_chat_id = int(metadata.get("telegram_chat_id", 0))
            parsed_signal.sender = str(metadata.get("sender", "unknown"))
            parsed_signal.timestamp = metadata.get("timestamp")

            if parsed_signal.parsed_action not in (ParsedAction.BUY, ParsedAction.SELL):
                # MODIFY/CLOSE reaching here matched no open trade or setup: nothing to act on
                self.logger.info(
                    f"{parsed_signal.parsed_action.value} ignored, no open trade or setup linked: "
                    f"chat={metadata.get('telegram_chat_id')} message_id={metadata.get('telegram_message_id')}"
                )
            elif await self._validate_live_price(parsed_signal):
                self._parsed_signals_today += 1
                if parsed_signal.parsed_action in (ParsedAction.BUY, ParsedAction.SELL) and (
                    parsed_signal.stop_loss is None
                    or parsed_signal.take_profit is None
                    or len(parsed_signal.take_profits) > 1  # multi-TP -> one leg per TP
                    # SL range / candle-close invalidation: only the setup tracks both levels
                    or sl_far(parsed_signal.raw_text) is not None
                    or close_timeframe(parsed_signal.raw_text) is not None
                ):
                    self._start_setup(parsed_signal)
                elif await self._use_live_price_for_sizing(parsed_signal):
                    await self._handle_parsed_signal(parsed_signal)
                else:
                    self.logger.info(
                        f"Signal rejected - no usable live price for sizing chat={metadata.get('telegram_chat_id')} "
                        f"message_id={metadata.get('telegram_message_id')}"
                    )
            else:
                self.logger.info(
                    f"Signal rejected - live price check failed for chat={metadata.get('telegram_chat_id')} "
                    f"message_id={metadata.get('telegram_message_id')}"
                )
        else:
            self.logger.info(
                f"Parse failed - no patterns matched for chat={metadata.get('telegram_chat_id')} "
                f"message_id={metadata.get('telegram_message_id')}"
            )

        success_rate = (self._parsed_signals_today / self._messages_today) * 100
        if self.console_dashboard is not None:
            self.console_dashboard.set_message_stats(
                messages_today=self._messages_today,
                last_message_time=self._last_message_time,
                success_rate=round(success_rate, 2),
                processing_latency_ms=latency_ms,
            )

    async def _handle_correlated_message(self, metadata: Dict[str, Any], position) -> None:
        """Analyze a correlated follow-up and apply confident SL/TP updates."""
        if self._signal_repository is None or self.mt5_connection_manager is None:
            self.logger.info(
                f"Correlated follow-up {metadata.get('telegram_message_id')} linked to "
                f"position {position.mt5_ticket}; context execution unavailable"
            )
            return

        if not settings.validate_openai_config():
            self.logger.info(
                "Correlated follow-up stored; OpenAI context analysis is not configured"
            )
            return

        try:
            from src.signal_parser.context_analyzer import (
                ContextAnalyzer,
                TelegramMessage as ContextMessage,
            )

            signal = await self._signal_repository.get_by_id(position.signal_id)
            if signal is None:
                self.logger.warning(
                    f"Could not load parent signal for correlated position {position.id}"
                )
                return
            position.signal = signal

            timestamp = metadata.get("timestamp") or datetime.now(UTC)
            if timestamp.tzinfo is not None:
                timestamp = timestamp.astimezone(UTC).replace(tzinfo=None)
            message = ContextMessage(
                telegram_message_id=int(metadata.get("telegram_message_id", 0)),
                telegram_chat_id=int(metadata.get("telegram_chat_id", 0)),
                sender=str(metadata.get("sender", "unknown")),
                timestamp=timestamp,
                raw_text=str(metadata.get("raw_text", "")),
                reply_to_message_id=metadata.get("reply_to_message_id"),
                chat_title=str(metadata.get("chat_title", "")),
                is_forwarded=bool(metadata.get("is_forwarded", False)),
                message_type=str(metadata.get("message_type", "text")),
            )
            parent_message = ContextMessage(
                telegram_message_id=signal.telegram_message_id,
                telegram_chat_id=signal.telegram_chat_id,
                sender=signal.sender,
                timestamp=signal.timestamp,
                raw_text=signal.raw_text,
            )
            analysis = await ContextAnalyzer().analyze_update_context(
                message,
                parent_message,
                [position],
            )
            if analysis is None:
                self.logger.info(
                    f"Correlated follow-up {message.telegram_message_id} stored without "
                    "an actionable context interpretation"
                )
                return

            if analysis.action != "modify" or analysis.confidence < 0.75:
                self.logger.info(
                    f"Correlated follow-up {message.telegram_message_id} not auto-applied: "
                    f"action={analysis.action}, confidence={analysis.confidence:.2f}"
                )
                return

            if analysis.target_position is not None and str(analysis.target_position) != str(
                position.mt5_ticket
            ):
                self.logger.warning(
                    f"LLM target {analysis.target_position} does not match correlated "
                    f"ticket {position.mt5_ticket}; update not applied"
                )
                return

            new_sl = analysis.parameters.get("new_sl")
            new_tp = analysis.parameters.get("new_tp")
            if new_sl is None and new_tp is None:
                self.logger.info(
                    f"Correlated follow-up {message.telegram_message_id} had no SL/TP values"
                )
                return

            action_name = getattr(signal.parsed_action, "value", str(signal.parsed_action)).upper()
            reference_price = float(position.open_price or signal.entry_price or 0)
            if (
                new_sl is not None
                and (
                    float(new_sl) <= reference_price
                    if action_name == "SELL"
                    else float(new_sl) >= reference_price
                )
            ) or (
                new_tp is not None
                and (
                    float(new_tp) >= reference_price
                    if action_name == "SELL"
                    else float(new_tp) <= reference_price
                )
            ):
                self.logger.warning(
                    f"Rejected correlated levels with invalid direction for {action_name} "
                    f"position {position.mt5_ticket}: SL={new_sl}, TP={new_tp}"
                )
                return

            if await self._apply_position_levels(position, new_sl, new_tp):
                self.logger.info(f"Correlated update confidence={analysis.confidence:.2f}")
        except Exception as exc:
            self.logger.error(
                f"Failed to process correlated follow-up "
                f"{metadata.get('telegram_message_id')}: {exc}",
                exc_info=True,
            )

    async def _apply_position_levels(self, position, new_sl, new_tp) -> bool:
        """Send SL/TP change to MT5 and persist it on the position record."""
        if self.mt5_connection_manager is None:
            self.logger.warning(f"Cannot modify ticket {position.mt5_ticket}: MT5 not connected")
            return False
        self.position_manager = self.position_manager or PositionManager(
            self.mt5_connection_manager
        )
        update_result = await self.position_manager.modify_position_levels(
            ticket=position.mt5_ticket,
            new_sl=float(new_sl) if new_sl is not None else None,
            new_tp=float(new_tp) if new_tp is not None else None,
        )
        # NO_CHANGES: MT5 already holds these levels, so they are in force
        if (
            not update_result.success
            and getattr(update_result, "retcode", None) != TRADE_RETCODE_NO_CHANGES
        ):
            self.logger.warning(
                f"Failed to apply levels to ticket {position.mt5_ticket}: "
                f"{update_result.error_message}"
            )
            return False

        position_update = {}
        if new_sl is not None:
            position_update["current_sl"] = float(new_sl)
        if new_tp is not None:
            position_update["current_tp"] = float(new_tp)
        if self._position_repository is not None:
            await self._position_repository.update_position_mt5_data(position.id, position_update)
        self.logger.info(
            f"Applied levels to ticket {position.mt5_ticket}: SL={new_sl}, TP={new_tp}"
        )
        return True

    def _start_setup(self, parsed_signal) -> None:
        """Buffer an incomplete BUY/SELL setup and watch price until it can fire."""
        setup, rearm_changes = self._setup_aggregator.start(parsed_signal, datetime.now(UTC))
        zone_watch = self._setup_aggregator.get_zone(setup.chat_id)
        if zone_watch is not None and any(zone.action != setup.action for zone in zone_watch.zones):
            self._setup_aggregator.drop_zone(zone_watch, f"opposite {setup.action.value} signal")
        state = (
            "started" if rearm_changes is None else f"re-armed (positions={len(setup.positions)})"
        )
        self.logger.info(
            f"Setup {state} chat={setup.chat_id} message_id={setup.message_id}: "
            f"{setup.action.value} {setup.symbol} {setup.low}-{setup.high} SL={setup.sl} TPs={setup.tps} "
            f"now={setup.fire_now}"
        )
        if rearm_changes is None:
            self._component_tasks[f"setup_{setup.chat_id}"] = asyncio.create_task(
                self._watch_setup(setup)
            )
        elif "sl" in rearm_changes or "tp" in rearm_changes:
            asyncio.create_task(self._apply_setup_levels(setup, rearm_changes))

    def _start_zone_watch(
        self, metadata: Dict[str, Any], symbol: str, zones: list, targets: list
    ) -> None:
        """Track zone hints until a rejection is confirmed on candles (see rejection_detector)."""
        if symbol_policy.get_rejection_params(symbol) is None:
            self.logger.warning(f"Zone hint ignored: no rejection params for {symbol}")
            return
        watch = ZoneWatch(
            chat_id=int(metadata.get("telegram_chat_id", 0)),
            message_id=int(metadata.get("telegram_message_id", 0)),
            sender=str(metadata.get("sender", "unknown")),
            timestamp=metadata.get("timestamp") or datetime.now(UTC),
            symbol=symbol,
            zones=[
                ZoneEntry(
                    action,
                    low,
                    high,
                    RejectionState(sell=action == ParsedAction.SELL, low=low, high=high),
                )
                for action, low, high in zones
            ],
            targets=targets,
            created=datetime.now(UTC),
            raw_text=metadata.get("raw_text", ""),
        )
        self._setup_aggregator.start_zone(watch)
        self.logger.info(
            f"Zone watch started chat={watch.chat_id} message_id={watch.message_id} {symbol}: "
            f"{[f'{z.action.value} {z.low}-{z.high}' for z in watch.zones]} targets={targets}"
        )
        self._component_tasks[f"zone_{watch.chat_id}"] = asyncio.create_task(
            self._watch_zone(watch)
        )

    def _apply_zone_followup(self, watch: ZoneWatch, followup: Followup) -> bool:
        """Targets/SL for a zone still waiting for its rejection. True when the message was about levels."""
        if not (
            followup.tps
            or followup.tp_slots
            or followup.final_tp is not None
            or followup.sl is not None
        ):
            return False
        changed = watch.apply(followup)
        self.logger.info(
            f"Zone watch updated chat={watch.chat_id} {changed or 'no change'}: "
            f"targets={watch.targets} SL={watch.sl}"
        )
        return True

    async def _watch_zone(self, watch: ZoneWatch) -> None:
        """Feed closed M1/M3 candles to each zone's rejection detector; trade on confirmation."""
        max_sweep, sl_buffer = symbol_policy.get_rejection_params(watch.symbol)
        params = RejectionParams(
            max_sweep=max_sweep,
            reclaim_candles=settings.rejection_reclaim_candles,
            confirm_candles=settings.rejection_confirm_candles,
        )
        deadline = watch.created + timedelta(hours=settings.zone_watch_hours)
        try:
            while self._setup_aggregator.get_zone(watch.chat_id) is watch:
                if datetime.now(UTC) > deadline:
                    self._setup_aggregator.drop_zone(watch, "expired")
                    return
                if all(zone.state.done for zone in watch.zones):
                    self._setup_aggregator.drop_zone(watch, "all zones resolved")
                    return
                if self.mt5_connection_manager is not None:
                    m1 = await self.mt5_connection_manager.get_closed_rates(watch.symbol, 1, 30)
                    m3 = await self.mt5_connection_manager.get_closed_rates(watch.symbol, 3, 30)
                    if m1 and m3:
                        await self._advance_zones(
                            watch,
                            [Candle(*r) for r in m1],
                            [Candle(*r) for r in m3],
                            params,
                            sl_buffer,
                        )
                await asyncio.sleep(settings.setup_price_poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(f"Zone watcher failed for chat {watch.chat_id}: {exc}", exc_info=True)
            self._setup_aggregator.drop_zone(watch, "watcher error")

    async def _advance_zones(
        self, watch: ZoneWatch, m1: list, m3: list, params: RejectionParams, sl_buffer: Decimal
    ) -> None:
        last = m1[-1]
        now = datetime.now(UTC)
        if watch.last_heartbeat is None or now - watch.last_heartbeat >= timedelta(minutes=10):
            watch.last_heartbeat = now
            self.logger.info(
                f"Zone watch alive chat={watch.chat_id} last M1 close={last.close}: "
                + "; ".join(
                    f"{z.action.value} {z.low}-{z.high} {z.state.phase.value}" for z in watch.zones
                )
            )
        for index, zone in enumerate(watch.zones):
            if zone.state.done:
                continue
            if not zone.touched and last.low <= zone.high and last.high >= zone.low:
                zone.touched = True
                self.logger.info(
                    f"Zone {zone.action.value} {zone.low}-{zone.high} touched chat={watch.chat_id}: "
                    f"M1 {last.low}-{last.high}, watching for sweep/rejection"
                )
            before = zone.state.phase
            advance(zone.state, m1, m3, params)
            if zone.state.phase == before:
                continue
            self.logger.info(
                f"Zone {zone.action.value} {zone.low}-{zone.high} chat={watch.chat_id}: "
                f"{before.value} -> {zone.state.phase.value} extreme={zone.state.extreme} {zone.state.reason}"
            )
            if zone.state.phase == Phase.CONFIRMED:
                await self._on_zone_confirmed(watch, index, zone, sl_buffer)

    def _zone_setup(
        self,
        watch: ZoneWatch,
        zone: ZoneEntry,
        index: int,
        sl_level: Decimal | None,
        close_tf: int | None,
    ) -> PendingSetup:
        """Setup for a zone of `watch`: post's targets as TPs (nearest first), fires on its watcher's next poll."""
        sell = zone.action == ParsedAction.SELL
        tps = sorted(
            {t for t in watch.targets if (t < zone.low if sell else t > zone.high)}, reverse=sell
        )
        # ponytail: signals are unique per (chat, message id); once the post's id is used, later orders
        # of the same post get a synthetic negative id. Give positions their own key if replies need it.
        used = watch.fills > 0 or watch.linked_setup is not None
        setup = PendingSetup(
            chat_id=watch.chat_id,
            message_id=-(watch.message_id * 10 + index) if used else watch.message_id,
            sender=watch.sender,
            timestamp=watch.timestamp,
            action=zone.action,
            symbol=watch.symbol,
            low=zone.low,
            high=zone.high,
            tps=tps,
            raw_texts=[watch.raw_text],
            last_update=datetime.now(UTC),
            fire_now=True,
        )
        if sl_level is not None:
            setup.set_sl(sl_level, close_tf)
        return setup

    def _author_zone_sl(
        self, watch: ZoneWatch, zone: ZoneEntry
    ) -> tuple[Decimal | None, int | None]:
        if watch.sl is not None and zone.sl_ok(watch.sl):
            return watch.sl, watch.close_tf
        return None, None

    async def _zone_now_leg(self, watch: ZoneWatch, side: ParsedAction) -> None:
        """ "buy now" close (time + price) to a waiting zone: open ONE leg now; the zone's confirmed
        rejection opens the remaining legs (one per target, or one more leg with a single target).
        """
        index, zone = next(
            ((i, z) for i, z in enumerate(watch.zones) if z.action == side and not z.state.done),
            (None, None),
        )
        if zone is None:
            self.logger.info(
                f"'{side.value} now' ignored chat={watch.chat_id}: no open {side.value} zone"
            )
            return
        if (
            watch.linked_setup is not None
            and self._setup_aggregator.get(watch.chat_id) is watch.linked_setup
        ):
            self.logger.info(
                f"'{side.value} now' ignored chat={watch.chat_id}: early leg already taken"
            )
            return
        age = datetime.now(UTC) - watch.created
        if age > timedelta(minutes=settings.zone_now_window_minutes):
            self.logger.info(
                f"'{side.value} now' ignored chat={watch.chat_id}: zone post is {age} old"
            )
            return
        price = (
            await self.mt5_connection_manager.get_current_price(watch.symbol)
            if self.mt5_connection_manager is not None
            else None
        )
        max_distance = symbol_policy.get_zone_now_max_distance(watch.symbol)
        if (
            price is None
            or max_distance is None
            or watch.distance(zone, Decimal(str(price))) > max_distance
        ):
            self.logger.info(
                f"'{side.value} now' ignored chat={watch.chat_id}: price {price} too far from zone "
                f"{zone.low}-{zone.high} (max {max_distance})"
            )
            return

        setup = self._zone_setup(watch, zone, index, *self._author_zone_sl(watch, zone))
        setup.planned_legs = (
            min(len(setup.tps), settings.setup_max_legs) if len(setup.tps) > 1 else 2
        )
        setup.hold_at = 1
        watch.linked_setup = setup
        existing = self._setup_aggregator.get(watch.chat_id)
        if existing is not None:
            self.logger.info(f"Early zone leg replaces setup message_id={existing.message_id}")
        self._setup_aggregator.adopt(setup)
        self.logger.info(
            f"'{side.value} now' joins zone {zone.low}-{zone.high} chat={watch.chat_id}: early leg 1/"
            f"{setup.planned_legs} at ~{price}, SL={setup.sl or 'default'} TPs={setup.tps}; rest on confirmed rejection"
        )
        self._component_tasks[f"setup_{setup.chat_id}"] = asyncio.create_task(
            self._watch_setup(setup)
        )

    async def _on_zone_confirmed(
        self, watch: ZoneWatch, index: int, zone: ZoneEntry, sl_buffer: Decimal
    ) -> None:
        """Rejection confirmed: market order with SL beyond the sweep and the post's reaction levels as TPs."""
        sell = zone.action == ParsedAction.SELL
        author_sl, close_tf = self._author_zone_sl(watch, zone)
        sweep_sl = zone.state.extreme + sl_buffer if sell else zone.state.extreme - sl_buffer
        sl = author_sl or sweep_sl
        now = datetime.now(UTC)
        existing = self._setup_aggregator.get(watch.chat_id)

        linked = watch.linked_setup
        if linked is not None and existing is linked:
            watch.fills += 1
            if linked.be_done or linked.be_pending:
                self.logger.info(
                    f"Zone {zone.low}-{zone.high} confirmed but TP1 already hit: no more legs"
                )
                return
            linked.hold_at = None
            linked.armed = linked.fire_now = True
            linked.last_update = now
            if linked.sl is None:  # early leg used the default SL; the sweep now gives a real one
                linked.set_sl(sweep_sl, None)
                await self._apply_setup_levels(linked, ["sl"])
            self.logger.info(
                f"Zone {zone.action.value} {zone.low}-{zone.high} confirmed chat={watch.chat_id}: "
                f"sweep {zone.state.extreme}, opening remaining legs (SL={linked.sl} TPs={linked.tps})"
            )
            return

        if (
            existing is not None
            and existing.positions
            and existing.action == zone.action
            and existing.symbol == watch.symbol
        ):
            # Reinforcement zone on a live trade: one more order; SL widened to cover both entries
            existing.reopen()
            existing.low, existing.high, existing.fire_now, existing.last_update = (
                zone.low,
                zone.high,
                True,
                now,
            )
            wider = (
                sl
                if existing.sl is None
                else (max(existing.sl, sl) if sell else min(existing.sl, sl))
            )
            self.logger.info(
                f"Zone {zone.low}-{zone.high} confirmed: reinforcement, SL {existing.sl} -> {wider}"
            )
            if wider != existing.sl:
                existing.sl = wider
                await self._apply_setup_levels(existing, ["sl"])
            watch.fills += 1
            return

        setup = self._zone_setup(watch, zone, index, sl, close_tf if author_sl else None)
        if existing is not None:
            self.logger.info(f"Zone confirmation replaces setup message_id={existing.message_id}")
        self._setup_aggregator.adopt(setup)
        watch.fills += 1
        self.logger.info(
            f"Zone {zone.action.value} {zone.low}-{zone.high} confirmed chat={watch.chat_id}: "
            f"sweep {zone.state.extreme} SL={setup.sl} TPs={setup.tps} -> executing"
        )
        self._component_tasks[f"setup_{setup.chat_id}"] = asyncio.create_task(
            self._watch_setup(setup)
        )

    async def _apply_setup_levels(self, setup: PendingSetup, changed: list[str]) -> None:
        """Push changed SL/TP to every position opened by this setup (TP per leg)."""
        for index, position in enumerate(setup.positions):
            sl = setup.sl if "sl" in changed else None
            if (
                sl is not None
                and (setup.be_done or setup.be_pending)
                and not setup.locks_profit(sl, Decimal(str(position.open_price)))
            ):
                sl = None  # after break-even only a profit-locking SL may replace it
            tp = setup.leg_tp(index) if "tp" in changed else None
            if sl is None and tp is None:
                continue
            applied = await self._apply_position_levels(position, sl, tp)
            if applied and sl is not None:
                # After TP1 this SL locks profit (checked above): a pending break-even retry would loosen it
                setup.retry_counts.pop(("be", position.mt5_ticket), None)

    def _disarm_or_drop(self, setup: PendingSetup, reason: str) -> None:
        """Stop trying to open the current entry; keep the setup if it already owns positions."""
        if setup.positions:
            setup.armed = setup.fire_now = False
            self.logger.info(f"Setup entry abandoned chat={setup.chat_id}: {reason}")
        else:
            self._setup_aggregator.drop(setup, reason)

    async def _handle_setup_edit(self, setup: PendingSetup, raw_text: str) -> None:
        """Author edited the setup message itself: re-read its range/SL/TP instead of treating it as chatter."""
        signal = self.regex_parser.parse(raw_text) if self.regex_parser else None
        if signal is None or signal.parsed_action != setup.action or signal.symbol != setup.symbol:
            self.logger.info(
                f"Setup message edit in chat {setup.chat_id} ignored (not the same trade)"
            )
            return
        if not setup.positions:
            setup.low, setup.high = (
                signal.entry_price,
                signal.entry_price_high or signal.entry_price,
            )
        self.logger.info(
            f"Setup message edited chat={setup.chat_id}: zone {setup.low}-{setup.high}"
        )
        await self._handle_setup_followup(
            setup,
            raw_text,
            Followup(
                sl=signal.stop_loss,
                tps=list(signal.take_profits),
                sl_close_tf=close_timeframe(raw_text),
            ),
        )

    async def _handle_setup_followup(
        self, setup: PendingSetup, raw_text: str, followup: Followup | None = None
    ) -> None:
        """Fold a follow-up message into the chat's pending (or already-filled) setup."""
        if followup is None:
            followup = extract_followup(raw_text)
            if followup.empty and self.llm_parser is not None:
                followup = await self._llm_setup_followup(setup, raw_text)

        if followup.cancel:
            self._disarm_or_drop(setup, "cancelled by author")
            if setup.positions:
                tickets = [p.mt5_ticket for p in setup.positions]
                self.logger.warning(
                    f"Cancel after fill; tickets {tickets} left open, manage manually"
                )
            return

        if not setup.armed:
            # After a fill only an explicit "sell/buy now" or a new price opens another entry;
            # restating the filled zone ("SELL → 4197 - 4200" + levels) only updates SL/TP.
            tolerance = symbol_policy.get_entry_tolerance(setup.symbol) or Decimal(0)
            new_price = followup.entry is not None and not setup.in_entry_zone(
                followup.entry, tolerance
            )
            if has_action_word(raw_text) and (followup.now or new_price):
                setup.reopen()
            else:
                followup.now, followup.entry, followup.entry_high = False, None, None

        changed = setup.apply(followup, raw_text, datetime.now(UTC))
        if not changed:
            return
        self.logger.info(
            f"Setup updated chat={setup.chat_id} {changed}: "
            f"{setup.low}-{setup.high} SL={setup.sl} TPs={setup.tps}"
        )
        if "sl" in changed or "tp" in changed:
            await self._apply_setup_levels(setup, changed)

    async def _llm_setup_followup(self, setup: PendingSetup, raw_text: str) -> Followup:
        """LLM fallback for follow-ups regex could not classify (odd cancel/level phrasing)."""
        context = (
            f"Setup en attente: {setup.action.value} {setup.symbol} entrée {setup.low}-{setup.high}, "
            f"SL {setup.sl}, TPs {setup.tps}. Le message suivant est une suite de ce setup."
        )
        try:
            parsed = await self.llm_parser.parse_with_llm(raw_text, context=context)
        except Exception as e:
            self.logger.warning(f"LLM setup follow-up parse failed: {e}")
            return Followup()
        if parsed is None:
            return Followup()
        if parsed.parsed_action == ParsedAction.CLOSE:
            if is_candle_close_remark(
                raw_text
            ):  # "clôture dans 5": waiting for a candle, not cancelling
                return Followup()
            return Followup(cancel=True)
        same_side = parsed.parsed_action == setup.action and parsed.entry_price > 0
        return Followup(
            sl=parsed.stop_loss if isinstance(parsed.stop_loss, Decimal) else None,
            final_tp=parsed.take_profit if isinstance(parsed.take_profit, Decimal) else None,
            entry=parsed.entry_price if same_side else None,
            sl_close_tf=close_timeframe(raw_text),
        )

    async def _watch_setup(self, setup: PendingSetup) -> None:
        """Poll live price until the setup fires, expires, or is replaced/cancelled."""
        try:
            while self._setup_aggregator.get(setup.chat_id) is setup:
                now = datetime.now(UTC)
                all_closed = bool(setup.positions) and await self._track_setup_legs(setup)
                if setup.first_fill_at and now - setup.first_fill_at > timedelta(
                    seconds=settings.setup_max_lifetime_seconds
                ):
                    self._setup_aggregator.drop(setup, "max lifetime reached")
                    return
                if (
                    setup.positions
                    and setup.close_invalidation
                    and await self._check_close_invalidation(setup)
                ):
                    return
                zone = self._setup_aggregator.get_zone(setup.chat_id)
                waiting_zone = (
                    setup.hold_at is not None and zone is not None and zone.linked_setup is setup
                )
                if setup.is_expired(now, settings.setup_idle_timeout_seconds) and not waiting_zone:
                    if not setup.positions:
                        self._setup_aggregator.drop(setup, "expired before entry")
                        return
                    # A candle-close invalidation is only enforced while the setup is watched
                    watch_close = setup.close_invalidation is not None
                    # A break-even or invalidation close still being retried keeps the watcher alive
                    retrying = setup.be_pending or setup.close_pending
                    if all_closed or (
                        not retrying
                        and (setup.be_done or (len(setup.positions) <= 1 and not watch_close))
                    ):
                        self._setup_aggregator.drop(setup, "idle timeout after fill")
                        return
                    # Keep watching open legs for TP1 -> break-even; stop waiting for unfilled legs.
                    setup.armed = setup.fire_now = False
                if setup.armed and self.mt5_connection_manager is not None:
                    price = await self.mt5_connection_manager.get_current_price(setup.symbol)
                    if price is not None:
                        await self._maybe_fire_setup(setup, Decimal(str(price)))
                await asyncio.sleep(settings.setup_price_poll_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.logger.error(
                f"Setup watcher failed for chat {setup.chat_id}: {exc}", exc_info=True
            )
            self._setup_aggregator.drop(setup, "watcher error")

    async def _check_close_invalidation(self, setup: PendingSetup) -> bool:
        """Close every leg when a candle of the invalidation timeframe CLOSES beyond the level
        (wicks don't count; the protective SL covers the gap). True when the setup was closed."""
        if self.mt5_connection_manager is None or self.position_manager is None:
            return False
        level, timeframe = setup.close_invalidation
        if setup.close_pending:  # breach already known: retry only the legs whose close failed
            targets = [p for p in setup.positions if ("close", p.mt5_ticket) in setup.retry_counts]
        else:
            rates = await self.mt5_connection_manager.get_closed_rates(setup.symbol, timeframe, 3)
            if not rates:
                return False
            # Baseline: only candles closing after the fill count
            if setup.close_check_time is None:
                setup.close_check_time = rates[-1][0]
                return False
            broken = None
            for candle_time, _open, _high, _low, close, _volume in rates:
                if candle_time > setup.close_check_time:
                    setup.close_check_time = candle_time
                    if setup.closed_beyond_invalidation(close):
                        broken = close
            if broken is None:
                return False
            self.logger.warning(
                f"Invalidation chat={setup.chat_id}: M{timeframe} closed at {broken} beyond {level}, closing legs"
            )
            setup.close_pending = True
            setup.armed = setup.fire_now = False
            targets = list(setup.positions)

        for position in targets:
            ticket = position.mt5_ticket
            result = await self.position_manager.close_position_full(ticket)
            if result.success or await self._leg_closed(ticket):
                setup.retry_counts.pop(("close", ticket), None)
                continue
            self.logger.warning(
                f"Close of ticket {ticket} failed: {result.error_message}; retrying next poll"
            )
            self._record_retry_failure(setup, "close", ticket, "still not closed")
        if any(kind == "close" for kind, _ in setup.retry_counts):
            return False
        self._setup_aggregator.drop(setup, f"M{timeframe} close beyond invalidation {level}")
        zone = self._setup_aggregator.get_zone(setup.chat_id)
        if zone is not None and zone.linked_setup is setup:
            self._setup_aggregator.drop_zone(zone, "scenario invalidated")
        return True

    async def _leg_closed(self, ticket: int) -> bool:
        """True when MT5 history shows the position closed (e.g. its SL hit before our close)."""
        try:
            return await self.position_manager.get_close_reason(ticket) is not None
        except Exception as exc:
            self.logger.warning(f"Close-reason check failed for ticket {ticket}: {exc}")
            return False

    async def _maybe_fire_setup(self, setup: PendingSetup, price: Decimal) -> None:
        """Open every leg that is due at `price` (single entry -> all legs in the same tick)."""
        while setup.armed and await self._fire_next_leg(setup, price):
            pass

    async def _fire_next_leg(self, setup: PendingSetup, price: Decimal) -> bool:
        """Leg 0: SL+TP known (same live check as single-message signals) or price in zone.
        Next legs: price reached their ladder level. Returns True when a leg was opened."""
        k = setup.next_leg()
        if k is None:
            setup.armed = setup.fire_now = False
            return False
        index = len(setup.positions)
        if k > 0 and setup.blind and not setup.tps:
            return False  # extra legs wait for the author's targets
        if k == 0 and setup.is_complete:
            signal = setup.to_signal(entry=setup.low, sl=setup.sl, tp=setup.leg_tp(index))
            if not await self._validate_live_price(signal):
                self._disarm_or_drop(setup, "live price check failed")
                return False
            signal.entry_price = price
        else:
            tolerance = symbol_policy.get_entry_tolerance(setup.symbol)
            if k == 0:
                due = tolerance is not None and setup.in_entry_zone(price, tolerance)
            else:
                due = tolerance is not None and setup.leg_reached(k, price, tolerance)
            if not (due or setup.fire_now):
                return False
            levels = setup.fire_levels(price, index)
            if levels is None:
                self._disarm_or_drop(setup, f"invalidated at price {price}")
                return False
            signal = setup.to_signal(entry=price, sl=levels[0], tp=levels[1])
            if setup.sl is None:
                self.logger.info(f"Setup firing with default SL {levels[0]} (none received)")

        legs = len(setup.legs())
        blind = setup.blind or (
            k == 0 and setup.ladder and not setup.tps and setup.planned_legs is None
        )
        # Blind first leg: half the risk; remaining legs share the other half
        divisor = (2 if k == 0 else 2 * (legs - 1)) if blind else legs
        volume = await self._order_volume(signal, divisor)
        position = await self._handle_parsed_signal(
            signal, volume=volume, allow_reuse=bool(setup.positions)
        )
        if position is None:
            self._disarm_or_drop(setup, "order not filled")
            return False
        setup.blind = blind
        if setup.planned_legs is None and not (blind and k == 0):
            setup.planned_legs = legs
        setup.positions.append(position)
        now = datetime.now(UTC)
        setup.first_fill_at = setup.first_fill_at or now
        setup.last_update = now
        if setup.next_leg() is None or (
            setup.hold_at is not None and len(setup.positions) >= setup.hold_at
        ):
            setup.armed = setup.fire_now = False
        self.logger.info(
            f"Setup leg {k + 1}/{len(setup.legs())} filled chat={setup.chat_id} ticket={position.mt5_ticket} "
            f"volume={volume} SL={signal.stop_loss} TP={signal.take_profit} positions={len(setup.positions)}"
        )

        # Levels that arrived while the order was in flight
        leg_tp = setup.leg_tp(index)
        late_sl = setup.sl if setup.sl is not None and setup.sl != signal.stop_loss else None
        late_tp = leg_tp if leg_tp is not None and leg_tp != signal.take_profit else None
        if late_sl is not None or late_tp is not None:
            await self._apply_position_levels(position, late_sl, late_tp)
        return True

    async def _track_setup_legs(self, setup: PendingSetup) -> bool:
        """TP1 hit -> SL to break-even on open legs + stop unfilled legs. Returns True if all legs closed."""
        if self.position_manager is None:
            return False
        reasons = []
        for position in setup.positions:
            try:
                reasons.append(await self.position_manager.get_close_reason(position.mt5_ticket))
            except Exception as exc:
                self.logger.warning(
                    f"Close-reason check failed for ticket {position.mt5_ticket}: {exc}"
                )
                reasons.append(None)
        if all(reason is not None for reason in reasons):
            return True
        open_legs = [p for p, reason in zip(setup.positions, reasons) if reason is None]
        if not setup.be_done and not setup.be_pending and reasons[0] == "tp":
            # be_done only once MT5 accepted every SL move; until then retry each poll
            setup.be_pending = True
            setup.armed = setup.fire_now = False
            targets, triggered = open_legs, True
        elif setup.be_pending:
            for position, reason in zip(setup.positions, reasons):
                if reason is not None:  # closed meanwhile (e.g. its SL): nothing left to move
                    setup.retry_counts.pop(("be", position.mt5_ticket), None)
            targets = [p for p in open_legs if ("be", p.mt5_ticket) in setup.retry_counts]
            triggered = False
        else:
            return False

        for position in targets:
            if await self._apply_position_levels(position, Decimal(str(position.open_price)), None):
                setup.retry_counts.pop(("be", position.mt5_ticket), None)
            else:
                self._record_retry_failure(
                    setup, "be", position.mt5_ticket, "still not at break-even"
                )
        pending = [ticket for kind, ticket in setup.retry_counts if kind == "be"]
        if pending:
            if triggered:  # per-attempt failures are logged by _apply_position_levels
                self.logger.warning(
                    f"TP1 hit chat={setup.chat_id}: break-even pending for tickets {pending}, "
                    "retrying each poll; unfilled legs cancelled"
                )
            return False
        setup.be_pending, setup.be_done = False, True
        self.logger.info(
            f"TP1 hit chat={setup.chat_id}: open legs moved to break-even, unfilled legs cancelled"
        )
        return False

    def _record_retry_failure(self, setup: PendingSetup, kind: str, ticket: int, what: str) -> None:
        """Count a failed break-even/close attempt; one ERROR log when a ticket keeps failing."""
        count = setup.retry_counts.get((kind, ticket), 0) + 1
        setup.retry_counts[(kind, ticket)] = count
        if count == BE_RETRY_ERROR_AFTER:
            self.logger.error(
                f"{what} after {count} attempts, ticket {ticket} chat={setup.chat_id}; still retrying"
            )

    async def _risk_guard_refusal(self, signal, volume: float) -> str | None:
        """Account-wide limits before any order; None = allowed. MT5 unreadable -> refused (fail closed)."""
        try:
            snapshot = await self.position_manager.get_risk_snapshot(signal.symbol)
        except Exception as exc:
            self.logger.warning(f"Risk snapshot failed for {signal.symbol}: {exc}")
            snapshot = None
        new_risk = None
        if snapshot is not None and signal.stop_loss is not None:
            new_risk = risk_guard.order_risk(
                signal.entry_price - signal.stop_loss,
                Decimal(str(volume)),
                snapshot["tick_size"],
                snapshot["tick_value"],
            )
        return risk_guard.refusal(snapshot, new_risk, settings)

    async def _order_volume(self, signal, legs: int = 1) -> float:
        """Risk-based lots per leg; falls back to the default lot when inputs are unavailable."""
        fallback = currency_config.default_lot_size
        if signal.stop_loss is None or self.mt5_connection_manager is None:
            return fallback
        self.position_manager = self.position_manager or PositionManager(
            self.mt5_connection_manager
        )
        try:
            inputs = await self.position_manager.get_sizing_inputs(signal.symbol)
        except Exception as exc:
            self.logger.warning(f"Lot sizing inputs unavailable: {exc}")
            inputs = None
        if inputs is None:
            self.logger.warning(f"Lot sizing fell back to {fallback} for {signal.symbol}")
            return fallback
        volume = leg_volume(
            balance=inputs["balance"],
            risk_percent=settings.risk_percent,
            sl_distance=abs(signal.entry_price - signal.stop_loss),
            tick_size=inputs["tick_size"],
            tick_value=inputs["tick_value"],
            volume_min=inputs["volume_min"],
            volume_step=inputs["volume_step"],
            volume_max=inputs["volume_max"],
            legs=legs,
        )
        return float(volume)

    async def _validate_live_price(self, parsed_signal) -> bool:
        """Reject signals that can't be confirmed against a live MT5 market price."""
        canonical_symbol = symbol_policy.canonicalize_symbol(parsed_signal.symbol)
        if canonical_symbol is None:
            self.logger.warning(
                f"Live price check rejected signal (symbol not enabled): {parsed_signal.symbol}. "
                f"Enabled symbols: {symbol_policy.get_enabled_canonical_symbols()}"
            )
            return False

        parsed_signal.symbol = canonical_symbol

        if self.mt5_connection_manager is None:
            self.logger.warning(
                f"Live price check rejected signal (MT5 not connected): "
                f"{parsed_signal.symbol} @ {parsed_signal.entry_price}"
            )
            return False

        current_price = await self.mt5_connection_manager.get_current_price(parsed_signal.symbol)
        self.logger.info(
            f"Live price check for {parsed_signal.symbol}: entry {parsed_signal.entry_price} vs market {current_price}"
        )
        if current_price is None:
            self.logger.warning(
                f"Live price check rejected signal (no market price for {parsed_signal.symbol}): "
                f"{parsed_signal.entry_price}"
            )
            return False

        deviation = abs(parsed_signal.entry_price - current_price)
        max_deviation = symbol_policy.get_max_live_price_deviation(parsed_signal.symbol)
        if max_deviation is None:
            self.logger.warning(
                f"Live price check rejected signal (no deviation policy for symbol): {parsed_signal.symbol}"
            )
            return False

        if deviation > max_deviation:
            self.logger.warning(
                f"Live price check rejected signal: entry {parsed_signal.entry_price} vs market "
                f"{current_price} (deviation {deviation} > {max_deviation})"
            )
            return False

        return True

    async def _use_live_price_for_sizing(self, parsed_signal) -> bool:
        """Complete single-message signal: size and risk-check on the live price, as setup legs do.

        The order fills at market, so the post's entry would understate the SL distance when the
        market is away from it (F35). Only the complete-signal branch calls this: a setup keeps
        the author's entry as its zone. A stop closer to the market than the policy minimum (gold:
        20 pips) is moved out to it, unless the post's own stop is tighter. False = no order (price
        unreadable, or market already at or beyond the SL)."""
        stop_loss = parsed_signal.stop_loss
        if not isinstance(stop_loss, Decimal):  # an LLM signal can carry "break_even" here
            self.logger.warning(
                f"Signal refused: SL {stop_loss!r} is not a price, cannot size "
                f"message_id={parsed_signal.telegram_message_id}"
            )
            return False

        price = None
        if self.mt5_connection_manager is not None:
            price = await self.mt5_connection_manager.get_current_price(parsed_signal.symbol)
        if price is None:
            self.logger.warning(
                f"Signal refused: no live price to size {parsed_signal.symbol} "
                f"message_id={parsed_signal.telegram_message_id}"
            )
            return False

        sell = parsed_signal.parsed_action == ParsedAction.SELL
        if price >= stop_loss if sell else price <= stop_loss:
            self.logger.warning(
                f"Signal refused: market {price} already at or beyond SL {stop_loss} "
                f"({parsed_signal.parsed_action.value} {parsed_signal.symbol} "
                f"message_id={parsed_signal.telegram_message_id})"
            )
            return False

        # Stop never under the policy minimum from the market, unless the post's own stop is tighter
        minimum = symbol_policy.get_min_sl_distance(parsed_signal.symbol)
        if minimum is not None:
            minimum = min(minimum, abs(parsed_signal.entry_price - stop_loss))
            if abs(price - stop_loss) < minimum:
                moved = price + minimum if sell else price - minimum
                moved = Decimal(str(normalize_price(float(moved), parsed_signal.symbol)))
                self.logger.info(
                    f"Single signal chat={parsed_signal.telegram_chat_id} "
                    f"message_id={parsed_signal.telegram_message_id}: SL moved {stop_loss} -> {moved} "
                    f"(minimum {minimum} from market {price})"
                )
                stop_loss = parsed_signal.stop_loss = moved

        if price != parsed_signal.entry_price:
            self.logger.info(
                f"Single signal chat={parsed_signal.telegram_chat_id} "
                f"message_id={parsed_signal.telegram_message_id}: sized on live price {price} "
                f"(post entry {parsed_signal.entry_price}), SL distance {abs(price - stop_loss)}"
            )
            parsed_signal.entry_price = price
        return True

    async def _handle_parsed_signal(
        self, parsed_signal, volume: float | None = None, allow_reuse: bool = False
    ):
        """Persist a parsed signal, execute it, and return the opened Position (or None)."""
        if self._database_ready and self._signal_repository is not None:
            try:
                await self._signal_repository.save_signal(
                    {
                        "telegram_message_id": parsed_signal.telegram_message_id,
                        "telegram_chat_id": parsed_signal.telegram_chat_id,
                        "sender": parsed_signal.sender,
                        "timestamp": parsed_signal.timestamp,
                        "raw_text": parsed_signal.raw_text,
                        "parsed_action": parsed_signal.parsed_action.value,
                        "symbol": parsed_signal.symbol,
                        "entry_price": float(parsed_signal.entry_price),
                        "stop_loss": (
                            float(parsed_signal.stop_loss)
                            if parsed_signal.stop_loss is not None
                            else None
                        ),
                        "take_profit": (
                            float(parsed_signal.take_profit)
                            if parsed_signal.take_profit is not None
                            else None
                        ),
                        "confidence_score": float(parsed_signal.confidence_score),
                        "parser_type": parsed_signal.parser_type,
                        "status": parsed_signal.status,
                    }
                )
            except Exception as e:
                self.logger.warning(f"Failed to persist parsed signal: {e}")

        position = await self._execute_signal_on_mt5(parsed_signal, volume, allow_reuse)

        if self.console_dashboard is not None:
            # Shown after execution so a refused order (MT5, risk guard) doesn't look like a trade
            self.console_dashboard.add_recent_signal(
                signal_type="NEW" if position is not None else "NOT FILLED",
                symbol=parsed_signal.symbol,
                action=parsed_signal.parsed_action.value,
                price=float(parsed_signal.entry_price),
                ticket_id=str(position.mt5_ticket) if position is not None else None,
                timestamp=parsed_signal.timestamp,
            )

        self.logger.info(
            f"Signal detected from chat {parsed_signal.telegram_chat_id}: "
            f"{parsed_signal.parsed_action.value} {parsed_signal.symbol} @ {parsed_signal.entry_price}"
            f"{'' if position is not None else ' (NOT FILLED)'}"
        )
        return position

    async def _execute_signal_on_mt5(
        self, parsed_signal, volume: float | None = None, allow_reuse: bool = False
    ):
        """Execute BUY/SELL on MT5 and persist the position. `allow_reuse`: extra leg of a signal
        this setup already executed (its claim is taken), so open another order on the same signal row.
        """
        if self.mt5_connection_manager is None:
            self.logger.info("MT5 execution skipped: connection manager not ready")
            return None

        action_name = getattr(
            parsed_signal.parsed_action, "value", str(parsed_signal.parsed_action)
        ).upper()
        if action_name not in {"BUY", "SELL"}:
            self.logger.info(
                f"MT5 execution skipped for non-market action: {action_name} on {parsed_signal.symbol}"
            )
            return

        if self._signal_repository is None:
            self.logger.warning("MT5 execution skipped: signal repository unavailable")
            return

        try:
            signal_record = await self._signal_repository.find_by_message_id(
                parsed_signal.telegram_message_id, chat_id=parsed_signal.telegram_chat_id
            )
            if signal_record is None:
                self.logger.warning("Signal record missing before MT5 execution")
                return

            claimed = await self._signal_repository.claim_signal_for_execution(signal_record.id)
            status = getattr(signal_record.status, "value", signal_record.status)
            extra_leg = not claimed and allow_reuse and status == SignalStatus.EXECUTED.value
            if not claimed and not extra_leg:
                self.logger.info(
                    f"MT5 execution skipped for signal {signal_record.id}: "
                    "already claimed or processed"
                )
                return None

            self.position_manager = self.position_manager or PositionManager(
                self.mt5_connection_manager
            )
            if volume is None:
                volume = await self._order_volume(parsed_signal)
            refused = await self._risk_guard_refusal(parsed_signal, volume)
            if refused is not None:
                if not extra_leg:
                    await self._signal_repository.update_signal_status(
                        signal_id=signal_record.id,
                        status=SignalStatus.REJECTED,
                        rejection_reason=f"risk guard: {refused}",
                    )
                self.logger.warning(
                    f"Risk guard refused {action_name} {parsed_signal.symbol} volume={volume}: {refused}"
                )
                return None
            order_result = await self.position_manager.open_market_order(
                parsed_signal, volume=volume
            )

            if not order_result.success:
                if not extra_leg:
                    await self._signal_repository.update_signal_status(
                        signal_id=signal_record.id,
                        status=SignalStatus.REJECTED,
                        rejection_reason=order_result.error_message or "MT5 order rejected",
                    )
                self.logger.warning(
                    f"MT5 order rejected for {parsed_signal.symbol}: {order_result.error_message}"
                )
                return None

            # Break-even and profit-lock checks use open_price: store the fill, not the quote
            fill_price = getattr(order_result, "fill_price", None)
            if fill_price is None:
                open_price = float(parsed_signal.entry_price)
                self.logger.warning(
                    f"Ticket {order_result.ticket}: fill price unknown, storing signal price {open_price}"
                )
            else:
                open_price = fill_price
                self.logger.info(
                    f"Ticket {order_result.ticket} filled at {fill_price} "
                    f"(aimed {float(parsed_signal.entry_price)})"
                )

            position = None
            if self._position_repository is not None:
                position = await self._position_repository.create(
                    signal_id=signal_record.id,
                    mt5_ticket=order_result.ticket,
                    open_time=datetime.now(UTC),
                    open_price=open_price,
                    volume=volume,
                    current_sl=(
                        float(parsed_signal.stop_loss)
                        if parsed_signal.stop_loss is not None
                        else None
                    ),
                    current_tp=(
                        float(parsed_signal.take_profit)
                        if parsed_signal.take_profit is not None
                        else None
                    ),
                    status=PositionStatus.OPEN,
                    last_sync=datetime.now(UTC),
                )

            if not extra_leg:
                await self._signal_repository.update_signal_status(
                    signal_id=signal_record.id,
                    status=SignalStatus.EXECUTED,
                )

            self.logger.info(
                f"MT5 execution successful: {signal_record.id} -> ticket {order_result.ticket} volume={volume}"
            )
            return position
        except Exception as exc:
            self.logger.error(f"Failed to execute signal on MT5: {exc}", exc_info=True)
            return None

    async def _poll_telegram_groups(self) -> None:
        """Fallback polling loop to ingest channel messages when push events are not received."""
        if self.telegram_client is None or self.telegram_client.client is None:
            self.logger.warning("Telegram polling fallback skipped: Telegram client unavailable")
            return

        if not self._connected_group_entities:
            self.logger.warning("Telegram polling fallback skipped: no connected groups")
            return

        client = self.telegram_client.client
        last_seen: dict[int, int] = {}

        try:
            # Seed per-group last seen IDs so only newer messages are processed.
            for entity in self._connected_group_entities:
                latest = await client.get_messages(entity, limit=1)
                if latest and len(latest) > 0:
                    last_seen[int(entity.id)] = int(latest[0].id)
                    self.logger.info(
                        "Telegram polling seed "
                        f"chat={get_peer_id(entity)} title='{getattr(entity, 'title', str(entity.id))}' "
                        f"last_message_id={int(latest[0].id)}"
                    )
                else:
                    last_seen[int(entity.id)] = 0
                    self.logger.info(
                        "Telegram polling seed "
                        f"chat={get_peer_id(entity)} title='{getattr(entity, 'title', str(entity.id))}' "
                        "last_message_id=0"
                    )

            self.logger.info(
                f"Telegram polling fallback started for {len(self._connected_group_entities)} group(s)"
            )

            while True:
                await asyncio.sleep(3)

                for entity in self._connected_group_entities:
                    entity_id = int(entity.id)
                    min_id = last_seen.get(entity_id, 0)

                    messages = await client.get_messages(entity, min_id=min_id, limit=20)
                    if not messages:
                        continue

                    ordered_messages = sorted(messages, key=lambda msg: msg.id)
                    for message in ordered_messages:
                        if not getattr(message, "text", None):
                            continue

                        chat_id = get_peer_id(entity)

                        from_id = getattr(message, "from_id", None)
                        sender = "unknown_sender"
                        if (
                            from_id is not None
                            and hasattr(from_id, "user_id")
                            and from_id.user_id is not None
                        ):
                            sender = f"user_{from_id.user_id}"
                        elif (
                            from_id is not None
                            and hasattr(from_id, "channel_id")
                            and from_id.channel_id is not None
                        ):
                            sender = f"channel_{from_id.channel_id}"

                        metadata = {
                            "telegram_message_id": int(message.id),
                            "telegram_chat_id": int(chat_id),
                            "sender": sender,
                            "timestamp": message.date if message.date else datetime.now(UTC),
                            "raw_text": message.text or "",
                            "reply_to_message_id": (
                                message.reply_to.reply_to_msg_id if message.reply_to else None
                            ),
                            "chat_title": getattr(entity, "title", str(entity_id)),
                            "is_forwarded": getattr(message, "forward", None) is not None,
                            "message_type": "text",
                            "edit_date": getattr(message, "edit_date", None),
                        }

                        await self._on_telegram_message(metadata)

                    newest_id = int(ordered_messages[-1].id)
                    if newest_id > min_id:
                        last_seen[entity_id] = newest_id

        except asyncio.CancelledError:
            self.logger.info("Telegram polling fallback stopped")
            raise
        except Exception as e:
            self.logger.error(f"Telegram polling fallback failed: {e}", exc_info=True)

    async def _initialize_mt5_connection(self) -> None:
        """Initialize MT5 connection manager in background and attach it to monitoring."""
        if not settings.validate_mt5_config():
            self.logger.warning("MT5 configuration is incomplete; MT5 startup skipped")
            return

        try:
            self.mt5_connection_manager = MT5ConnectionManager(
                max_connections=settings.mt5_max_connections
            )
            self.mt5_connection = self.mt5_connection_manager

            if self.health_monitor is not None:
                self.health_monitor.set_mt5_connection(self.mt5_connection_manager)

            await self.mt5_connection_manager._ensure_initialized()
            if self.mt5_connection_manager._initialized:
                self.logger.info("MT5 runtime initialized")
            else:
                self.logger.warning("MT5 not reachable yet; will retry on next MT5 call")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.logger.warning(f"MT5 initialization failed, continuing in degraded mode: {e}")

    async def _initialize_monitoring_components(self) -> None:
        """Initialize health monitoring, metrics collection, and dashboard."""
        self.logger.info("Initializing monitoring components")

        # Initialize health monitor
        self.health_monitor = HealthMonitor(check_interval=60)
        if self.telegram_client is not None:
            self.health_monitor.set_telegram_client(self.telegram_client)
        if self.mt5_connection_manager is not None:
            self.health_monitor.set_mt5_connection(self.mt5_connection_manager)
        if settings.validate_openai_config():
            try:
                from config.openai_config import openai_client

                self.health_monitor.set_openai_client(openai_client)
            except Exception as e:
                self.logger.warning(f"OpenAI client setup skipped: {e}")

        # Initialize metrics collector
        self.metrics_collector = MetricsCollector(max_history_minutes=60)

        # Initialize console dashboard
        self.console_dashboard = ConsoleDashboard(
            health_monitor=self.health_monitor, refresh_interval=2
        )

        # Start monitoring components
        await self.health_monitor.start_monitoring()
        self.logger.info("Health monitoring started")

        # Start dashboard in background task
        self._component_tasks["dashboard"] = asyncio.create_task(
            self.console_dashboard.start_dashboard()
        )
        self.logger.info("Console dashboard started")

    async def run(self) -> None:
        """Main application run loop."""
        try:
            # Wait for shutdown signal
            await self.shutdown_event.wait()
            self.logger.info("Shutdown signal received, initiating graceful shutdown")

        except Exception as e:
            self.logger.error(f"Error in main run loop: {e}", exc_info=True)
            raise

    async def shutdown(self) -> None:
        """Perform graceful shutdown of all components."""
        self.logger.info("Starting graceful shutdown process")

        try:
            # Save current application state
            await self._save_application_state()

            # Stop monitoring components
            await self._shutdown_monitoring_components()

            # Stop Telegram auth/client lifecycle
            if self.telegram_auth_manager is not None:
                await self.telegram_auth_manager.cleanup()

            # Stop MT5 connection manager lifecycle
            if self.mt5_connection_manager is not None:
                await self.mt5_connection_manager.shutdown()

            # Close database lifecycle
            if self._database_ready:
                await close_database()
                self._database_ready = False

            # Cancel all component tasks
            await self._cancel_component_tasks()

            # Flush and close log handlers
            await self._shutdown_logging()

            self.logger.info("Graceful shutdown completed successfully")

        except Exception as e:
            self.logger.error(f"Error during shutdown: {e}", exc_info=True)
            raise

    async def _save_application_state(self) -> None:
        """Save current application state to disk for recovery."""
        try:
            # Create state directory if it doesn't exist
            state_dir = settings.data_dir / "state"
            state_dir.mkdir(parents=True, exist_ok=True)

            # Save queue states (placeholder for actual queue implementations)
            queue_state_file = state_dir / "queue_states.json"
            with open(queue_state_file, "w") as f:
                json.dump(self._queue_states, f, indent=2, default=str)

            # Save metrics summary
            if self.metrics_collector:
                metrics_file = state_dir / "metrics_summary.json"
                with open(metrics_file, "w") as f:
                    json.dump(
                        self.metrics_collector.get_all_metrics_summary(), f, indent=2, default=str
                    )

            # Save component health status
            if self.health_monitor:
                health_file = state_dir / "health_status.json"
                health_data = {
                    component: health.to_dict()
                    for component, health in self.health_monitor.get_all_component_healths().items()
                }
                with open(health_file, "w") as f:
                    json.dump(health_data, f, indent=2, default=str)

            self.logger.info("Application state saved successfully")

        except Exception as e:
            self.logger.error(f"Error saving application state: {e}", exc_info=True)

    async def _shutdown_monitoring_components(self) -> None:
        """Shutdown monitoring components gracefully."""
        try:
            # Stop console dashboard
            if self.console_dashboard:
                await self.console_dashboard.stop_dashboard()
                self.logger.info("Console dashboard stopped")

            # Stop health monitoring
            if self.health_monitor:
                await self.health_monitor.stop_monitoring()
                self.logger.info("Health monitoring stopped")

        except Exception as e:
            self.logger.error(f"Error stopping monitoring components: {e}")

    async def _cancel_component_tasks(self) -> None:
        """Cancel all background component tasks."""
        if not self._component_tasks:
            return

        self.logger.info(f"Cancelling {len(self._component_tasks)} component tasks")

        # Cancel all tasks
        for task_name, task in self._component_tasks.items():
            if not task.done():
                self.logger.debug(f"Cancelling task: {task_name}")
                task.cancel()

        # Wait for tasks to complete with timeout
        if self._component_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*self._component_tasks.values(), return_exceptions=True),
                    timeout=self.shutdown_timeout,
                )
            except asyncio.TimeoutError:
                self.logger.warning(
                    f"Some tasks did not complete within {self.shutdown_timeout}s timeout"
                )
            except Exception as e:
                self.logger.error(f"Error waiting for tasks to complete: {e}")

        self._component_tasks.clear()
        self.logger.info("All component tasks cancelled")

    async def _shutdown_logging(self) -> None:
        """Flush and close all logging handlers."""
        try:
            # Flush all handlers
            for handler in logging.root.handlers[:]:
                handler.flush()
                if hasattr(handler, "close"):
                    handler.close()

            self.logger.info("Logging handlers flushed and closed")

        except Exception as e:
            # Use print here since logging might be compromised
            print(f"Error shutting down logging: {e}")

    def signal_shutdown(self) -> None:
        """Signal the application to begin shutdown."""
        self.logger.info("Shutdown signal received")
        self.shutdown_event.set()


# Global application manager instance
app_manager: Optional[ApplicationManager] = None


def signal_handler(sig: int, frame) -> None:
    """
    Signal handler for graceful shutdown.
    Handles SIGTERM and SIGINT signals.
    """
    global app_manager

    signal_names = {signal.SIGTERM: "SIGTERM", signal.SIGINT: "SIGINT"}
    signal_name = signal_names.get(sig, f"Signal {sig}")

    if app_manager:
        print(f"\nReceived {signal_name}, initiating graceful shutdown...")
        app_manager.signal_shutdown()
    else:
        print(f"\nReceived {signal_name}, exiting immediately...")
        sys.exit(1)


async def main() -> None:
    """
    Main application entry point with graceful shutdown handling.
    Sets up signal handlers and manages the application lifecycle.
    """
    global app_manager

    # Setup logging first (per-instance folder when BOT_INSTANCE is set)
    setup_logging(log_dir=str(settings.log_dir))
    logger = get_logger(__name__)

    try:
        # Register signal handlers for graceful shutdown
        signal.signal(signal.SIGTERM, signal_handler)
        signal.signal(signal.SIGINT, signal_handler)

        # Create and initialize application manager
        app_manager = ApplicationManager()

        # Start application components
        await app_manager.startup()

        # Run main application loop
        await app_manager.run()

    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    except Exception as e:
        logger.error(f"Fatal error in main application: {e}", exc_info=True)
        sys.exit(1)
    finally:
        # Perform graceful shutdown
        if app_manager:
            try:
                await asyncio.wait_for(app_manager.shutdown(), timeout=app_manager.shutdown_timeout)
            except asyncio.TimeoutError:
                logger.error(
                    f"Shutdown did not complete within {app_manager.shutdown_timeout}s timeout"
                )
                sys.exit(1)
            except Exception as e:
                logger.error(f"Error during shutdown: {e}", exc_info=True)
                sys.exit(1)

        logger.info("Application shutdown complete")


if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())
