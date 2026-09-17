import os
import sys
from unittest.mock import patch

import cli


def test_start_uses_current_python_executable(monkeypatch):
    """Starting the app should use the current venv Python interpreter instead of a generic shell `python`."""
    manager = cli.SignalEAManager()
    monkeypatch.setattr(manager, "is_running", lambda: False)

    with patch("cli.subprocess.Popen") as mock_popen:
        manager.start()

    mock_popen.assert_called_once()
    assert mock_popen.call_args[0][0][0] == sys.executable


def test_is_running_handles_windows_invalid_pid(monkeypatch, tmp_path):
    """Status checks should treat invalid Windows PIDs as not running instead of crashing."""
    manager = cli.SignalEAManager()
    manager.pid_file = tmp_path / "signal_ea.pid"
    manager.pid_file.write_text("999999", encoding="utf-8")

    def raise_oserror(*args, **kwargs):
        raise OSError(87, "The parameter is incorrect")

    monkeypatch.setattr(os, "kill", raise_oserror)

    assert manager.is_running() is False
