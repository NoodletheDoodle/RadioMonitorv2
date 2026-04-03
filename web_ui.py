#!/usr/bin/env python3
"""Small HTTP UI for viewing and editing the capture config JSON."""

from __future__ import annotations

import csv
import io
import os
import json
import re
import socket
import subprocess
import sys
import threading
import zipfile
from datetime import datetime
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlparse


# Web UI runtime settings used by the embedded server.
@dataclass(frozen=True)
class WebUiConfig:
    """Host/port and enable flag for the config web interface."""

    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 12345


DEFAULT_CHANNEL = {
    "name": "park_ranger_1",
    "ip": "239.192.49.1",
    "port": 60322,
}


# Default config generation and normalization helpers.
def default_config_dict() -> dict[str, Any]:
    """Return a full default config dictionary."""

    return {
        "multicast_interface": "10.3.1.253",
        "gstreamer_bin": "gst-launch-1.0",
        "recordings_base": "./recordings",
        "logs_base": "./logs",
        "ptt_end_silence_threshold": 2.0,
        "poll_interval": 0.5,
        "web_ui": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": 12345,
        },
        "channels": [dict(DEFAULT_CHANNEL)],
    }


def get_ip_information() -> dict[str, Any]:
    """Gather local network interface information quickly on Linux and Windows."""
    by_interface: dict[str, set[str]] = {}
    ip_to_interface: dict[str, str] = {}

    def add_ip(name: str, ip: str) -> None:
        iface = str(name).strip() or "unknown"
        addr = str(ip).strip()
        if not addr:
            return
        if addr.startswith("127."):
            return
        by_interface.setdefault(iface, set()).add(addr)
        ip_to_interface.setdefault(addr, iface)

    # Fast hostname lookup; avoids repeated per-interface DNS resolution.
    try:
        hostname = socket.gethostname()
        for ip in socket.gethostbyname_ex(hostname)[2]:
            add_ip(hostname, ip)
    except (socket.error, OSError):
        pass

    if os.name == "nt":
        # Windows: parse ipconfig for adapter/IP pairs.
        try:
            proc = subprocess.run(
                ["ipconfig"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if proc.returncode == 0:
                current_iface = ""
                for line in proc.stdout.splitlines():
                    stripped = line.strip()
                    if stripped.endswith(":"):
                        current_iface = stripped[:-1]
                        continue
                    match = re.search(r"IPv4[^:]*:\s*([0-9.]+)", stripped)
                    if match and current_iface:
                        add_ip(current_iface, match.group(1))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

        # Windows route table can expose routed interface IPs quickly.
        try:
            proc = subprocess.run(
                ["route", "print", "-4"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if proc.returncode == 0:
                route_ip_regex = re.compile(r"\b((?:\d{1,3}\.){3}\d{1,3})\b")
                for line in proc.stdout.splitlines():
                    if not line.strip() or line.lstrip().startswith("="):
                        continue
                    ips = route_ip_regex.findall(line)
                    if len(ips) >= 4:
                        iface_ip = ips[2]
                        # Keep the existing adapter name if known, else mark as routed.
                        add_ip(ip_to_interface.get(iface_ip, "windows_route"), iface_ip)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass
    else:
        # Linux: parse local IPv4 addresses per interface.
        try:
            proc = subprocess.run(
                ["ip", "-o", "-4", "addr", "show"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    # Example: "2: eth0    inet 192.168.1.10/24 ..."
                    match = re.search(
                        r"^\d+:\s+([^\s:]+).*\binet\s+((?:\d{1,3}\.){3}\d{1,3})/",
                        line,
                    )
                    if match:
                        add_ip(match.group(1), match.group(2))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

        # Linux routes can include interfaces that are currently link-down but still routed.
        try:
            proc = subprocess.run(
                ["ip", "-4", "route", "show"],
                check=False,
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            if proc.returncode == 0:
                for line in proc.stdout.splitlines():
                    dev_match = re.search(r"\bdev\s+(\S+)", line)
                    src_match = re.search(r"\bsrc\s+((?:\d{1,3}\.){3}\d{1,3})", line)
                    if dev_match and src_match:
                        add_ip(dev_match.group(1), src_match.group(1))
        except (OSError, ValueError, subprocess.TimeoutExpired):
            pass

    interfaces: list[dict[str, str]] = []
    for iface_name in sorted(by_interface.keys()):
        for ip in sorted(by_interface[iface_name]):
            interfaces.append({"name": iface_name, "ip": ip})

    if not interfaces:
        interfaces.append({"name": "localhost", "ip": "127.0.0.1"})

    return {"interfaces": interfaces}


def normalize_config(raw: dict[str, Any]) -> dict[str, Any]:
    """Merge user config with defaults and coerce value types."""

    defaults = default_config_dict()
    cfg = {
        "multicast_interface": str(
            raw.get("multicast_interface", defaults["multicast_interface"])
        ),
        "gstreamer_bin": str(raw.get("gstreamer_bin", defaults["gstreamer_bin"])),
        "recordings_base": str(raw.get("recordings_base", defaults["recordings_base"])),
        "logs_base": str(raw.get("logs_base", defaults["logs_base"])),
        "ptt_end_silence_threshold": float(
            raw.get("ptt_end_silence_threshold", defaults["ptt_end_silence_threshold"])
        ),
        "poll_interval": float(raw.get("poll_interval", defaults["poll_interval"])),
    }

    web_raw = raw.get("web_ui", {})
    if not isinstance(web_raw, dict):
        web_raw = {}
    cfg["web_ui"] = {
        "enabled": bool(web_raw.get("enabled", defaults["web_ui"]["enabled"])),
        "host": str(web_raw.get("host", defaults["web_ui"]["host"])),
        "port": int(web_raw.get("port", defaults["web_ui"]["port"])),
    }

    channels_raw = raw.get("channels", [])
    channels: list[dict[str, Any]] = []
    if isinstance(channels_raw, list):
        for item in channels_raw:
            if isinstance(item, dict):
                ch = dict(DEFAULT_CHANNEL)
                ch.update(item)
                ch["name"] = str(ch.get("name", DEFAULT_CHANNEL["name"]))
                ch["ip"] = str(ch.get("ip", DEFAULT_CHANNEL["ip"]))
                ch["port"] = int(ch.get("port", DEFAULT_CHANNEL["port"]))
                channels.append(ch)
    if not channels:
        channels = [dict(DEFAULT_CHANNEL)]
    cfg["channels"] = channels
    return cfg


def write_config(path: Path, cfg: dict[str, Any]) -> None:
    """Persist config JSON atomically with indentation."""

    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(cfg, indent=2) + "\n")


def ensure_config_file(path: Path) -> dict[str, Any]:
    """Create or normalize config file, returning effective config."""

    if not path.exists():
        cfg = default_config_dict()
        write_config(path, cfg)
        return cfg

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        cfg = default_config_dict()
        write_config(path, cfg)
        return cfg

    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a JSON object.")
    cfg = normalize_config(raw)
    if cfg != raw:
        write_config(path, cfg)
    return cfg


def parse_web_ui_config(raw: dict[str, Any]) -> WebUiConfig:
    """Extract web-ui-specific settings from raw config."""

    cfg = normalize_config(raw)
    web_raw = cfg.get("web_ui", {})
    return WebUiConfig(
        enabled=bool(web_raw.get("enabled", True)),
        host=str(web_raw.get("host", "0.0.0.0")),
        port=int(web_raw.get("port", 12345)),
    )


def validate_config_shape(raw: dict[str, Any]) -> None:
    """Validate minimal schema required by capture runtime."""

    channels = raw.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("Config must include a non-empty 'channels' list.")

    for idx, channel in enumerate(channels):
        if not isinstance(channel, dict):
            raise ValueError(f"channels[{idx}] must be an object.")
        if "name" not in channel or not str(channel["name"]).strip():
            raise ValueError(f"channels[{idx}] is missing required field 'name'.")
        if "ip" not in channel or not str(channel["ip"]).strip():
            raise ValueError(f"channels[{idx}] is missing required field 'ip'.")
        if "port" not in channel:
            raise ValueError(f"channels[{idx}] is missing required field 'port'.")
        try:
            port = int(channel["port"])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"channels[{idx}].port must be an integer.") from exc
        if port <= 0 or port > 65535:
            raise ValueError(f"channels[{idx}].port must be in range 1..65535.")


# HTTP response and file write utilities.
def _json_response(
    handler: BaseHTTPRequestHandler, code: int, payload: dict[str, Any]
) -> None:
    """Send a JSON response with explicit content headers."""

    data = json.dumps(payload, indent=2).encode("utf-8")
    handler.send_response(code)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _html_response(handler: BaseHTTPRequestHandler, html: str) -> None:
    """Send an HTML response page."""

    data = html.encode("utf-8")
    handler.send_response(HTTPStatus.OK)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def _atomic_write(path: Path, content: str) -> None:
    """Write file content atomically via temp-file replace."""

    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# Embedded threaded HTTP server with form-based config UI.
class ConfigWebServer:
    """Serve config editor UI and REST endpoints."""

    def __init__(
        self,
        config_path: Path,
        host: str,
        port: int,
        status_provider: Callable[[], dict[str, Any]] | None = None,
    ) -> None:
        """Initialize server state and build HTTP handler."""

        self.config_path = config_path
        self.host = host
        self.port = port
        self.status_provider = status_provider
        self._lock = threading.Lock()
        self._httpd = self._build_server()
        self._thread: threading.Thread | None = None

    def _build_server(self) -> ThreadingHTTPServer:
        """Create ThreadingHTTPServer instance with local handlers."""

        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                """Handle UI page, config read endpoint, and health check."""

                if self.path == "/":
                    _html_response(self, owner._render_html())
                    return
                if self.path == "/api/config":
                    owner._handle_get_config(self)
                    return
                if self.path == "/api/recordings":
                    owner._handle_get_recordings(self)
                    return
                if self.path.startswith("/api/download"):
                    owner._handle_download(self)
                    return
                if self.path == "/api/status":
                    owner._handle_get_status(self)
                    return
                if self.path == "/api/network":
                  owner._handle_get_network(self)
                  return
                if self.path == "/api/network/interfaces":
                  owner._handle_get_network_interfaces(self)
                  return
                if self.path == "/api/network/routes":
                  owner._handle_get_network_routes(self)
                  return
                if self.path == "/api/network/internet":
                  owner._handle_get_network_internet(self)
                  return
                if self.path == "/health":
                    _json_response(self, HTTPStatus.OK, {"status": "ok"})
                    return
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not Found"})

            def do_POST(self) -> None:  # noqa: N802
                """Handle config update requests from the UI."""

                if self.path == "/api/config":
                    owner._handle_post_config(self)
                    return
                if self.path == "/api/download-batch":
                    owner._handle_download_batch(self)
                    return
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "Not Found"})

            def log_message(self, format: str, *args: Any) -> None:
                """Suppress default HTTP access logging."""

                return

        return ThreadingHTTPServer((self.host, self.port), Handler)

    def _read_raw(self) -> dict[str, Any]:
        """Return current effective config from disk."""

        return ensure_config_file(self.config_path)

    def _render_html(self) -> str:
        """Render single-page HTML form used to edit config values."""
        ui_path = Path(__file__).with_name("ui.html")
        try:
            return ui_path.read_text(encoding="utf-8")
        except OSError as exc:
            return (
                "<!doctype html><html><body><h1>UI file missing</h1>"
                f"<pre>{exc}</pre></body></html>"
            )

    def _network_snapshot(self) -> dict[str, Any]:
        """Return network status from runtime provider or a lightweight fallback."""

        fallback: dict[str, Any] = {
            "interfaces": get_ip_information().get("interfaces", []),
            "routes": [],
            "default_routes": [],
            "internet": {
                "connected": False,
                "probe": "unavailable",
                "error": "status provider unavailable",
            },
        }

        if self.status_provider is None:
            return fallback

        try:
            payload = self.status_provider()
        except Exception as exc:
            fallback["internet"]["error"] = str(exc)
            return fallback

        if not isinstance(payload, dict):
            return fallback
        network = payload.get("network")
        if not isinstance(network, dict):
            return fallback
        return network

    def _handle_get_network(self, handler: BaseHTTPRequestHandler) -> None:
        """Return full network diagnostics payload."""

        _json_response(handler, HTTPStatus.OK, self._network_snapshot())

    def _handle_get_network_interfaces(self, handler: BaseHTTPRequestHandler) -> None:
        """Return only network interface details."""

        snap = self._network_snapshot()
        _json_response(
            handler,
            HTTPStatus.OK,
            {"interfaces": snap.get("interfaces", [])},
        )

    def _handle_get_network_routes(self, handler: BaseHTTPRequestHandler) -> None:
        """Return route table details and default routes."""

        snap = self._network_snapshot()
        _json_response(
            handler,
            HTTPStatus.OK,
            {
                "routes": snap.get("routes", []),
                "default_routes": snap.get("default_routes", []),
            },
        )

    def _handle_get_network_internet(self, handler: BaseHTTPRequestHandler) -> None:
        """Return internet reachability status only."""

        snap = self._network_snapshot()
        _json_response(
            handler,
            HTTPStatus.OK,
            {"internet": snap.get("internet", {})},
        )

    def _handle_get_config(self, handler: BaseHTTPRequestHandler) -> None:
        """Return current config payload as JSON."""

        try:
            with self._lock:
                raw = self._read_raw()
        except Exception as exc:
            _json_response(
                handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
            )
            return
        _json_response(
            handler, HTTPStatus.OK, {"config": raw, "path": str(self.config_path)}
        )

    def _handle_get_status(self, handler: BaseHTTPRequestHandler) -> None:
        """Return read-only runtime status payload as JSON."""

        payload: dict[str, Any] = {
            "status": "ok",
            "service": "config-web-ui",
            "config_path": str(self.config_path),
        }
        if self.status_provider is not None:
            try:
                runtime_payload = self.status_provider()
                if isinstance(runtime_payload, dict):
                    payload["runtime"] = runtime_payload
                else:
                    payload["runtime"] = {
                        "warning": "status provider returned non-object payload"
                    }
            except Exception as exc:
                _json_response(
                    handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
                )
                return
        _json_response(handler, HTTPStatus.OK, payload)

    def _resolve_data_roots(self) -> tuple[Path, Path]:
        """Return absolute recordings and logs base directories from config."""

        cfg = self._read_raw()
        config_dir = self.config_path.parent.resolve()
        recordings_base = cfg.get("recordings_base", "./recordings")
        logs_base = cfg.get("logs_base", "./logs")

        recordings_root = (config_dir / str(recordings_base)).resolve()
        logs_root = (config_dir / str(logs_base)).resolve()
        return recordings_root, logs_root

    def _resolve_data_roots_threadsafe(self) -> tuple[Path, Path]:
        """Resolve recordings/log roots under lock for thread-safe config reads."""

        with self._lock:
            return self._resolve_data_roots()

    def _safe_child_path(self, root: Path, relative_path: str) -> Path:
        """Resolve a user-supplied relative path under root or raise ValueError."""

        if not relative_path.strip():
            raise ValueError("Missing path.")

        normalized = relative_path.replace("\\", "/").lstrip("/")
        candidate = (root / normalized).resolve()
        if not candidate.is_relative_to(root):
            raise ValueError("Path is outside allowed directory.")
        return candidate

    @staticmethod
    def _row_text(row: dict[str, Any], key: str) -> str:
        """Return a stripped string value from a CSV row dict."""

        return str(row.get(key, "")).strip()

    @staticmethod
    def _download_url(kind: str, rel_path: str) -> str:
        """Build a download URL for a validated relative path."""

        return f"/api/download?kind={kind}&path={quote(rel_path, safe='/')}"

    def _recording_row_from_csv(
        self,
        csv_path: Path,
        log_rel: str,
        row: dict[str, Any],
        recordings_root: Path,
    ) -> dict[str, Any]:
        """Build one recordings payload row from CSV metadata and file lookup."""

        date_fragment = csv_path.parent.name
        channel_name = self._row_text(row, "channel_name")
        wav_filename = self._row_text(row, "wav_filename")

        wav_rel = ""
        wav_exists = False
        if date_fragment and channel_name and wav_filename:
            wav_rel = f"{date_fragment}/{channel_name}/{wav_filename}"
            wav_path = (recordings_root / wav_rel).resolve()
            wav_exists = wav_path.is_file() and wav_path.is_relative_to(recordings_root)

        return {
            "unique_id": self._row_text(row, "unique_id"),
            "channel_name": channel_name,
            "sender_ip": self._row_text(row, "sender_ip"),
            "relative_start": self._row_text(row, "relative_start"),
            "relative_end": self._row_text(row, "relative_end"),
            "local_start_time": self._row_text(row, "local_start_time"),
            "local_end_time": self._row_text(row, "local_end_time"),
            "duration_seconds": self._row_text(row, "duration_seconds"),
            "wav_filename": wav_filename,
            "recording_path": wav_rel,
            "recording_exists": wav_exists,
            "recording_download_url": (
                self._download_url("recording", wav_rel) if wav_exists else ""
            ),
            "log_csv_path": log_rel,
            "log_csv_download_url": self._download_url("log", log_rel),
        }

    def _handle_get_recordings(self, handler: BaseHTTPRequestHandler) -> None:
        """Return flattened recording rows derived from per-channel CSV logs."""

        try:
            recordings_root, logs_root = self._resolve_data_roots_threadsafe()
            rows: list[dict[str, Any]] = []
            if logs_root.exists():
                for csv_path in sorted(logs_root.rglob("*.csv"), reverse=True):
                    log_rel = csv_path.relative_to(logs_root).as_posix()
                    try:
                        with csv_path.open("r", newline="", encoding="utf-8") as fh:
                            reader = csv.DictReader(fh)
                            csv_rows = [
                                self._recording_row_from_csv(
                                    csv_path,
                                    log_rel,
                                    row,
                                    recordings_root,
                                )
                                for row in reader
                            ]
                            rows.extend(reversed(csv_rows))
                    except OSError:
                        # Skip unreadable log files but keep the endpoint responsive.
                        continue

            _json_response(
                handler,
                HTTPStatus.OK,
                {
                    "ok": True,
                    "recordings_base": str(recordings_root),
                    "logs_base": str(logs_root),
                    "count": len(rows),
                    "rows": rows,
                },
            )
        except Exception as exc:
            _json_response(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(exc)},
            )

    def _handle_download(self, handler: BaseHTTPRequestHandler) -> None:
        """Serve files from recordings/log directories via validated relative paths."""

        try:
            parsed = urlparse(handler.path)
            params = parse_qs(parsed.query)
            kind = str(params.get("kind", [""])[0]).strip().lower()
            rel_path = str(params.get("path", [""])[0]).strip()
            if kind not in {"recording", "log"}:
                raise ValueError("Invalid kind. Use 'recording' or 'log'.")

            recordings_root, logs_root = self._resolve_data_roots_threadsafe()

            root = recordings_root if kind == "recording" else logs_root
            target = self._safe_child_path(root, rel_path)
            if not target.exists() or not target.is_file():
                _json_response(handler, HTTPStatus.NOT_FOUND, {"error": "Not Found"})
                return

            data = target.read_bytes()
            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "application/octet-stream")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header(
                "Content-Disposition", f'attachment; filename="{target.name}"'
            )
            handler.end_headers()
            handler.wfile.write(data)
        except ValueError as exc:
            _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            _json_response(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(exc)},
            )

    def _handle_download_batch(self, handler: BaseHTTPRequestHandler) -> None:
        """Bundle filtered files into a ZIP archive and return as attachment."""

        length_raw = handler.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except ValueError:
            length = 0

        if length <= 0:
            _json_response(
                handler, HTTPStatus.BAD_REQUEST, {"error": "Empty request body."}
            )
            return

        try:
            body = handler.rfile.read(length)
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("JSON root must be an object.")

            kind = str(payload.get("kind", "")).strip().lower()
            if kind not in {"recording", "log"}:
                raise ValueError("Invalid kind. Use 'recording' or 'log'.")

            paths_raw = payload.get("paths")
            if not isinstance(paths_raw, list):
                raise ValueError("'paths' must be a JSON array.")

            recordings_root, logs_root = self._resolve_data_roots_threadsafe()
            root = recordings_root if kind == "recording" else logs_root

            files: list[tuple[str, Path]] = []
            seen: set[str] = set()
            for item in paths_raw:
                rel_path = str(item).strip()
                if not rel_path or rel_path in seen:
                    continue
                seen.add(rel_path)
                target = self._safe_child_path(root, rel_path)
                if target.is_file():
                    files.append((rel_path, target))

            if not files:
                _json_response(
                    handler,
                    HTTPStatus.NOT_FOUND,
                    {"error": "No matching files found for download."},
                )
                return

            zip_buffer = io.BytesIO()
            with zipfile.ZipFile(
                zip_buffer, "w", compression=zipfile.ZIP_DEFLATED
            ) as zf:
                for rel_path, target in files:
                    zf.write(target, arcname=rel_path.replace("\\", "/"))

            data = zip_buffer.getvalue()
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            suffix = "audio" if kind == "recording" else "csv"
            filename = f"{suffix}_filtered_{stamp}.zip"

            handler.send_response(HTTPStatus.OK)
            handler.send_header("Content-Type", "application/zip")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header(
                "Content-Disposition", f'attachment; filename="{filename}"'
            )
            handler.end_headers()
            handler.wfile.write(data)
        except ValueError as exc:
            _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        except Exception as exc:
            _json_response(
                handler,
                HTTPStatus.INTERNAL_SERVER_ERROR,
                {"error": str(exc)},
            )

    def _handle_post_config(self, handler: BaseHTTPRequestHandler) -> None:
        """Validate and persist posted config payload."""

        length_raw = handler.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except ValueError:
            length = 0
        if length <= 0:
            _json_response(
                handler, HTTPStatus.BAD_REQUEST, {"error": "Empty request body."}
            )
            return
        body = handler.rfile.read(length)
        try:
            parsed = json.loads(body.decode("utf-8"))
            if not isinstance(parsed, dict):
                raise ValueError("JSON root must be an object.")
            parsed = normalize_config(parsed)
            validate_config_shape(parsed)
        except Exception as exc:
            _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return

        try:
            with self._lock:
                write_config(self.config_path, parsed)
        except Exception as exc:
            _json_response(
                handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)}
            )
            return

        _json_response(
            handler,
            HTTPStatus.OK,
            {"ok": True, "path": str(self.config_path), "config": parsed},
        )

    def start(self) -> None:
        """Start web server in a daemon thread."""

        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop web server and wait briefly for thread shutdown."""

        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)


def start_config_server(
    config_path: Path,
    web_cfg: WebUiConfig,
    status_provider: Callable[[], dict[str, Any]] | None = None,
) -> ConfigWebServer:
    """Construct and start ConfigWebServer from settings."""

    server = ConfigWebServer(
        config_path=config_path,
        host=web_cfg.host,
        port=web_cfg.port,
        status_provider=status_provider,
    )
    server.start()
    return server


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run the Voice TX config web UI.")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("config.json"),
        help="Path to config JSON file.",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", default=8080, type=int, help="Bind port")
    args = parser.parse_args()

    ensure_config_file(args.config)
    web_cfg = WebUiConfig(enabled=True, host=args.host, port=args.port)
    print(
        f"Starting config web server at http://{web_cfg.host}:{web_cfg.port}/ with config file {args.config}"
    )
    server = start_config_server(args.config, web_cfg)

    try:
        while True:
            threading.Event().wait(1.0)
    except KeyboardInterrupt:
        print("Shutting down server...")
        server.stop()
