#!/usr/bin/env python3
"""
radio_monitor.py

Monitors multiple PTT radio channels transmitted as Opus/RTP over UDP multicast.
Each active PTT transmission is recorded as a .WAV file via GStreamer.
Metadata for each call is logged to a per-channel daily CSV file.

PTT detection is based on WAV file size growth monitoring.
GStreamer handles all audio work via subprocess.Popen.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import getpass
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from web_ui import ensure_config_file, parse_web_ui_config, start_config_server

DEFAULT_CONFIG_PATH = Path("config.json")
SCRIPT_DIR = Path(__file__).resolve().parent

Channel = dict[str, Any]

CSV_COLUMNS = [
    "unique_id",
    "channel_name",
    "sender_ip",
    "relative_start",
    "relative_end",
    "local_start_time",
    "local_end_time",
    "duration_seconds",
    "wav_filename",
]


class ConfigManager:
    """Handles config loading, validation, defaults, and runtime config state."""

    DEFAULT_CHANNELS = [
        {"name": "park_ranger_1", "ip": "239.192.49.1", "port": 60322},
        # {"name": "park_ranger_2", "ip": "239.192.49.3", "port": 60326},
    ]
    DEFAULT_WEB_UI = {
        "enabled": True,
        "host": "0.0.0.0",
        "port": 12345,
    }
    DEFAULT_RECORDINGS_BASE = "./recordings"
    DEFAULT_LOGS_BASE = "./logs"
    DEFAULT_SILENCE_THRESHOLD = 2.0
    DEFAULT_POLL_INTERVAL = 0.5
    DEFAULT_GSTREAMER_BIN = "gst-launch-1.0"
    DEFAULT_WINDOWS_MCAST_IFACE_IP = "10.3.1.253"

    def __init__(self, config_path: Path) -> None:
        self.config_path = config_path
        self.lock = threading.Lock()

        defaults = self.default_config_dict()
        self.channels = self.validate_channels(defaults["channels"])
        self.multicast_interface = str(defaults["multicast_interface"])
        self.ptt_end_silence_threshold = float(defaults["ptt_end_silence_threshold"])
        self.poll_interval = float(defaults["poll_interval"])
        self.gstreamer_bin = str(defaults["gstreamer_bin"])
        self.recordings_base = str(defaults["recordings_base"])
        self.logs_base = str(defaults["logs_base"])
        self.web_cfg = parse_web_ui_config({})

    def _get_windows_mcast_iface(self) -> str | None:
        """Attempt to find a reasonable default multicast interface IP on Windows.
        Returns an IP address instead of an interface name, since windows multicast
        configs typically expect an IP instead of an interface name."""
        try:
            import winreg

            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SYSTEM\CurrentControlSet\Services\Tcpip\Parameters\Interfaces",
            ) as key:
                for i in range(winreg.QueryInfoKey(key)[0]):
                    subkey_name = winreg.EnumKey(key, i)
                    with winreg.OpenKey(key, subkey_name) as subkey:
                        try:
                            ip_addr = winreg.QueryValueEx(subkey, "DhcpIPAddress")[0]
                            if ip_addr and ip_addr.startswith("10."):
                                return ip_addr
                        except FileNotFoundError:
                            continue
        except Exception as exc:
            print(f"WARNING: Failed to detect Windows multicast interface IP: {exc}")
        return None

    def _get_linux_mcast_iface(self) -> str | None:
        """Attempt to find a reasonable default multicast interface IP on Linux.
        Returns the interface, rather than the IP, since linux multicast configs typically
        expect an interface name instead of an IP address."""
        try:
            import netifaces

            for iface in netifaces.interfaces():
                addrs = netifaces.ifaddresses(iface)
                if netifaces.AF_INET in addrs:
                    for addr_info in addrs[netifaces.AF_INET]:
                        ip_addr = addr_info.get("addr")
                        if ip_addr and ip_addr.startswith("10."):
                            return iface
        except ImportError:
            print(
                "WARNING: netifaces library not found, skipping Linux multicast interface auto-detection."
            )
        except Exception as exc:
            print(f"WARNING: Failed to detect Linux multicast interface IP: {exc}")
        return None

    def _get_platform_mcast_iface(self) -> str:
        """Return a reasonable default multicast interface IP based on platform heuristics."""
        match sys.platform.lower():
            case p if p.startswith("win"):
                return (
                    iface
                    if (iface := self._get_windows_mcast_iface())
                    else self.DEFAULT_WINDOWS_MCAST_IFACE_IP
                )
            case p if p.startswith("linux"):
                return iface if (iface := self._get_linux_mcast_iface()) else "eth0"
            case p if p.startswith("darwin"):
                return "en0"
            case _:
                print(
                    "WARNING: Unrecognized platform. Defaulting to multicast interface IP"
                )
                return self.DEFAULT_WINDOWS_MCAST_IFACE_IP

    def default_config_dict(self) -> dict[str, Any]:
        """Return a starter config payload that matches existing defaults."""

        return {
            "multicast_interface": self._get_platform_mcast_iface(),
            "gstreamer_bin": self.resolve_gstreamer_bin(self.DEFAULT_GSTREAMER_BIN),
            "recordings_base": self.DEFAULT_RECORDINGS_BASE,
            "logs_base": self.DEFAULT_LOGS_BASE,
            "ptt_end_silence_threshold": self.DEFAULT_SILENCE_THRESHOLD,
            "poll_interval": self.DEFAULT_POLL_INTERVAL,
            "web_ui": dict(self.DEFAULT_WEB_UI),
            "channels": [dict(channel) for channel in self.DEFAULT_CHANNELS],
        }

    def init_default_config(self) -> int:
        """Create config file with defaults when absent; return shell-style status code."""
        if self.config_path.exists():
            print(f"Config already exists: {self.config_path}")
            return 1
        self.write_config(self.default_config_dict())
        print(f"Wrote starter config to {self.config_path}")
        return 0

    def read_config(self) -> dict[str, Any]:
        """Read and normalize config.json via the shared web_ui helper."""
        return ensure_config_file(self.config_path)

    def write_config(self, raw_config: dict[str, Any]) -> None:
        """Persist config JSON in a human-readable format."""
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(
            json.dumps(raw_config, indent=2) + "\n", encoding="utf-8"
        )

    def config_mtime_ns(self) -> int:
        """Return mtime in ns for hot reload checks, or -1 if file is missing."""
        try:
            return self.config_path.stat().st_mtime_ns
        except FileNotFoundError:
            return -1

    def validate_channels(self, channels: Any) -> list[Channel]:
        """Validate and normalize configured channels."""
        if not isinstance(channels, list) or not channels:
            raise ValueError("Config must define a non-empty 'channels' list.")

        seen = set()
        normalized: list[Channel] = []

        for idx, channel in enumerate(channels):
            if not isinstance(channel, dict):
                raise ValueError(f"channels[{idx}] must be an object.")

            name = str(channel.get("name", "")).strip()
            ip = str(channel.get("ip", "")).strip()

            try:
                port = int(channel.get("port"))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"channels[{idx}].port must be an integer.") from exc

            if not name:
                raise ValueError(f"channels[{idx}] is missing required field 'name'.")
            if not ip:
                raise ValueError(f"channels[{idx}] is missing required field 'ip'.")
            if port <= 0 or port > 65535:
                raise ValueError(f"channels[{idx}].port must be in range 1..65535.")
            if name in seen:
                raise ValueError(f"Duplicate channel name '{name}'.")

            seen.add(name)
            normalized.append({"name": name, "ip": ip, "port": port})

        return normalized

    def resolve_gstreamer_bin(self, gstreamer_bin: str = "gst-launch-1.0") -> str:
        """Resolve configured GStreamer binary to an absolute executable path when possible."""
        if os.path.isfile(gstreamer_bin) and os.access(gstreamer_bin, os.X_OK):
            print(f"  GStreamer binary : {gstreamer_bin} (configured path)")
            return str(Path(gstreamer_bin).resolve())
        print(f"  GStreamer binary : {gstreamer_bin} (searching PATH)")
        fp = shutil.which(gstreamer_bin)
        if fp:
            print(f"  GStreamer binary : {fp} (found on PATH)")
            return str(Path(fp).resolve())
        raise ValueError(
            f"GStreamer binary '{gstreamer_bin}' not found in PATH and is not an executable file."
        )

    def normalize_config(self, raw_config: dict[str, Any]) -> dict[str, Any]:
        """Return validated and normalized runtime settings from raw config payload."""
        channels = self.validate_channels(raw_config.get("channels", self.channels))
        multicast_interface = str(
            raw_config.get("multicast_interface", self.multicast_interface)
        ).strip()
        gstreamer_bin = self.resolve_gstreamer_bin(
            str(raw_config.get("gstreamer_bin", self.gstreamer_bin))
        )
        recordings_base = str(
            raw_config.get("recordings_base", self.recordings_base)
        ).strip()
        logs_base = str(raw_config.get("logs_base", self.logs_base)).strip()
        silence_threshold = float(
            raw_config.get("ptt_end_silence_threshold", self.ptt_end_silence_threshold)
        )
        poll_interval = float(raw_config.get("poll_interval", self.poll_interval))

        if not multicast_interface:
            raise ValueError("'multicast_interface' cannot be empty.")
        if not gstreamer_bin:
            raise ValueError("'gstreamer_bin' cannot be empty.")
        if not recordings_base:
            raise ValueError("'recordings_base' cannot be empty.")
        if not logs_base:
            raise ValueError("'logs_base' cannot be empty.")
        if silence_threshold <= 0:
            raise ValueError("'ptt_end_silence_threshold' must be > 0.")
        if poll_interval <= 0:
            raise ValueError("'poll_interval' must be > 0.")

        return {
            "channels": channels,
            "multicast_interface": multicast_interface,
            "gstreamer_bin": gstreamer_bin,
            "recordings_base": recordings_base,
            "logs_base": logs_base,
            "ptt_end_silence_threshold": silence_threshold,
            "poll_interval": poll_interval,
            "web_cfg": parse_web_ui_config(raw_config),
        }

    def apply_normalized_config(self, normalized: dict[str, Any]) -> None:
        """Apply already-normalized runtime settings."""
        with self.lock:
            self.channels = [dict(ch) for ch in normalized["channels"]]
            self.multicast_interface = str(normalized["multicast_interface"])
            self.gstreamer_bin = str(normalized["gstreamer_bin"])
            self.recordings_base = str(normalized["recordings_base"])
            self.logs_base = str(normalized["logs_base"])
            self.ptt_end_silence_threshold = float(
                normalized["ptt_end_silence_threshold"]
            )
            self.poll_interval = float(normalized["poll_interval"])
            self.web_cfg = normalized["web_cfg"]

    def apply_config(self, raw_config: dict[str, Any]) -> None:
        """Validate and apply runtime settings from config payload."""
        self.apply_normalized_config(self.normalize_config(raw_config))

    def load_and_apply_from_disk(self) -> None:
        """Reload config from disk and apply it to current runtime state."""
        raw_config = self.read_config()
        normalized = self.normalize_config(raw_config)
        if raw_config.get("gstreamer_bin") != normalized["gstreamer_bin"]:
            raw_config["gstreamer_bin"] = normalized["gstreamer_bin"]
            self.write_config(raw_config)
        self.apply_normalized_config(normalized)

    def snapshot(self) -> dict[str, Any]:
        """Return a copy of current runtime config values for thread-safe reads."""
        with self.lock:
            return {
                "channels": [dict(ch) for ch in self.channels],
                "multicast_interface": self.multicast_interface,
                "gstreamer_bin": self.gstreamer_bin,
                "recordings_base": self.recordings_base,
                "logs_base": self.logs_base,
                "ptt_end_silence_threshold": self.ptt_end_silence_threshold,
                "poll_interval": self.poll_interval,
                "web_cfg": self.web_cfg,
            }


class CsvManager:
    """Handles all CSV pathing and writing behavior."""

    def __init__(
        self, config_manager: ConfigManager, script_start_time: datetime.datetime
    ) -> None:
        self.config_manager = config_manager
        self.script_start_time = script_start_time

    def elapsed_str(self, dt: datetime.datetime) -> str:
        """Return elapsed runtime clock string relative to script start."""
        delta = dt - self.script_start_time
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def csv_path(self, channel_name: str) -> str:
        """Return path to the per-channel run CSV file for the current day."""
        logs_base = self.config_manager.snapshot()["logs_base"]
        log_dir = os.path.join(logs_base, datetime.date.today().strftime("%Y-%m-%d"))
        os.makedirs(log_dir, exist_ok=True)
        run_ts = self.script_start_time.strftime("%Y%m%d_%H%M%S")
        return os.path.join(log_dir, f"{channel_name}_{run_ts}.csv")

    def write_row(self, channel_name: str, row_data: dict[str, Any]) -> None:
        """Append one call metadata row, creating header when file is new."""
        path = self.csv_path(channel_name)
        file_exists = os.path.isfile(path)
        with open(path, "a", newline="") as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=CSV_COLUMNS)
            if not file_exists:
                writer.writeheader()
            writer.writerow(row_data)


class StreamManager:
    """Handles GStreamer process lifecycle, channel thread loop, and WAV persistence."""

    def __init__(
        self,
        config_manager: ConfigManager,
        csv_manager: CsvManager,
        shutdown_event: threading.Event,
        reload_channels_event: threading.Event,
        call_counter_lock: threading.Lock,
        call_counters: dict[str, int],
    ) -> None:
        self.config_manager = config_manager
        self.csv_manager = csv_manager
        self.shutdown_event = shutdown_event
        self.reload_channels_event = reload_channels_event
        self.call_counter_lock = call_counter_lock
        self.call_counters = call_counters

    def recordings_dir(self, channel_name: str, recordings_base: str | None = None) -> str:
        """Return per-channel output folder for today's date."""
        base = (
            recordings_base
            if recordings_base is not None
            else self.config_manager.snapshot()["recordings_base"]
        )
        return os.path.join(
            base, datetime.date.today().strftime("%Y-%m-%d"), channel_name
        )

    def staging_wav_path(
        self, channel_name: str, recordings_base: str | None = None
    ) -> str:
        """Return temporary WAV output path used while a call is active."""
        return os.path.join(
            self.recordings_dir(channel_name, recordings_base),
            f"staging_{channel_name}.wav",
        )

    def final_wav_path(
        self,
        channel_name: str,
        start_dt: datetime.datetime,
        recordings_base: str | None = None,
    ) -> tuple[str, str]:
        """Return final WAV path and filename for a completed call."""
        timestamp = start_dt.strftime("%Y%m%d_%H%M%S")
        filename = f"{channel_name}_{timestamp}.wav"
        return (
            os.path.join(self.recordings_dir(channel_name, recordings_base), filename),
            filename,
        )

    def build_gst_command(
        self,
        channel: Channel,
        output_path: str,
        runtime_settings: dict[str, Any],
    ) -> list[str]:
        """Build gst-launch command list from current runtime config and channel."""
        return [
            runtime_settings["gstreamer_bin"],
            "udpsrc",
            f"address={channel['ip']}",
            f"port={channel['port']}",
            "auto-multicast=true",
            f"multicast-iface={runtime_settings['multicast_interface']}",
            "caps=application/x-rtp,media=audio,encoding-name=OPUS,payload=112",
            "!",
            "queue",
            "!",
            "rtpopusdepay",
            "!",
            "queue",
            "!",
            "opusdec",
            "!",
            "queue",
            "!",
            "audioconvert",
            "!",
            "audioresample",
            "!",
            "wavenc",
            "!",
            "filesink",
            f"location={output_path.replace(os.sep, '/')}",
        ]

    def launch_gstreamer(
        self,
        channel: Channel,
        output_path: str,
        runtime_settings: dict[str, Any],
    ) -> subprocess.Popen[Any]:
        """Launch gst-launch process that writes audio to output_path."""
        return subprocess.Popen(
            self.build_gst_command(channel, output_path, runtime_settings),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def terminate_gstreamer(self, proc: subprocess.Popen[Any] | None) -> None:
        """Terminate process gracefully, then kill if it does not exit quickly."""
        if proc is None or proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()

    def persist_call_with_sender(
        self,
        channel_name: str,
        call_start_dt: datetime.datetime,
        staging: str,
        sender_ip: str = "",
        recordings_base: str | None = None,
    ) -> None:
        """Finalize staging WAV into timestamped file and append CSV metadata."""
        call_end_dt = datetime.datetime.now()
        duration = (call_end_dt - call_start_dt).total_seconds()

        final_path, wav_filename = self.final_wav_path(
            channel_name, call_start_dt, recordings_base
        )
        out_dir = os.path.dirname(final_path)
        os.makedirs(out_dir, exist_ok=True)
        os.rename(staging, final_path)

        with self.call_counter_lock:
            self.call_counters[channel_name] = (
                self.call_counters.get(channel_name, 0) + 1
            )
            uid = self.call_counters[channel_name]

        self.csv_manager.write_row(
            channel_name,
            {
                "unique_id": uid,
                "channel_name": channel_name,
                "sender_ip": sender_ip,
                "relative_start": self.csv_manager.elapsed_str(call_start_dt),
                "relative_end": self.csv_manager.elapsed_str(call_end_dt),
                "local_start_time": call_start_dt.strftime("%H:%M:%S"),
                "local_end_time": call_end_dt.strftime("%H:%M:%S"),
                "duration_seconds": f"{duration:.1f}",
                "wav_filename": wav_filename,
            },
        )

    def build_sender_socket(self, channel: Channel) -> socket.socket | None:
        """Create a nonblocking UDP listener to observe sender IPs on a channel multicast stream."""
        group_ip = channel["ip"]
        port = int(channel["port"])
        iface_ip = self.config_manager.snapshot()["multicast_interface"]

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                # Some platforms require binding directly to group address.
                sock.bind((group_ip, port))

            mreq = socket.inet_aton(group_ip) + socket.inet_aton(iface_ip)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setblocking(False)
            return sock
        except OSError as exc:
            print(f"[{channel['name']}] WARNING: sender IP capture disabled ({exc}).")
            try:
                sock.close()
            except OSError:
                pass
            return None

    def ensure_sender_socket(
        self,
        channel: Channel,
        sender_sock: socket.socket | None,
        next_retry_at: float,
    ) -> tuple[socket.socket | None, float]:
        """Return a live sender socket when possible, retrying on a backoff timer."""
        if sender_sock is not None:
            return sender_sock, next_retry_at

        now = time.monotonic()
        if now < next_retry_at:
            return None, next_retry_at

        sender_sock = self.build_sender_socket(channel)
        if sender_sock is None:
            # Keep retrying if network/interface state recovers later.
            return None, now + 5.0
        return sender_sock, now

    def poll_sender_ip(self, sender_sock: socket.socket | None) -> str | None:
        """Drain available UDP packets and return the latest observed sender IP."""
        if sender_sock is None:
            return None

        latest_ip = None
        while True:
            try:
                _payload, (sender_ip, _sender_port) = sender_sock.recvfrom(4096)
                latest_ip = sender_ip
            except BlockingIOError:
                break
            except OSError:
                break
        return latest_ip

    def prepare_staging_file(self, channel_name: str, recordings_base: str) -> str:
        """Ensure channel output directory exists and staging file starts clean."""
        out_dir = self.recordings_dir(channel_name, recordings_base)
        os.makedirs(out_dir, exist_ok=True)
        staging = self.staging_wav_path(channel_name, recordings_base)
        if os.path.exists(staging):
            os.remove(staging)
        return staging

    def start_call_process(
        self,
        channel: Channel,
        channel_name: str,
        staging: str,
        runtime_settings: dict[str, Any],
    ) -> subprocess.Popen[Any] | None:
        """Start GStreamer recording process, returning None when startup fails."""
        try:
            return self.launch_gstreamer(channel, staging, runtime_settings)
        except FileNotFoundError:
            print(
                f"[{channel_name}] ERROR: gst-launch-1.0 not found. "
                "Ensure GStreamer is installed and on PATH. Retrying in 5s..."
            )
            time.sleep(5)
            return None
        except OSError as exc:
            print(
                f"[{channel_name}] ERROR launching GStreamer: {exc}. Retrying in 5s..."
            )
            time.sleep(5)
            return None

    def monitor_call_activity(
        self,
        staging: str,
        sender_sock: socket.socket | None,
    ) -> tuple[bool, str]:
        """Track growth/silence to detect call boundaries and latest sender IP."""
        last_size = 0
        last_growth_time = time.monotonic()
        call_had_audio = False
        call_sender_ip = ""

        while (
            not self.shutdown_event.is_set()
            and not self.reload_channels_event.is_set()
        ):
            snap = self.config_manager.snapshot()
            poll_interval = snap["poll_interval"]
            silence_threshold = snap["ptt_end_silence_threshold"]
            time.sleep(poll_interval)

            sender_ip = self.poll_sender_ip(sender_sock)
            if sender_ip:
                call_sender_ip = sender_ip

            try:
                current_size = os.path.getsize(staging)
            except FileNotFoundError:
                current_size = 0

            if current_size > last_size:
                last_size = current_size
                last_growth_time = time.monotonic()
                call_had_audio = True
            elif call_had_audio:
                silence_duration = time.monotonic() - last_growth_time
                if silence_duration >= silence_threshold:
                    break

        return call_had_audio, call_sender_ip

    def finalize_or_discard_call(
        self,
        channel_name: str,
        call_start_dt: datetime.datetime,
        staging: str,
        call_had_audio: bool,
        sender_ip: str,
        recordings_base: str,
    ) -> None:
        """Persist valid call audio and metadata, otherwise remove staging file."""
        if (
            not call_had_audio
            or not os.path.exists(staging)
            or os.path.getsize(staging) == 0
        ):
            if os.path.exists(staging):
                os.remove(staging)
            return

        self.persist_call_with_sender(
            channel_name,
            call_start_dt,
            staging,
            sender_ip=sender_ip,
            recordings_base=recordings_base,
        )

    def monitor_channel(self, channel: Channel) -> None:
        """Per-channel loop: record, detect end by file growth, persist call metadata."""
        name = channel["name"]
        sender_sock: socket.socket | None = None
        next_sender_sock_retry_at = 0.0

        try:
            while (
                not self.shutdown_event.is_set()
                and not self.reload_channels_event.is_set()
            ):
                try:
                    sender_sock, next_sender_sock_retry_at = self.ensure_sender_socket(
                        channel, sender_sock, next_sender_sock_retry_at
                    )

                    runtime_settings = self.config_manager.snapshot()
                    recordings_base = runtime_settings["recordings_base"]
                    staging = self.prepare_staging_file(name, recordings_base)

                    call_start_dt = datetime.datetime.now()
                    proc = self.start_call_process(
                        channel,
                        name,
                        staging,
                        runtime_settings,
                    )
                    if proc is None:
                        continue

                    call_had_audio, call_sender_ip = self.monitor_call_activity(
                        staging,
                        sender_sock,
                    )

                    self.terminate_gstreamer(proc)

                    if self.shutdown_event.is_set() or self.reload_channels_event.is_set():
                        self.finalize_or_discard_call(
                            name,
                            call_start_dt,
                            staging,
                            call_had_audio,
                            call_sender_ip,
                            recordings_base,
                        )
                        break

                    self.finalize_or_discard_call(
                        name,
                        call_start_dt,
                        staging,
                        call_had_audio,
                        call_sender_ip,
                        recordings_base,
                    )
                except Exception as exc:
                    # Keep monitor threads alive across transient socket/interface failures.
                    print(f"[{name}] WARNING: monitor loop recovered from error: {exc}")
                    if sender_sock is not None:
                        try:
                            sender_sock.close()
                        except OSError:
                            pass
                        sender_sock = None
                    next_sender_sock_retry_at = time.monotonic() + 5.0
                    time.sleep(1)
        finally:
            if sender_sock is not None:
                try:
                    sender_sock.close()
                except OSError:
                    pass

    def check_gstreamer(self) -> None:
        """Verify gst-launch is reachable before channel threads are started."""
        gst_bin = self.config_manager.snapshot()["gstreamer_bin"]
        try:
            result = subprocess.run(
                [gst_bin, "--version"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            lines = result.stdout.decode(errors="replace").splitlines()
            version_line = lines[0] if lines else "gst-launch version unknown"
            print(f"  GStreamer        : {version_line}")
        except FileNotFoundError:
            print(f"\nERROR: '{gst_bin}' not found.")
            print("  - Confirm GStreamer is installed and its bin\\ folder is on PATH.")
            print("  - Or set gstreamer_bin in config.json to the full path.")
            print(
                "  - Current PATH:\n    "
                + "\n    ".join(os.environ.get("PATH", "").split(os.pathsep))
            )
            raise SystemExit(1)


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

        self.csv_manager = CsvManager(self.config_manager, self.script_start_time)
        self.stream_manager = StreamManager(
            config_manager=self.config_manager,
            csv_manager=self.csv_manager,
            shutdown_event=self.shutdown_event,
            reload_channels_event=self.reload_channels_event,
            call_counter_lock=self.call_counter_lock,
            call_counters=self.call_counters,
        )

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
            default_python if default_python.exists() else Path(sys.executable)
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
                "Restart=on-failure",
                "RestartSec=2",
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
        }

    def start_web_server(self) -> None:
        """Start embedded config/status web server when enabled in config."""
        web_cfg = self.config_manager.snapshot()["web_cfg"]
        if not web_cfg.enabled:
            self.web_server = None
            return

        self.web_server = start_config_server(
            self.config_manager.config_path,
            web_cfg,
            status_provider=self.status_provider,
        )
        print(f"  Web UI           : http://{web_cfg.host}:{web_cfg.port}")

    def reload_web_server_if_needed(self, previous_web_cfg: Any) -> None:
        """Apply web server enable/host/port changes after config hot reload."""
        new_web_cfg = self.config_manager.snapshot()["web_cfg"]
        if new_web_cfg == previous_web_cfg:
            return

        if previous_web_cfg.enabled and self.web_server is not None:
            self.web_server.stop()
            self.web_server = None

        if new_web_cfg.enabled:
            self.web_server = start_config_server(
                self.config_manager.config_path,
                new_web_cfg,
                status_provider=self.status_provider,
            )

    def run(self) -> int:
        """Run the main service loop with hot-reload and graceful shutdown."""
        signal.signal(signal.SIGTERM, self.handle_shutdown)
        signal.signal(signal.SIGINT, self.handle_shutdown)

        self.config_manager.load_and_apply_from_disk()

        self.print_startup_info()
        self.stream_manager.check_gstreamer()

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


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor Opus/RTP multicast radio channels and record per-call WAV files."
    )
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to JSON config file (default: config.json)",
    )
    parser.add_argument(
        "--init-config",
        action="store_true",
        help="Write a starter config file and exit.",
    )
    parser.add_argument(
        "--generate-service",
        action="store_true",
        help="Generate transcribe.service and exit.",
    )
    parser.add_argument(
        "--service-path",
        default="",
        help="Optional output path for service file (default: ./transcribe.service beside radio_monitor.py).",
    )
    parser.add_argument(
        "--service-user",
        default="",
        help="Optional User= value for generated service file (default: current user).",
    )
    parser.add_argument(
        "--service-group",
        default="",
        help="Optional Group= value for generated service file (default: same as user).",
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = parse_args(argv)
    app = RadioMonitorApp(Path(args.config))

    if args.init_config:
        return app.config_manager.init_default_config()

    if args.generate_service:
        service_path = app.generate_transcribe_service(
            service_path=Path(args.service_path) if args.service_path else None,
            service_user=args.service_user or None,
            service_group=args.service_group or None,
        )
        print(f"Wrote service file to {service_path}")
        return 0

    return app.run()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
