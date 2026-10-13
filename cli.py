#!/usr/bin/env python3
"""
Command line interface for Telegram Signal EA.
Provides start, stop, status, and configuration commands.

Broker instances: profiles in config/brokers.json (see brokers.example.json). `start` with no name
shows a menu; each chosen broker runs as its own bot process (BOT_INSTANCE=<key>) with its own
MT5 terminal, data folder, database, logs and Telegram session.
"""

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil

# Add src to path for imports
sys.path.insert(0, str(Path(__file__).parent / "src"))

from config.brokers import load_brokers, parse_selection
from config.logging_config import get_logger, setup_logging

PROJECT_ROOT = Path(__file__).resolve().parent


class SignalEAManager:
    """Manages one Telegram Signal EA process (one broker instance, or the single legacy setup)."""

    def __init__(self, instance: str | None = None):
        self.logger = get_logger(__name__)
        self.instance = instance
        self.name = f"Signal EA [{instance}]" if instance else "Signal EA"
        data_dir = PROJECT_ROOT / "data" / instance if instance else PROJECT_ROOT / "data"
        self.pid_file = data_dir / "signal_ea.pid"
        self.session_file = data_dir / "telegram.session"

    def start(self) -> None:
        """Start the Signal EA application."""
        if self.is_running():
            self.logger.warning(f"{self.name} is already running")
            print(f"{self.name} is already running")
            return

        self.logger.info(f"Starting {self.name}")
        print(f"Starting {self.name}...")

        cmd = [sys.executable, str(PROJECT_ROOT / "main.py")]
        env = dict(os.environ)
        if self.instance:
            env["BOT_INSTANCE"] = self.instance
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)

        if sys.platform == "win32":
            # Own console window: shows this broker's dashboard and lets you type the Telegram
            # login code on the first run of a new instance.
            process = subprocess.Popen(
                cmd, cwd=str(PROJECT_ROOT), env=env, creationflags=subprocess.CREATE_NEW_CONSOLE
            )
        else:
            process = subprocess.Popen(
                cmd,
                cwd=str(PROJECT_ROOT),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

        self.pid_file.write_text(str(process.pid), encoding="utf-8")
        print(f"{self.name} started (PID: {process.pid})")
        if self.instance and not self.session_file.exists():
            print(f"  First run of '{self.instance}': enter the Telegram login code in its window.")

    def stop(self) -> None:
        """Stop the Signal EA application."""
        if not self.is_running():
            self.logger.warning(f"{self.name} is not running")
            print(f"{self.name} is not running")
            self.pid_file.unlink(missing_ok=True)
            return

        pid = self.get_pid()
        if pid:
            try:
                os.kill(pid, signal.SIGTERM)
                self.logger.info(f"Sent SIGTERM to process {pid}")
                print(f"Stopping {self.name} (PID: {pid})")

                for _ in range(10):  # Wait up to 10 seconds
                    if not self.is_running():
                        break
                    time.sleep(1)

                if self.is_running():
                    # Force kill if still running (no SIGKILL on Windows: SIGTERM already terminates)
                    os.kill(pid, getattr(signal, "SIGKILL", signal.SIGTERM))
                    self.logger.warning(f"Force killed process {pid}")
                    print(f"Force stopped {self.name}")
                else:
                    print(f"{self.name} stopped successfully")

            except ProcessLookupError:
                self.logger.info(f"Process {pid} not found")
                print("Process not found (already stopped)")
            except PermissionError:
                self.logger.error(f"Permission denied killing process {pid}")
                print("Permission denied - cannot stop process")

        self.pid_file.unlink(missing_ok=True)

    def status(self) -> None:
        """Show Signal EA application status."""
        if self.is_running():
            print(f"{self.name} is running (PID: {self.get_pid()})")
        else:
            print(f"{self.name} is not running")

    def is_running(self) -> bool:
        """Check if Signal EA is currently running: the PID file's process must run this main.py."""
        pid = self.get_pid()
        # psutil, not os.kill(pid, 0): on Windows signal 0 is CTRL_C_EVENT and would stop the bot
        if pid is None or not psutil.pid_exists(pid):
            return False
        # Windows reuses PIDs: after a crash the stale file can name another program (e.g. MT5).
        # Exact match on the absolute path start() passes; no abspath(): a relative "main.py" of
        # another process would resolve against this CLI's cwd, not that process's.
        main_script = os.path.normcase(str(PROJECT_ROOT / "main.py"))
        try:
            process = psutil.Process(pid)
            if any(os.path.normcase(arg) == main_script for arg in process.cmdline()):
                return True
            name = process.name()
        except psutil.Error:
            return False  # cannot tell: never treat it as the bot (stop would kill it)
        self.logger.warning(
            f"PID {pid} in {self.pid_file} is not the bot ({name}); ignoring stale PID file"
        )
        return False

    def get_pid(self) -> int | None:
        """Get the PID of the running Signal EA process."""
        try:
            return int(self.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None


def _menu(brokers: dict[str, dict]) -> list[str]:
    """Ask which configured brokers to start."""
    keys = list(brokers)
    print("Configured brokers:")
    for i, key in enumerate(keys, start=1):
        profile = brokers[key]
        print(
            f"  {i}) {profile.get('label', key)}  [{key}, {profile.get('mode', '?').upper()} {profile.get('login', '?')}]"
        )
    while True:
        try:
            return parse_selection(input("Start which? (e.g. 1  or  1,3  or  all): "), keys)
        except ValueError as e:
            print(e)


def _targets(names: list[str], brokers: dict[str, dict], command: str) -> list[str | None]:
    """Instances a command applies to. No broker profiles -> the single legacy instance (None)."""
    if not brokers:
        if names:
            raise ValueError(
                "No brokers configured: copy config/brokers.example.json to config/brokers.json"
            )
        return [None]
    if names:
        return parse_selection(" ".join(names), list(brokers))
    if command == "start":
        return _menu(brokers)
    return list(brokers)  # stop / status without names: every broker


def main() -> None:
    """CLI main function."""
    parser = argparse.ArgumentParser(
        description="Telegram Signal EA - Automated trading system",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")
    for command, help_text in (
        ("start", "Start broker instance(s); no name = choose from a menu"),
        ("stop", "Stop broker instance(s); no name = all"),
        ("status", "Show status; no name = all"),
    ):
        sub = subparsers.add_parser(command, help=help_text)
        sub.add_argument("brokers", nargs="*", help="broker keys or menu numbers, or 'all'")
        if command == "start":
            sub.add_argument("--daemon", action="store_true", help="Run in daemon mode")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    # Setup logging for CLI operations
    setup_logging()

    try:
        for instance in _targets(args.brokers, load_brokers(), args.command):
            getattr(SignalEAManager(instance), args.command)()
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
