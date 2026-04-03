#!/usr/bin/env python3
"""Network diagnostics for routes/interfaces and internet connectivity."""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import threading
import time
from typing import Any


class NetworkManager:
    """Collect and cache network status for web/runtime reporting."""

    def __init__(
        self,
        refresh_interval_seconds: float = 5.0,
        internet_probe_host: str = "1.1.1.1",
        internet_probe_port: int = 53,
        internet_probe_timeout: float = 1.5,
    ) -> None:
        self.refresh_interval_seconds = refresh_interval_seconds
        self.internet_probe_host = internet_probe_host
        self.internet_probe_port = internet_probe_port
        self.internet_probe_timeout = internet_probe_timeout
        self._lock = threading.Lock()
        self._last_refresh = 0.0
        self._cached: dict[str, Any] = {
            "interfaces": [],
            "routes": [],
            "default_routes": [],
            "internet": {
                "connected": False,
                "probe": f"{internet_probe_host}:{internet_probe_port}",
            },
        }

    def _run(self, args: list[str], timeout: float = 2.0) -> str:
        """Run a command with timeout and return stdout or empty string."""
        try:
            proc = subprocess.run(
                args,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode == 0:
                return proc.stdout
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return ""
        return ""

    def _check_internet(self) -> dict[str, Any]:
        """Check whether outbound internet is reachable via a short TCP probe."""
        start = time.monotonic()
        try:
            with socket.create_connection(
                (self.internet_probe_host, self.internet_probe_port),
                timeout=self.internet_probe_timeout,
            ):
                latency_ms = round((time.monotonic() - start) * 1000.0, 1)
                return {
                    "connected": True,
                    "latency_ms": latency_ms,
                    "probe": f"{self.internet_probe_host}:{self.internet_probe_port}",
                }
        except OSError as exc:
            latency_ms = round((time.monotonic() - start) * 1000.0, 1)
            return {
                "connected": False,
                "latency_ms": latency_ms,
                "probe": f"{self.internet_probe_host}:{self.internet_probe_port}",
                "error": str(exc),
            }

    def _collect_linux(self) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
        interfaces: list[dict[str, str]] = []
        routes: list[dict[str, str]] = []
        defaults: list[dict[str, str]] = []

        addr_output = self._run(["ip", "-o", "-4", "addr", "show"])
        for line in addr_output.splitlines():
            match = re.search(
                r"^\d+:\s+([^\s:]+).*\binet\s+((?:\d{1,3}\.){3}\d{1,3})/",
                line,
            )
            if match:
                interfaces.append({"name": match.group(1), "ip": match.group(2)})

        route_output = self._run(["ip", "-4", "route", "show"])
        for line in route_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            dev_match = re.search(r"\bdev\s+(\S+)", stripped)
            via_match = re.search(r"\bvia\s+((?:\d{1,3}\.){3}\d{1,3})", stripped)
            src_match = re.search(r"\bsrc\s+((?:\d{1,3}\.){3}\d{1,3})", stripped)
            if stripped.startswith("default"):
                defaults.append(
                    {
                        "destination": "default",
                        "gateway": via_match.group(1) if via_match else "",
                        "interface": dev_match.group(1) if dev_match else "",
                        "source": src_match.group(1) if src_match else "",
                    }
                )
            routes.append(
                {
                    "destination": stripped.split()[0],
                    "gateway": via_match.group(1) if via_match else "",
                    "interface": dev_match.group(1) if dev_match else "",
                    "source": src_match.group(1) if src_match else "",
                    "raw": stripped,
                }
            )

        return interfaces, routes, defaults

    def _collect_windows(self) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, str]]]:
        interfaces: list[dict[str, str]] = []
        routes: list[dict[str, str]] = []
        defaults: list[dict[str, str]] = []

        ip_to_iface: dict[str, str] = {}
        ipconfig_output = self._run(["ipconfig"])
        current_iface = ""
        for line in ipconfig_output.splitlines():
            stripped = line.strip()
            if stripped.endswith(":"):
                current_iface = stripped[:-1]
                continue
            match = re.search(r"IPv4[^:]*:\s*([0-9.]+)", stripped)
            if match and current_iface:
                ip = match.group(1)
                interfaces.append({"name": current_iface, "ip": ip})
                ip_to_iface[ip] = current_iface

        route_output = self._run(["route", "print", "-4"])
        route_ip_regex = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b")
        for line in route_output.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("="):
                continue
            ips = route_ip_regex.findall(stripped)
            if len(ips) < 4:
                continue
            dest, mask, gateway, iface_ip = ips[0], ips[1], ips[2], ips[3]
            iface_name = ip_to_iface.get(iface_ip, "windows_route")
            route_row = {
                "destination": dest,
                "mask": mask,
                "gateway": gateway,
                "interface": iface_name,
                "interface_ip": iface_ip,
                "raw": stripped,
            }
            routes.append(route_row)
            if dest == "0.0.0.0" and mask == "0.0.0.0":
                defaults.append(route_row)

        return interfaces, routes, defaults

    def _collect(self) -> dict[str, Any]:
        if os.name == "nt":
            interfaces, routes, defaults = self._collect_windows()
        else:
            interfaces, routes, defaults = self._collect_linux()

        deduped = []
        seen = set()
        for item in interfaces:
            key = (item.get("name", ""), item.get("ip", ""))
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)

        return {
            "platform": sys.platform,
            "interfaces": deduped,
            "routes": routes,
            "default_routes": defaults,
            "internet": self._check_internet(),
        }

    def get_status(self) -> dict[str, Any]:
        """Return cached status; refresh when cache is stale."""
        now = time.monotonic()
        with self._lock:
            if now - self._last_refresh < self.refresh_interval_seconds:
                return dict(self._cached)

            self._cached = self._collect()
            self._last_refresh = now
            return dict(self._cached)
