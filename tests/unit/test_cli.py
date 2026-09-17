import os
import sys
from unittest.mock import AsyncMock, patch

import pytest

import cli
from config.settings import settings
from main import ApplicationManager


def test_start_uses_current_python_executable(monkeypatch):
    """Starting the app should use the current venv Python interpreter instead of a generic shell `python`."""
    manager = cli.SignalEAManager()
    monkeypatch.setattr(manager, "is_running", lambda: False)

    with patch("cli.subprocess.Popen") as mock_popen:
        manager.start()

    mock_popen.assert_called_once()
    assert mock_popen.call_args[0][0][0] == sys.executable


@pytest.mark.asyncio
async def test_startup_wires_live_telegram_and_mt5_clients(monkeypatch):
    """Startup should register the real Telegram and MT5 clients with the health monitor."""
    manager = ApplicationManager()
    manager.health_monitor = AsyncMock()
    manager.health_monitor.set_telegram_client = lambda client: setattr(manager, "telegram_client", client)
    manager.health_monitor.set_mt5_connection = lambda connection: setattr(manager, "mt5_connection", connection)
    monkeypatch.setattr(manager, "_initialize_monitoring_components", AsyncMock())
    monkeypatch.setattr(settings, "validate_telegram_config", lambda: True)
    monkeypatch.setattr(settings, "validate_mt5_config", lambda: True)

    mock_auth_manager = AsyncMock()
    mock_auth_manager.telegram_client = object()
    mock_auth_manager.startup_authentication = AsyncMock(return_value=True)
    mock_mt5_manager = AsyncMock()
    mock_mt5_manager._ensure_initialized = AsyncMock()

    monkeypatch.setattr("main.AuthManager", lambda: mock_auth_manager)
    monkeypatch.setattr("main.MT5ConnectionManager", lambda max_connections=None: mock_mt5_manager)

    await manager.startup()

    assert manager.telegram_client is mock_auth_manager.telegram_client
    assert manager.mt5_connection is mock_mt5_manager
    mock_auth_manager.startup_authentication.assert_awaited_once()
    mock_mt5_manager._ensure_initialized.assert_awaited_once()


def test_is_running_handles_windows_invalid_pid(monkeypatch, tmp_path):
    """Status checks should treat invalid Windows PIDs as not running instead of crashing."""
    manager = cli.SignalEAManager()
    manager.pid_file = tmp_path / "signal_ea.pid"
    manager.pid_file.write_text("999999", encoding="utf-8")

    def raise_oserror(*args, **kwargs):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(os, "kill", raise_oserror)

    assert manager.is_running() is False
