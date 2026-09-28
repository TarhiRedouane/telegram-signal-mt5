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
from datetime import UTC, datetime
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
from src.database import close_database, get_position_repository, get_signal_repository, initialize_database
from src.database.models import PositionStatus, SignalStatus
from telethon.utils import get_peer_id


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
            self._component_tasks['mt5_init'] = asyncio.create_task(
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
            raise ValueError("Telegram configuration is incomplete. Check TELEGRAM_API_ID, TELEGRAM_API_HASH, and PHONE_NUMBER.")

        self.telegram_auth_manager = AuthManager()
        telegram_ready = await self.telegram_auth_manager.startup_authentication()
        if not telegram_ready:
            self.logger.warning("Telegram authentication did not complete successfully during startup")
            startup_reason = self.telegram_auth_manager.last_startup_error or "Unknown Telegram startup error"
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
                    group
                    for group, result in connection_results.items()
                    if result.get("success")
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
                self.message_handler.handle_new_message,
                self._telegram_event_filter
            )

            # Fallback path: poll connected groups so we still ingest messages
            # when Telegram push events are delayed or missed.
            self._component_tasks['telegram_polling'] = asyncio.create_task(
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
            self._database_ready = True
            self.logger.info("Persistence components initialized")
        except Exception as e:
            self._database_ready = False
            self._signal_repository = None
            self.logger.warning(f"Persistence initialization failed, continuing in degraded mode: {e}")

    async def _on_telegram_message(self, metadata: Dict[str, Any]) -> None:
        """Parse incoming Telegram messages and push actionable signals to dashboard."""
        message_key = (
            f"{metadata.get('telegram_chat_id', 0)}_"
            f"{metadata.get('telegram_message_id', 0)}"
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
        self.logger.info(
            f"Message received chat={metadata.get('telegram_chat_id')} "
            f"message_id={metadata.get('telegram_message_id')} text='{preview}'"
        )

        started_at = asyncio.get_running_loop().time()
        parsed_signal = self.regex_parser.parse(raw_text) if self.regex_parser else None

        if parsed_signal is None and self.llm_parser is not None:
            try:
                parsed_signal = await self.llm_parser.parse_with_llm(raw_text)
            except Exception as e:
                self.logger.warning(f"LLM fallback parse failed: {e}")

        latency_ms = int((asyncio.get_running_loop().time() - started_at) * 1000)

        if parsed_signal:
            parsed_signal.telegram_message_id = int(metadata.get("telegram_message_id", 0))
            parsed_signal.telegram_chat_id = int(metadata.get("telegram_chat_id", 0))
            parsed_signal.sender = str(metadata.get("sender", "unknown"))
            parsed_signal.timestamp = metadata.get("timestamp")

            if await self._validate_live_price(parsed_signal):
                self._parsed_signals_today += 1
                await self._handle_parsed_signal(parsed_signal)
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

    async def _handle_parsed_signal(self, parsed_signal) -> None:
        """Persist a successfully parsed signal and update the dashboard."""
        if self._database_ready and self._signal_repository is not None:
            try:
                await self._signal_repository.save_signal({
                    "telegram_message_id": parsed_signal.telegram_message_id,
                    "telegram_chat_id": parsed_signal.telegram_chat_id,
                    "sender": parsed_signal.sender,
                    "timestamp": parsed_signal.timestamp,
                    "raw_text": parsed_signal.raw_text,
                    "parsed_action": parsed_signal.parsed_action.value,
                    "symbol": parsed_signal.symbol,
                    "entry_price": float(parsed_signal.entry_price),
                    "stop_loss": float(parsed_signal.stop_loss) if parsed_signal.stop_loss is not None else None,
                    "take_profit": float(parsed_signal.take_profit) if parsed_signal.take_profit is not None else None,
                    "confidence_score": float(parsed_signal.confidence_score),
                    "parser_type": parsed_signal.parser_type,
                    "status": parsed_signal.status,
                })
            except Exception as e:
                self.logger.warning(f"Failed to persist parsed signal: {e}")

        if self.console_dashboard is not None:
            self.console_dashboard.add_recent_signal(
                signal_type="NEW",
                symbol=parsed_signal.symbol,
                action=parsed_signal.parsed_action.value,
                price=float(parsed_signal.entry_price),
                timestamp=parsed_signal.timestamp,
            )

        await self._execute_signal_on_mt5(parsed_signal)

        self.logger.info(
            f"Signal detected from chat {parsed_signal.telegram_chat_id}: "
            f"{parsed_signal.parsed_action.value} {parsed_signal.symbol} @ {parsed_signal.entry_price}"
        )

    async def _execute_signal_on_mt5(self, parsed_signal) -> None:
        """Execute BUY/SELL signals on MT5 and persist an open position record."""
        if self.mt5_connection_manager is None:
            self.logger.info("MT5 execution skipped: connection manager not ready")
            return

        action_name = getattr(parsed_signal.parsed_action, "value", str(parsed_signal.parsed_action)).upper()
        if action_name not in {"BUY", "SELL"}:
            self.logger.info(
                f"MT5 execution skipped for non-market action: {action_name} on {parsed_signal.symbol}"
            )
            return

        if self._signal_repository is None:
            self.logger.warning("MT5 execution skipped: signal repository unavailable")
            return

        try:
            self.position_manager = self.position_manager or PositionManager(self.mt5_connection_manager)
            volume = 0.01
            order_result = await self.position_manager.open_market_order(parsed_signal, volume=volume)

            if not order_result.success:
                await self._signal_repository.update_signal_status(
                    signal_id=(await self._signal_repository.find_by_message_id(parsed_signal.telegram_message_id)).id,
                    status=SignalStatus.REJECTED,
                    rejection_reason=order_result.error_message or "MT5 order rejected"
                )
                self.logger.warning(
                    f"MT5 order rejected for {parsed_signal.symbol}: {order_result.error_message}"
                )
                return

            signal_record = await self._signal_repository.find_by_message_id(parsed_signal.telegram_message_id)
            if signal_record is None:
                self.logger.warning("Signal record missing before MT5 position creation")
                return

            if self._position_repository is not None:
                await self._position_repository.create(
                    signal_id=signal_record.id,
                    mt5_ticket=order_result.ticket,
                    open_time=datetime.now(UTC),
                    open_price=float(parsed_signal.entry_price),
                    volume=volume,
                    current_sl=float(parsed_signal.stop_loss) if parsed_signal.stop_loss is not None else None,
                    current_tp=float(parsed_signal.take_profit) if parsed_signal.take_profit is not None else None,
                    status=PositionStatus.OPEN,
                    last_sync=datetime.now(UTC),
                )

            await self._signal_repository.update_signal_status(
                signal_id=signal_record.id,
                status=SignalStatus.EXECUTED,
            )

            self.logger.info(
                f"MT5 execution successful: {signal_record.id} -> ticket {order_result.ticket}"
            )
        except Exception as exc:
            self.logger.error(f"Failed to execute signal on MT5: {exc}", exc_info=True)

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
                        if not getattr(message, 'text', None):
                            continue

                        chat_id = getattr(message, 'chat_id', None)
                        if chat_id is None:
                            chat_id = get_peer_id(entity)

                        from_id = getattr(message, 'from_id', None)
                        sender = "unknown_sender"
                        if from_id is not None and hasattr(from_id, 'user_id') and from_id.user_id is not None:
                            sender = f"user_{from_id.user_id}"
                        elif from_id is not None and hasattr(from_id, 'channel_id') and from_id.channel_id is not None:
                            sender = f"channel_{from_id.channel_id}"

                        metadata = {
                            'telegram_message_id': int(message.id),
                            'telegram_chat_id': int(chat_id),
                            'sender': sender,
                            'timestamp': message.date if message.date else datetime.now(UTC),
                            'raw_text': message.text or '',
                            'reply_to_message_id': message.reply_to.reply_to_msg_id if message.reply_to else None,
                            'chat_title': getattr(entity, 'title', str(entity_id)),
                            'is_forwarded': getattr(message, 'forward', None) is not None,
                            'message_type': 'text',
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
            self.mt5_connection_manager = MT5ConnectionManager(max_connections=settings.mt5_max_connections)
            self.mt5_connection = self.mt5_connection_manager

            if self.health_monitor is not None:
                self.health_monitor.set_mt5_connection(self.mt5_connection_manager)

            await self.mt5_connection_manager._ensure_initialized()
            self.logger.info("MT5 runtime initialized")
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
            health_monitor=self.health_monitor,
            refresh_interval=2
        )

        # Start monitoring components
        await self.health_monitor.start_monitoring()
        self.logger.info("Health monitoring started")

        # Start dashboard in background task
        self._component_tasks['dashboard'] = asyncio.create_task(
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
            state_dir = Path("data/state")
            state_dir.mkdir(parents=True, exist_ok=True)
            
            # Save queue states (placeholder for actual queue implementations)
            queue_state_file = state_dir / "queue_states.json"
            with open(queue_state_file, 'w') as f:
                json.dump(self._queue_states, f, indent=2, default=str)
            
            # Save metrics summary
            if self.metrics_collector:
                metrics_file = state_dir / "metrics_summary.json"
                with open(metrics_file, 'w') as f:
                    json.dump(
                        self.metrics_collector.get_all_metrics_summary(),
                        f, 
                        indent=2, 
                        default=str
                    )
            
            # Save component health status
            if self.health_monitor:
                health_file = state_dir / "health_status.json"
                health_data = {
                    component: health.to_dict()
                    for component, health in self.health_monitor.get_all_component_healths().items()
                }
                with open(health_file, 'w') as f:
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
                    timeout=self.shutdown_timeout
                )
            except asyncio.TimeoutError:
                self.logger.warning(f"Some tasks did not complete within {self.shutdown_timeout}s timeout")
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
                if hasattr(handler, 'close'):
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
    
    # Setup logging first
    setup_logging()
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
                await asyncio.wait_for(
                    app_manager.shutdown(),
                    timeout=app_manager.shutdown_timeout
                )
            except asyncio.TimeoutError:
                logger.error(f"Shutdown did not complete within {app_manager.shutdown_timeout}s timeout")
                sys.exit(1)
            except Exception as e:
                logger.error(f"Error during shutdown: {e}", exc_info=True)
                sys.exit(1)
        
        logger.info("Application shutdown complete")


if __name__ == "__main__":
    # Run the async main function
    asyncio.run(main())
