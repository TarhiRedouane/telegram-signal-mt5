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
from pathlib import Path
from typing import Any, Dict, Optional

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent / "src"))

from config.logging_config import get_logger, setup_logging
from src.monitoring.health_checker import HealthMonitor
from src.monitoring.metrics_collector import MetricsCollector
from src.monitoring.console_dashboard import ConsoleDashboard


class ApplicationManager:
    """
    Main application manager with graceful shutdown capabilities.
    Manages all system components and handles shutdown procedures.
    """
    
    def __init__(self):
        self.logger = get_logger(__name__)
        self.shutdown_event = asyncio.Event()
        self.shutdown_timeout = 30  # seconds
        
        # System components (will be initialized during startup)
        self.telegram_client = None
        self.mt5_connection = None
        self.health_monitor: Optional[HealthMonitor] = None
        self.metrics_collector: Optional[MetricsCollector] = None  
        self.console_dashboard: Optional[ConsoleDashboard] = None
        
        # Component state for shutdown
        self._component_tasks: Dict[str, asyncio.Task] = {}
        self._queue_states: Dict[str, Any] = {}
        
        self.logger.info("Application manager initialized")
    
    async def startup(self) -> None:
        """Initialize and start all application components."""
        try:
            self.logger.info("Starting Telegram Signal EA application")
            
            # Initialize monitoring components first
            await self._initialize_monitoring_components()
            
            # TODO: Initialize configuration
            # TODO: Initialize database
            # TODO: Start Telegram client
            # TODO: Start signal processing pipeline  
            # TODO: Start MT5 connection
            
            self.logger.info("Application startup complete - ready to process signals")
            
        except Exception as e:
            self.logger.error(f"Error during startup: {e}", exc_info=True)
            raise
    
    async def _initialize_monitoring_components(self) -> None:
        """Initialize health monitoring, metrics collection, and dashboard."""
        self.logger.info("Initializing monitoring components")
        
        # Initialize health monitor
        self.health_monitor = HealthMonitor(check_interval=60)
        
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
            
            # TODO: Stop signal processing pipeline
            # TODO: Close MT5 connections
            # TODO: Close Telegram connections
            # TODO: Close database connections
            
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
