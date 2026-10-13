import asyncio
import os
import subprocess
import sys
from unittest.mock import AsyncMock, Mock, patch

import psutil
import pytest

import cli
from config.settings import settings
from main import ApplicationManager


def test_start_uses_current_python_executable(monkeypatch, tmp_path):
    """Starting the app should use the current venv Python interpreter instead of a generic shell `python`."""
    manager = cli.SignalEAManager()
    manager.pid_file = tmp_path / "signal_ea.pid"  # never write into the real data/ folder
    monkeypatch.setattr(manager, "is_running", lambda: False)

    with patch("cli.subprocess.Popen") as mock_popen:
        manager.start()

    mock_popen.assert_called_once()
    assert mock_popen.call_args[0][0][0] == sys.executable


@pytest.mark.asyncio
async def test_startup_wires_live_telegram_and_mt5_clients(monkeypatch):
    """Startup should initialize Telegram, register monitoring, and start MT5 initialization without blocking."""
    manager = ApplicationManager()
    manager.health_monitor = AsyncMock()
    manager.health_monitor.set_telegram_client = lambda client: setattr(
        manager, "_test_telegram_set", client
    )
    manager.health_monitor.set_mt5_connection = lambda connection: setattr(
        manager, "_test_mt5_set", connection
    )

    initialize_monitoring_mock = AsyncMock()
    monkeypatch.setattr(manager, "_initialize_monitoring_components", initialize_monitoring_mock)
    initialize_telegram_monitoring_mock = AsyncMock()
    monkeypatch.setattr(
        manager, "_initialize_telegram_monitoring", initialize_telegram_monitoring_mock
    )
    initialize_mt5_mock = AsyncMock()
    monkeypatch.setattr(manager, "_initialize_mt5_connection", initialize_mt5_mock)
    monkeypatch.setattr(settings, "validate_telegram_config", lambda: True)

    mock_auth_manager = AsyncMock()
    mock_auth_manager.telegram_client = object()
    mock_auth_manager.startup_authentication = AsyncMock(return_value=True)

    monkeypatch.setattr("main.AuthManager", lambda: mock_auth_manager)

    await manager.startup()
    await asyncio.sleep(0)

    assert manager.telegram_client is mock_auth_manager.telegram_client
    mock_auth_manager.startup_authentication.assert_awaited_once()
    initialize_monitoring_mock.assert_awaited_once()
    initialize_telegram_monitoring_mock.assert_awaited_once()
    initialize_mt5_mock.assert_awaited_once()


def test_is_running_handles_windows_invalid_pid(monkeypatch, tmp_path):
    """Status checks should treat invalid Windows PIDs as not running instead of crashing."""
    manager = cli.SignalEAManager()
    manager.pid_file = tmp_path / "signal_ea.pid"
    manager.pid_file.write_text("999999", encoding="utf-8")

    def raise_oserror(*args, **kwargs):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(os, "kill", raise_oserror)

    assert manager.is_running() is False


SLEEP = "import time; time.sleep(30)"


@pytest.fixture
def spawn():
    """Start real Python processes; killed at teardown."""
    processes = []

    def _spawn(*args, cwd=None):
        process = subprocess.Popen([sys.executable, *args], cwd=cwd)
        processes.append(process)
        return process

    yield _spawn
    for process in processes:
        process.kill()
        process.wait()


def _manager(tmp_path, pid):
    manager = cli.SignalEAManager()
    manager.pid_file = tmp_path / "signal_ea.pid"  # never touch the real data/ folder
    manager.pid_file.write_text(str(pid), encoding="utf-8")
    manager.logger = Mock()
    return manager


def test_reused_pid_of_other_program_is_not_running(spawn, tmp_path):
    """A stale PID file whose PID now belongs to another program is not the bot (F27)."""
    other = spawn("-c", SLEEP)
    manager = _manager(tmp_path, other.pid)

    assert manager.is_running() is False
    manager.logger.warning.assert_called_once()
    message = manager.logger.warning.call_args[0][0]
    assert str(other.pid) in message
    assert "time.sleep" not in message  # never log another process's command line


def test_stop_never_kills_unrelated_process(spawn, tmp_path, monkeypatch):
    """`stop` on a reused PID sends no signal and removes the stale PID file."""
    other = spawn("-c", SLEEP)
    manager = _manager(tmp_path, other.pid)
    kill = Mock()
    monkeypatch.setattr(cli.os, "kill", kill)
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)

    manager.stop()

    kill.assert_not_called()
    assert not manager.pid_file.exists()
    assert other.poll() is None


def test_start_ignores_reused_pid(spawn, tmp_path):
    """`start` on a reused PID launches the bot instead of refusing with "already running"."""
    other = spawn("-c", SLEEP)
    manager = _manager(tmp_path, other.pid)

    with patch("cli.subprocess.Popen", return_value=Mock(pid=4242)) as mock_popen:
        manager.start()

    mock_popen.assert_called_once()
    assert manager.pid_file.read_text(encoding="utf-8") == "4242"


def test_bot_process_is_running(spawn, tmp_path, monkeypatch):
    """A process running this checkout's main.py is the bot."""
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path)
    script = tmp_path / "main.py"
    script.write_text(SLEEP, encoding="utf-8")
    bot = spawn(str(script))
    manager = _manager(tmp_path, bot.pid)

    assert manager.is_running() is True
    manager.logger.warning.assert_not_called()


def test_other_projects_main_py_is_not_the_bot(spawn, tmp_path, monkeypatch):
    """Only this checkout's main.py counts, not any script named main.py."""
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path / "project")
    other_script = tmp_path / "other" / "main.py"
    other_script.parent.mkdir()
    other_script.write_text(SLEEP, encoding="utf-8")
    other = spawn(str(other_script))
    manager = _manager(tmp_path, other.pid)

    assert manager.is_running() is False


def test_relative_main_py_of_other_project_is_not_the_bot(spawn, tmp_path, monkeypatch):
    """`python main.py` started in another folder is not the bot, even when the CLI runs from the project root."""
    project, other = tmp_path / "project", tmp_path / "other"
    project.mkdir()
    other.mkdir()
    (other / "main.py").write_text(SLEEP, encoding="utf-8")
    monkeypatch.setattr(cli, "PROJECT_ROOT", project)
    monkeypatch.chdir(project)  # cli.py is usually run from the project root
    process = spawn("main.py", cwd=other)
    manager = _manager(tmp_path, process.pid)

    assert manager.is_running() is False


@pytest.mark.parametrize(
    "error", [psutil.NoSuchProcess(1234), psutil.AccessDenied(1234), psutil.ZombieProcess(1234)]
)
def test_unreadable_process_is_not_running(error, tmp_path, monkeypatch):
    """When the command line cannot be read, assume it is not the bot: never kill on doubt."""
    manager = _manager(tmp_path, 1234)
    process = Mock()
    process.cmdline.side_effect = error
    monkeypatch.setattr(cli.psutil, "pid_exists", lambda _: True)
    monkeypatch.setattr(cli.psutil, "Process", lambda _: process)

    assert manager.is_running() is False
