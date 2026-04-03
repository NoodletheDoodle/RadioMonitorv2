#!/usr/bin/env python3
"""Application orchestrator: startup, hot reload, web server, and graceful shutdown."""

from __future__ import annotations

import datetime
import getpass
import os
import shutil
import signal
import threading
import time
from pathlib import Path
from typing import Any

from config_manager import ConfigManager
from csv_manager import CsvManager
from network_manager import NetworkManager
from stream_manager import StreamManager
from web_ui import start_config_server

SCRIPT_DIR = Path(__file__).resolve().parent


class RadioMonitorApp:
    """Application orchestrator: startup, hot reload, web server, and graceful shutdown."""

    def __init__(self, config_path: Path) -> None:
        self.script_start_time = datetime.datetime.now()
        self.config_manager = ConfigManager(config_path)

        self.shutdown_event = threading.Event()
        self.reload_channels_event = threading.Event()

        self.call_counter_lock = threading.Lock()
        self.call_counters: dict[str, int] = {}

        self.state_lock = threading.Lock()
        self.threads: list[threading.Thread] = []
        self.web_server = None

        self.last_reload_applied_at: str | None = None
        self.last_reload_error: str | None = None
        self.startup_retry_delay_seconds = 5.0

        self.csv_manager = CsvManager(self.config_manager, self.script_start_time)
        self.stream_manager = StreamManager(
            config_manager=self.config_manager,
            csv_manager=self.csv_manager,
            shutdown_event=self.shutdown_event,
            reload_channels_event=self.reload_channels_event,
            call_counter_lock=self.call_counter_lock,
            call_counters=self.call_counters,
        )
        self.network_manager = NetworkManager()

    def generate_transcribe_service(
        self,
        service_path: Path | None = None,
        service_user: str | None = None,
        service_group: str | None = None,
    ) -> Path:
        """Generate a systemd service file using script-directory-based defaults."""
        service_dir = SCRIPT_DIR
        target_path = service_path or (service_dir / "transcribe.service")

        default_python = service_dir / ".venv" / "bin" / "python"
        python_executable = (
            default_python if default_python.exists() else Path(os.sys.executable)
        )
        script_path = service_dir / "radio_monitor.py"
        config_path = service_dir / "config.json"

        user = service_user or getpass.getuser()
        group = service_group or user

        service_text = "\n".join(
            [
                "[Unit]",
                "Description=Radio Monitor and Config Web UI",
                "After=network-online.target",
                "Wants=network-online.target",
                "",
                "[Service]",
                "Type=simple",
                f"User={user}",
                f"Group={group}",
                f"WorkingDirectory={service_dir.as_posix()}",
                "Environment=PYTHONUNBUFFERED=1",
                (
                    "ExecStart="
                    f"{python_executable.as_posix()} "
                    f"{script_path.as_posix()} "
                    f"--config {config_path.as_posix()}"
                ),
                "Restart=always",
                "RestartSec=3",
                "StartLimitIntervalSec=0",
                "KillSignal=SIGINT",
                "TimeoutStopSec=20",
                "StandardOutput=journal",
                "StandardError=journal",
                "",
                "[Install]",
                "WantedBy=multi-user.target",
                "",
            ]
        )

        target_path.write_text(service_text, encoding="utf-8")
        return target_path

    def print_startup_info(self) -> None:
        """Print startup banner and runtime summary exactly once at boot."""
        snap = self.config_manager.snapshot()

        print("=" * 60)
        print("  Radio Monitor - Starting")
        print(
            f"  Script start time : {self.script_start_time.strftime('%Y-%m-%d %H:%M:%S')}"
        )

        os.makedirs(snap["recordings_base"], exist_ok=True)
        usage = shutil.disk_usage(snap["recordings_base"])
        free_gb = usage.free / (1024**3)
        total_gb = usage.total / (1024**3)
        print(f"  Disk space        : {free_gb:.1f} GB free / {total_gb:.1f} GB total")

        print(f"  Interface IP      : {snap['multicast_interface']}")
        print(f"  Silence threshold : {snap['ptt_end_silence_threshold']}s")
        print(f"  Channels ({len(snap['channels'])}):")
        for ch in snap["channels"]:
            print(f"    [{ch['name']}]  {ch['ip']}:{ch['port']}")
        print("=" * 60)
        print("  Monitoring started. Press Ctrl+C to stop.")
        print("=" * 60)

    def handle_shutdown(self, signum: int, _frame: Any) -> None:
        print(f"\n[Shutdown] Signal {signum} received - stopping all channels...")
        self.shutdown_event.set()

    def start_channel_threads(self) -> None:
        """Launch one monitor thread per configured channel."""
        channels = self.config_manager.snapshot()["channels"]
        threads: list[threading.Thread] = []
        for channel in channels:
            t = threading.Thread(
                target=self.stream_manager.monitor_channel,
                args=(channel,),
                name=f"monitor-{channel['name']}",
                daemon=True,
            )
            t.start()
            threads.append(t)

        with self.state_lock:
            self.threads = threads

    def join_channel_threads(self, timeout: float = 10.0) -> None:
        """Join all current monitor threads using the given timeout."""
        with self.state_lock:
            old_threads = list(self.threads)

        for t in old_threads:
            t.join(timeout=timeout)

    def request_stop_channel_threads(self) -> None:
        """Signal monitor threads to stop and wait briefly for cleanup."""
        self.reload_channels_event.set()
        self.join_channel_threads()

    def restart_channel_threads(self) -> None:
        """Restart all monitor threads after channel list changes."""
        self.request_stop_channel_threads()
        self.reload_channels_event.clear()
        self.start_channel_threads()

    def stop_channel_threads(self) -> None:
        """Stop monitor threads and wait briefly for cleanup."""
        self.request_stop_channel_threads()

    def has_dead_channel_threads(self) -> bool:
        """Return True if any monitor thread exited unexpectedly."""
        with self.state_lock:
            if not self.threads:
                return False
            return any(not t.is_alive() for t in self.threads)

    def status_provider(self) -> dict[str, Any]:
        """Build read-only runtime status payload for web UI endpoint."""
        with self.state_lock:
            thread_names = [t.name for t in self.threads]
            alive_threads = sum(1 for t in self.threads if t.is_alive())
            reload_applied = self.last_reload_applied_at
            reload_error = self.last_reload_error

        snap = self.config_manager.snapshot()
        now_dt = datetime.datetime.now()
        uptime_seconds = (now_dt - self.script_start_time).total_seconds()

        return {
            "service": "radio-monitor",
            "script_start_time": self.script_start_time.isoformat(timespec="seconds"),
            "now": now_dt.isoformat(timespec="seconds"),
            "uptime_seconds": round(uptime_seconds, 1),
            "shutdown_requested": self.shutdown_event.is_set(),
            "channels_configured": [dict(ch) for ch in snap["channels"]],
            "monitor_threads": {
                "total": len(thread_names),
                "alive": alive_threads,
                "names": thread_names,
            },
            "runtime_settings": {
                "multicast_interface": snap["multicast_interface"],
                "gstreamer_bin": snap["gstreamer_bin"],
                "recordings_base": snap["recordings_base"],
                "logs_base": snap["logs_base"],
                "ptt_end_silence_threshold": snap["ptt_end_silence_threshold"],
                "poll_interval": snap["poll_interval"],
            },
            "config": {
                "path": str(self.config_manager.config_path),
                "last_reload_applied_at": reload_applied,
                "last_reload_error": reload_error,
            },
            "network": self.network_manager.get_status(),
        }

    def start_web_server(self) -> None:
        """Start embedded config/status web server when enabled in config."""
        web_cfg = self.config_manager.snapshot()["web_cfg"]
        if not web_cfg.enabled:
            self.web_server = None
            return
        try:
            self.web_server = start_config_server(
                self.config_manager.config_path,
                web_cfg,
                status_provider=self.status_provider,
            )
            print(f"  Web UI           : http://{web_cfg.host}:{web_cfg.port}")
        except OSError as exc:
            self.web_server = None
            print(
                f"[Web UI] WARNING: could not start on {web_cfg.host}:{web_cfg.port}: {exc}. "
                "Continuing without web UI."
            )

    def reload_web_server_if_needed(self, previous_web_cfg: Any) -> None:
        """Apply web server enable/host/port changes after config hot reload."""
        new_web_cfg = self.config_manager.snapshot()["web_cfg"]
        if new_web_cfg == previous_web_cfg:
            return

        if previous_web_cfg.enabled and self.web_server is not None:
            self.web_server.stop()
            self.web_server = None

        if new_web_cfg.enabled:
            try:
                self.web_server = start_config_server(
                    self.config_manager.config_path,
                    new_web_cfg,
                    status_provider=self.status_provider,
                )
            except OSError as exc:
                self.web_server = None
                print(
                    f"[Web UI] WARNING: reload could not bind {new_web_cfg.host}:{new_web_cfg.port}: {exc}. "
                    "Web UI remains disabled."
                )

    def run(self) -> int:
        """Run the main service loop with hot-reload and graceful shutdown."""
        signal.signal(signal.SIGTERM, self.handle_shutdown)
        signal.signal(signal.SIGINT, self.handle_shutdown)

        startup_attempt = 0
        while not self.shutdown_event.is_set():
            startup_attempt += 1
            try:
                self.config_manager.load_and_apply_from_disk()
                self.stream_manager.check_gstreamer()
                break
            except Exception as exc:
                print(
                    f"[Startup] Attempt {startup_attempt} failed: {exc}. "
                    f"Retrying in {self.startup_retry_delay_seconds:.0f}s..."
                )
                time.sleep(self.startup_retry_delay_seconds)

        if self.shutdown_event.is_set():
            print("[Shutdown] Startup aborted due to shutdown request.")
            return 0

        self.print_startup_info()

        self.start_channel_threads()
        self.start_web_server()

        last_config_mtime = self.config_manager.config_mtime_ns()
        next_reload_check = time.monotonic() + 1.0

        try:
            while not self.shutdown_event.is_set():
                time.sleep(0.5)
                now_mono = time.monotonic()
                if now_mono < next_reload_check:
                    continue

                next_reload_check = now_mono + 1.0
                current_mtime = self.config_manager.config_mtime_ns()
                if current_mtime == last_config_mtime:
                    if self.has_dead_channel_threads():
                        print(
                            "[Health] One or more channel monitor threads stopped unexpectedly. Restarting..."
                        )
                        self.restart_channel_threads()
                    continue

                previous_snap = self.config_manager.snapshot()
                previous_channels = previous_snap["channels"]
                previous_web_cfg = previous_snap["web_cfg"]

                try:
                    self.config_manager.load_and_apply_from_disk()
                    last_config_mtime = current_mtime
                    with self.state_lock:
                        self.last_reload_error = None
                except Exception as exc:
                    print(f"[Config] Reload rejected: {exc}")
                    with self.state_lock:
                        self.last_reload_error = str(exc)
                    continue

                current_channels = self.config_manager.snapshot()["channels"]
                if current_channels != previous_channels:
                    print(
                        "[Config] Channel list changed. Restarting channel monitor threads..."
                    )
                    self.restart_channel_threads()

                self.reload_web_server_if_needed(previous_web_cfg)

                with self.state_lock:
                    self.last_reload_applied_at = datetime.datetime.now().isoformat(
                        timespec="seconds"
                    )

                print("[Config] Hot reload applied.")

        except KeyboardInterrupt:
            self.shutdown_event.set()

        finally:
            if self.web_server is not None:
                self.web_server.stop()
            self.stop_channel_threads()

        print("[Shutdown] All channels stopped. Exiting.")
        return 0
