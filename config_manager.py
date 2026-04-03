#!/usr/bin/env python3
"""Configuration loading, normalization, and runtime config state."""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Any

from web_ui import ensure_config_file, parse_web_ui_config

Channel = dict[str, Any]


class ConfigManager:
    """Handles config loading, validation, defaults, and runtime config state."""

    DEFAULT_CHANNELS = [
        {"name": "park_ranger_1", "ip": "239.192.49.1", "port": 60322},
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
        """Attempt to find a reasonable default multicast interface IP on Windows."""
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
        """Attempt to find a reasonable default multicast interface on Linux."""
        try:
            import importlib

            netifaces = importlib.import_module("netifaces")

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
        """Return a reasonable default multicast interface based on platform heuristics."""
        match sys.platform.lower():
            case p if p.startswith("win"):
                return (
                    iface
                    if (iface := self._get_windows_mcast_iface())
                    else self.DEFAULT_WINDOWS_MCAST_IFACE_IP
                )
            case p if p.startswith("linux"):
                return iface if (iface := self._get_linux_mcast_iface()) else "10.3.1.254"
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
