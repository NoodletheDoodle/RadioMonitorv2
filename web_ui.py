#!/usr/bin/env python3
"""Small HTTP UI for viewing and editing the capture config JSON."""

from __future__ import annotations

import csv
import io
import json
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

        return """<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>Radio Monitor Config</title>
  <style>
    :root {
      --bg: #f5f8f2;
      --panel: #ffffff;
      --ink: #122025;
      --muted: #55646d;
      --accent: #116d65;
      --accent-2: #0f4f7c;
      --border: #d8e0d7;
      --warn: #9b1c1c;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: \"IBM Plex Sans\", \"Source Sans Pro\", sans-serif;
      color: var(--ink);
      background: radial-gradient(circle at 0% 0%, #e9f3ec, #f7f8f2 55%, #ecf0eb 100%);
      min-height: 100vh;
      padding: 20px;
    }
    .wrap {
      max-width: 1120px;
      margin: 0 auto;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 14px;
      overflow: hidden;
      box-shadow: 0 12px 36px rgba(0, 0, 0, 0.08);
    }
    header {
      padding: 16px 18px;
      border-bottom: 1px solid var(--border);
      background: linear-gradient(90deg, #e0ece6, #e8f0ea);
    }
    .header-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      flex-wrap: wrap;
    }
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      border-radius: 999px;
      padding: 5px 10px;
      font-size: 0.8rem;
      font-weight: 700;
      letter-spacing: 0.02em;
      border: 1px solid transparent;
      color: #243038;
      background: #edf1ee;
    }
    .status-badge::before {
      content: "";
      width: 8px;
      height: 8px;
      border-radius: 50%;
      background: currentColor;
      opacity: 0.9;
    }
    .status-healthy {
      color: #0f6844;
      background: #e5f4ec;
      border-color: #b9dec9;
    }
    .status-degraded {
      color: #8a5f00;
      background: #fff5da;
      border-color: #ecd59a;
    }
    .status-stopped {
      color: #8f1e1e;
      background: #ffe7e7;
      border-color: #e5b3b3;
    }
    .status-unknown {
      color: #33414a;
      background: #edf1f3;
      border-color: #cfd7dc;
    }
    h1 { margin: 0; font-size: 1.2rem; }
    p { margin: 6px 0 0; color: var(--muted); }
    .body { padding: 16px 18px 20px; }
    .group {
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px;
      margin-bottom: 14px;
      background: #fbfcfa;
    }
    .tab-bar {
      display: flex;
      gap: 8px;
      margin-bottom: 14px;
      flex-wrap: wrap;
    }
    .tab-btn {
      border: 1px solid #bdd0c5;
      border-radius: 999px;
      padding: 7px 14px;
      cursor: pointer;
      background: #eef4ef;
      color: #214249;
      font-weight: 700;
    }
    .tab-btn.active {
      color: #fff;
      border-color: transparent;
      background: linear-gradient(90deg, #0f6f66, #0f4f7c);
    }
    .tab-panel { display: none; }
    .tab-panel.active { display: block; }
    .group h2 { margin: 0 0 10px; font-size: 1rem; }
    .subhelp { margin: 0 0 10px; color: var(--muted); font-size: 0.85rem; }
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 10px;
    }
    label { display: block; font-size: 0.85rem; color: #35454f; margin-bottom: 4px; }
    input, select {
      width: 100%;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 8px 10px;
      background: #fff;
      color: var(--ink);
      font-family: \"IBM Plex Sans\", sans-serif;
      font-size: 0.9rem;
    }
    .sources { display: grid; gap: 10px; }
    .source {
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 10px;
      background: #ffffff;
    }
    .source-head {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 8px;
      gap: 8px;
    }
    .source-title { font-weight: 700; font-size: 0.9rem; }
    .row { margin-top: 12px; display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }
    button {
      border: 0;
      border-radius: 8px;
      padding: 9px 12px;
      cursor: pointer;
      color: #fff;
      font-weight: 600;
      background: var(--accent);
    }
    button.alt { background: var(--accent-2); }
    button.warn { background: #7a2e2e; }
    #status { color: var(--muted); font-size: 0.9rem; }
    #status.error { color: var(--warn); }
    .records-table-wrap {
      width: 100%;
      overflow: auto;
      border: 1px solid var(--border);
      border-radius: 10px;
      background: #fff;
    }
    .records-table {
      width: 100%;
      border-collapse: collapse;
      min-width: 1040px;
    }
    .records-table th,
    .records-table td {
      padding: 8px;
      border-bottom: 1px solid #edf2ee;
      text-align: left;
      font-size: 0.82rem;
      vertical-align: top;
      white-space: nowrap;
    }
    .records-table thead th {
      position: sticky;
      top: 0;
      background: #eef4ef;
      color: #23343a;
      z-index: 1;
    }
    .records-empty {
      padding: 14px;
      font-size: 0.9rem;
      color: var(--muted);
    }
    .runtime-status {
      margin: 0;
      max-height: 260px;
      overflow: auto;
      background: #fff;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 10px;
      font-size: 0.82rem;
    }
    .recordings-toolbar {
      margin-top: 0;
      margin-bottom: 10px;
    }
    .recordings-status {
      color: var(--muted);
      font-size: 0.9rem;
    }
    .recordings-controls {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      gap: 10px;
      margin-bottom: 10px;
    }
    .recordings-controls .control {
      display: flex;
      flex-direction: column;
      gap: 4px;
    }
    .recordings-controls .control label {
      margin-bottom: 0;
      font-size: 0.78rem;
      color: #4b5d66;
    }
    .recordings-status.error {
      color: var(--warn);
    }
    .link-like {
      color: #0f4f7c;
      text-decoration: none;
      font-weight: 600;
    }
    .link-like:hover {
      text-decoration: underline;
    }
    @media (max-width: 640px) {
      body { padding: 10px; }
      .body { padding: 12px; }
      .group { padding: 10px; }
      .status-badge { font-size: 0.75rem; }
    }
  </style>
</head>
<body>
  <main class=\"wrap\">
    <header>
      <div class="header-row">
        <h1>Radio Monitor Configuration</h1>
        <span id="service-status-badge" class="status-badge status-unknown">Checking...</span>
      </div>
      <p>Blank or missing config files are auto-created with defaults. Saved changes apply in about one second.</p>
    </header>
    <section class=\"body\">
      <div class=\"tab-bar\" role=\"tablist\" aria-label=\"Main sections\">
        <button id=\"tab-config\" class=\"tab-btn active\" type=\"button\" data-tab=\"config\" role=\"tab\" aria-selected=\"true\">Configuration</button>
        <button id=\"tab-recordings\" class=\"tab-btn\" type=\"button\" data-tab=\"recordings\" role=\"tab\" aria-selected=\"false\">Recordings</button>
      </div>

      <section id=\"panel-config\" class=\"tab-panel active\" role=\"tabpanel\" aria-labelledby=\"tab-config\">
      <div class=\"group\">
        <h2>General</h2>
        <p class="subhelp">Hover any input to see a quick explanation.</p>
        <div class=\"grid\">
          <div><label for=\"multicast_interface\">multicast_interface</label><input id=\"multicast_interface\" type=\"text\" /></div>
          <div><label for=\"gstreamer_bin\">gstreamer_bin</label><input id=\"gstreamer_bin\" type=\"text\" /></div>
          <div><label for=\"recordings_base\">recordings_base</label><input id=\"recordings_base\" type=\"text\" /></div>
          <div><label for=\"logs_base\">logs_base</label><input id=\"logs_base\" type=\"text\" /></div>
          <div><label for=\"ptt_end_silence_threshold\">ptt_end_silence_threshold</label><input id=\"ptt_end_silence_threshold\" type=\"number\" min=\"0\" step=\"0.1\" /></div>
          <div><label for=\"poll_interval\">poll_interval</label><input id=\"poll_interval\" type=\"number\" min=\"0.1\" step=\"0.1\" /></div>
        </div>
      </div>

      <div class="group">
        <h2>Runtime Status</h2>
        <p class="subhelp">Read-only live status from the running monitor process.</p>
        <pre id="runtime-status" class="runtime-status"></pre>
      </div>

      <div class=\"group\">
        <h2>Web UI</h2>
        <div class=\"grid\">
          <div>
            <label for=\"web_enabled\">enabled</label>
            <select id=\"web_enabled\"><option value=\"true\">true</option><option value=\"false\">false</option></select>
          </div>
          <div><label for=\"web_host\">host</label><input id=\"web_host\" type=\"text\" /></div>
          <div><label for=\"web_port\">port</label><input id=\"web_port\" type=\"number\" min=\"1\" max=\"65535\" /></div>
        </div>
      </div>

      <div class=\"group\">
        <h2>Channels</h2>
        <div id=\"channels\" class=\"sources\"></div>
        <div class=\"row\">
          <button id=\"add-channel\" class=\"alt\" type=\"button\">Add Channel</button>
        </div>
      </div>

      <div class=\"row\">
        <button id=\"reload\" class=\"alt\" type=\"button\">Reload</button>
        <button id=\"save\" type=\"button\">Save</button>
        <span id=\"status\"></span>
      </div>
      </section>

      <section id=\"panel-recordings\" class=\"tab-panel\" role=\"tabpanel\" aria-labelledby=\"tab-recordings\">
        <div class=\"group\">
          <h2>Recorded Calls</h2>
          <p class=\"subhelp\">Flat view of all call rows found in CSV logs, with downloads for both WAV recordings and log files.</p>
          <div class=\"row recordings-toolbar\">
            <button id=\"refresh-recordings\" class=\"alt\" type=\"button\">Refresh List</button>
            <button id=\"download-filtered-audio\" class=\"alt\" type=\"button\">Download All Audio</button>
            <button id=\"download-filtered-csv\" class=\"alt\" type=\"button\">Download All CSVs</button>
            <span id=\"recordings-status\" class=\"recordings-status\"></span>
          </div>
          <div class=\"recordings-controls\">
            <div class=\"control\">
              <label for=\"rec-filter-channel\">channel</label>
              <select id=\"rec-filter-channel\">
                <option value=\"\">all</option>
              </select>
            </div>
            <div class=\"control\">
              <label for=\"rec-filter-sender\">sender contains</label>
              <input id=\"rec-filter-sender\" type=\"text\" placeholder=\"e.g. 10.3.1.\" />
            </div>
            <div class=\"control\">
              <label for=\"rec-filter-search\">search filename</label>
              <input id=\"rec-filter-search\" type=\"text\" placeholder=\"part of WAV/CSV name\" />
            </div>
            <div class=\"control\">
              <label for=\"rec-sort-by\">sort by</label>
              <select id=\"rec-sort-by\">
                <option value=\"local_start_time\">local_start_time</option>
                <option value=\"unique_id\">unique_id</option>
                <option value=\"channel_name\">channel_name</option>
                <option value=\"sender_ip\">sender_ip</option>
                <option value=\"duration_seconds\">duration_seconds</option>
                <option value=\"wav_filename\">wav_filename</option>
              </select>
            </div>
            <div class=\"control\">
              <label for=\"rec-sort-dir\">sort direction</label>
              <select id=\"rec-sort-dir\">
                <option value=\"desc\">desc</option>
                <option value=\"asc\">asc</option>
              </select>
            </div>
          </div>
          <div id=\"recordings-container\" class=\"records-table-wrap\">
            <div class=\"records-empty\">Loading recordings...</div>
          </div>
        </div>
      </section>
    </section>
  </main>

  <template id=\"channel-template\">
    <article class=\"source\">
      <div class=\"source-head\">
        <div class=\"source-title\">Channel</div>
        <button class=\"warn remove-channel\" type=\"button\">Remove</button>
      </div>
      <div class=\"grid\">
        <div><label>name</label><input data-key=\"name\" type=\"text\" /></div>
        <div><label>ip</label><input data-key=\"ip\" type=\"text\" /></div>
        <div><label>port</label><input data-key=\"port\" type=\"number\" min=\"1\" max=\"65535\" /></div>
      </div>
    </article>
  </template>

  <script>
    const el = id => document.getElementById(id);

    const statusEl = el('status');
    const serviceStatusBadgeEl = el('service-status-badge');
    const runtimeStatusEl = el('runtime-status');
    const recordingsContainerEl = el('recordings-container');
    const recordingsStatusEl = el('recordings-status');
    const recFilterChannelEl = el('rec-filter-channel');
    const recFilterSenderEl = el('rec-filter-sender');
    const recFilterSearchEl = el('rec-filter-search');
    const recSortByEl = el('rec-sort-by');
    const recSortDirEl = el('rec-sort-dir');
    const downloadFilteredAudioEl = el('download-filtered-audio');
    const downloadFilteredCsvEl = el('download-filtered-csv');
    const formEls = {
      multicastInterface: el('multicast_interface'),
      gstreamerBin: el('gstreamer_bin'),
      recordingsBase: el('recordings_base'),
      logsBase: el('logs_base'),
      silenceThreshold: el('ptt_end_silence_threshold'),
      pollInterval: el('poll_interval'),
      webEnabled: el('web_enabled'),
      webHost: el('web_host'),
      webPort: el('web_port'),
      addChannel: el('add-channel'),
      reload: el('reload'),
      save: el('save'),
      refreshRecordings: el('refresh-recordings'),
    };

    const tabButtons = Array.from(document.querySelectorAll('.tab-btn'));
    const tabPanels = {
      config: el('panel-config'),
      recordings: el('panel-recordings'),
    };
    const channelsEl = el('channels');
    const channelTpl = el('channel-template');
    const FIELD_HELP_BY_ID = {
      multicast_interface: 'Local interface/IP used for multicast join.',
      gstreamer_bin: 'Path to gst-launch-1.0 executable or command name on PATH.',
      recordings_base: 'Base directory where WAV recordings are saved.',
      logs_base: 'Base directory where per-channel CSV files are saved.',
      ptt_end_silence_threshold: 'Seconds with no WAV growth before call is closed.',
      poll_interval: 'How often to check staging WAV file size.',
      web_enabled: 'Enable or disable the web configuration UI.',
      web_host: 'Bind address for web UI (0.0.0.0 listens on all interfaces).',
      web_port: 'TCP port for the web UI service.',
    };
    const CHANNEL_FIELD_HELP = {
      name: 'Unique channel label used in folder/file names.',
      ip: 'Multicast group address for this channel.',
      port: 'UDP port for this channel.',
    };
    const AUDIO_BUTTON_BASE = 'Download All Audio';
    const CSV_BUTTON_BASE = 'Download All CSVs';
    const RECORDING_VALUE_COLUMNS = [
      'unique_id',
      'channel_name',
      'sender_ip',
      'relative_start',
      'relative_end',
      'local_start_time',
      'local_end_time',
      'duration_seconds',
      'wav_filename',
    ];
    const RECORDING_TABLE_HEADERS = [
      ...RECORDING_VALUE_COLUMNS,
      'recording',
      'log_csv',
    ];

    let recordingsRows = [];
    let filteredRecordingsRows = [];

    function applyTopLevelHelp() {
      Object.entries(FIELD_HELP_BY_ID).forEach(([id, help]) => {
        const el = document.getElementById(id);
        if (el) {
          el.title = help;
        }
      });
    }

    function updateBatchDownloadButtonLabels() {
      const audioCount = filteredRecordingsRows.filter(row => !!row.recording_exists).length;
      const csvCount = filteredRecordingsRows.filter(row => !!row.log_csv_path).length;

      downloadFilteredAudioEl.textContent = `${AUDIO_BUTTON_BASE} (${audioCount})`;
      downloadFilteredCsvEl.textContent = `${CSV_BUTTON_BASE} (${csvCount})`;
      downloadFilteredAudioEl.disabled = audioCount === 0;
      downloadFilteredCsvEl.disabled = csvCount === 0;
    }

    function showStatus(msg, isError = false) {
      statusEl.textContent = msg;
      statusEl.className = isError ? 'error' : '';
    }

    function showRecordingsStatus(msg, isError = false) {
      recordingsStatusEl.textContent = msg;
      recordingsStatusEl.classList.toggle('error', isError);
    }

    function switchTab(tabName) {
      tabButtons.forEach(btn => {
        const active = btn.dataset.tab === tabName;
        btn.classList.toggle('active', active);
        btn.setAttribute('aria-selected', active ? 'true' : 'false');
      });
      Object.entries(tabPanels).forEach(([name, panel]) => {
        panel.classList.toggle('active', name === tabName);
      });

      if (tabName === 'recordings') {
        loadRecordings().catch(err => showRecordingsStatus(String(err), true));
      }
    }

    function setServiceBadge(state, label) {
      const classes = ['status-healthy', 'status-degraded', 'status-stopped', 'status-unknown'];
      serviceStatusBadgeEl.classList.remove(...classes);
      serviceStatusBadgeEl.classList.add(state);
      serviceStatusBadgeEl.textContent = label;
    }

    function deriveServiceState(statusData) {
      const runtime = statusData && statusData.runtime;
      if (!runtime || typeof runtime !== 'object') {
        return { state: 'status-degraded', label: 'Degraded' };
      }
      if (runtime.shutdown_requested) {
        return { state: 'status-stopped', label: 'Stopped' };
      }

      const threads = runtime.monitor_threads || {};
      const total = Number(threads.total || 0);
      const alive = Number(threads.alive || 0);
      const config = runtime.config || {};

      if (config.last_reload_error) {
        return { state: 'status-degraded', label: 'Degraded' };
      }
      if (total <= 0 || alive <= 0) {
        return { state: 'status-stopped', label: 'Stopped' };
      }
      if (alive < total) {
        return { state: 'status-degraded', label: 'Degraded' };
      }
      return { state: 'status-healthy', label: 'Healthy' };
    }

    function boolValue(v) {
      return String(v) === 'true';
    }

    function numValue(input, fallback = 0) {
      const n = Number(input.value);
      return Number.isFinite(n) ? n : fallback;
    }

    function setSelect(selectEl, value) {
      selectEl.value = value ? 'true' : 'false';
    }

    function addChannelCard(channel = {}) {
      const frag = channelTpl.content.cloneNode(true);
      const card = frag.querySelector('.source');
      const fields = card.querySelectorAll('[data-key]');
      fields.forEach(el => {
        const key = el.dataset.key;
        const val = channel[key];
        el.title = CHANNEL_FIELD_HELP[key] || '';
        el.value = val !== undefined ? String(val) : '';
      });
      card.querySelector('.remove-channel').addEventListener('click', () => {
        card.remove();
        renumberChannels();
      });
      channelsEl.appendChild(card);
      renumberChannels();
    }

    function renumberChannels() {
      const cards = channelsEl.querySelectorAll('.source');
      cards.forEach((card, idx) => {
        card.querySelector('.source-title').textContent = `Channel ${idx + 1}`;
      });
    }

    function loadForm(cfg) {
      formEls.multicastInterface.value = cfg.multicast_interface || '';
      formEls.gstreamerBin.value = cfg.gstreamer_bin || '';
      formEls.recordingsBase.value = cfg.recordings_base || './recordings';
      formEls.logsBase.value = cfg.logs_base || './logs';
      formEls.silenceThreshold.value = cfg.ptt_end_silence_threshold || 2.0;
      formEls.pollInterval.value = cfg.poll_interval || 0.5;
      setSelect(formEls.webEnabled, !!(cfg.web_ui && cfg.web_ui.enabled));
      formEls.webHost.value = (cfg.web_ui && cfg.web_ui.host) || '0.0.0.0';
      formEls.webPort.value = (cfg.web_ui && cfg.web_ui.port) || 12345;

      channelsEl.innerHTML = '';
      const channels = Array.isArray(cfg.channels) && cfg.channels.length ? cfg.channels : [{}];
      channels.forEach(c => addChannelCard(c));
    }

    function readChannelCard(card) {
      const get = key => card.querySelector(`[data-key="${key}"]`);
      return {
        name: get('name').value,
        ip: get('ip').value,
        port: numValue(get('port'), 60322),
      };
    }

    function readForm() {
      const cards = Array.from(channelsEl.querySelectorAll('.source'));
      return {
        multicast_interface: formEls.multicastInterface.value,
        gstreamer_bin: formEls.gstreamerBin.value,
        recordings_base: formEls.recordingsBase.value,
        logs_base: formEls.logsBase.value,
        ptt_end_silence_threshold: numValue(formEls.silenceThreshold, 2.0),
        poll_interval: numValue(formEls.pollInterval, 0.5),
        web_ui: {
          enabled: boolValue(formEls.webEnabled.value),
          host: formEls.webHost.value,
          port: numValue(formEls.webPort, 12345),
        },
        channels: cards.map(readChannelCard),
      };
    }

    function cellText(value) {
      if (value === null || value === undefined || value === '') {
        return '-';
      }
      return String(value);
    }

    function buildDownloadCell(url, label) {
      const td = document.createElement('td');
      if (!url) {
        td.textContent = '-';
        return td;
      }
      const a = document.createElement('a');
      a.href = url;
      a.textContent = label;
      a.className = 'link-like';
      td.appendChild(a);
      return td;
    }

    function normalizeText(value) {
      return String(value || '').toLowerCase();
    }

    function populateChannelFilter(rows) {
      const previous = recFilterChannelEl.value;
      const channels = Array.from(
        new Set(
          (rows || [])
            .map(r => String(r.channel_name || '').trim())
            .filter(Boolean)
        )
      ).sort((a, b) => a.localeCompare(b));

      recFilterChannelEl.innerHTML = '<option value="">all</option>';
      channels.forEach(channel => {
        const opt = document.createElement('option');
        opt.value = channel;
        opt.textContent = channel;
        recFilterChannelEl.appendChild(opt);
      });

      if (channels.includes(previous)) {
        recFilterChannelEl.value = previous;
      }
    }

    function sortRows(rows, sortBy, sortDir) {
      const sorted = [...rows];
      const direction = sortDir === 'asc' ? 1 : -1;
      sorted.sort((a, b) => {
        const av = a ? a[sortBy] : '';
        const bv = b ? b[sortBy] : '';

        if (sortBy === 'unique_id' || sortBy === 'duration_seconds') {
          const an = Number(av);
          const bn = Number(bv);
          const aNum = Number.isFinite(an) ? an : Number.NEGATIVE_INFINITY;
          const bNum = Number.isFinite(bn) ? bn : Number.NEGATIVE_INFINITY;
          if (aNum === bNum) {
            return 0;
          }
          return aNum > bNum ? direction : -direction;
        }

        const as = normalizeText(av);
        const bs = normalizeText(bv);
        const cmp = as.localeCompare(bs, undefined, { numeric: true, sensitivity: 'base' });
        if (cmp === 0) {
          return 0;
        }
        return cmp > 0 ? direction : -direction;
      });
      return sorted;
    }

    function applyRecordingsView() {
      const channelFilter = String(recFilterChannelEl.value || '');
      const senderFilter = normalizeText(recFilterSenderEl.value).trim();
      const searchFilter = normalizeText(recFilterSearchEl.value).trim();
      const sortBy = String(recSortByEl.value || 'local_start_time');
      const sortDir = String(recSortDirEl.value || 'desc');

      let rows = [...recordingsRows];
      if (channelFilter) {
        rows = rows.filter(row => String(row.channel_name || '') === channelFilter);
      }
      if (senderFilter) {
        rows = rows.filter(row => normalizeText(row.sender_ip).includes(senderFilter));
      }
      if (searchFilter) {
        rows = rows.filter(
          row => normalizeText(row.wav_filename).includes(searchFilter)
            || normalizeText(row.log_csv_path).includes(searchFilter)
        );
      }

      const sortedRows = sortRows(rows, sortBy, sortDir);
      filteredRecordingsRows = sortedRows;
      renderRecordingsTable(sortedRows);
      updateBatchDownloadButtonLabels();
      showRecordingsStatus(
        `Showing ${sortedRows.length} of ${recordingsRows.length} row${recordingsRows.length === 1 ? '' : 's'}`
      );
    }

    function extractFilenameFromDisposition(contentDisposition) {
      if (!contentDisposition) {
        return '';
      }
      const match = contentDisposition.match(/filename="?([^";]+)"?/i);
      return match ? match[1] : '';
    }

    async function fetchJson(url, options = undefined) {
      const response = await fetch(url, options);
      let data = {};
      try {
        data = await response.json();
      } catch (_err) {
        data = {};
      }
      return { response, data };
    }

    async function downloadFiltered(kind) {
      const isRecording = kind === 'recording';
      const paths = filteredRecordingsRows
        .filter(row => (isRecording ? !!row.recording_exists : true))
        .map(row => (isRecording ? row.recording_path : row.log_csv_path))
        .filter(Boolean);

      if (!paths.length) {
        showRecordingsStatus(
          isRecording ? 'No audio files in current filtered results.' : 'No CSV files in current filtered results.',
          true
        );
        return;
      }

      showRecordingsStatus(
        isRecording ? 'Preparing audio ZIP...' : 'Preparing CSV ZIP...'
      );

      const response = await fetch('/api/download-batch', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ kind, paths }),
      });

      if (!response.ok) {
        let errorMessage = 'Batch download failed.';
        try {
          const payload = await response.json();
          errorMessage = payload.error || errorMessage;
        } catch (_err) {
          // Keep fallback message when non-JSON error is returned.
        }
        showRecordingsStatus(errorMessage, true);
        return;
      }

      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition');
      const filename = extractFilenameFromDisposition(disposition)
        || (isRecording ? 'audio_filtered.zip' : 'csv_filtered.zip');

      const downloadUrl = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = downloadUrl;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.URL.revokeObjectURL(downloadUrl);

      showRecordingsStatus(
        isRecording ? `Downloaded ${paths.length} audio file(s).` : `Downloaded ${paths.length} CSV file(s).`
      );
    }

    function renderRecordingsTable(rows) {
      if (!Array.isArray(rows) || rows.length === 0) {
        recordingsContainerEl.innerHTML = '<div class="records-empty">No recordings found yet.</div>';
        return;
      }

      const wrap = document.createElement('div');
      wrap.className = 'records-table-wrap';

      const table = document.createElement('table');
      table.className = 'records-table';

      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      RECORDING_TABLE_HEADERS.forEach(h => {
        const th = document.createElement('th');
        th.textContent = h;
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);

      const tbody = document.createElement('tbody');
      rows.forEach(row => {
        const tr = document.createElement('tr');
        RECORDING_VALUE_COLUMNS.forEach(key => {
          const td = document.createElement('td');
          td.textContent = cellText(row[key]);
          tr.appendChild(td);
        });

        tr.appendChild(buildDownloadCell(row.recording_download_url, 'Download WAV'));
        tr.appendChild(buildDownloadCell(row.log_csv_download_url, 'Download CSV'));
        tbody.appendChild(tr);
      });

      table.appendChild(thead);
      table.appendChild(tbody);
      wrap.appendChild(table);

      recordingsContainerEl.innerHTML = '';
      recordingsContainerEl.appendChild(wrap);
    }

    async function loadRecordings() {
      showRecordingsStatus('Loading recordings...');
      const { response, data } = await fetchJson('/api/recordings');
      if (!response.ok) {
        recordingsRows = [];
        filteredRecordingsRows = [];
        updateBatchDownloadButtonLabels();
        showRecordingsStatus(data.error || 'Failed to load recordings', true);
        recordingsContainerEl.innerHTML = '<div class="records-empty">Failed to load recordings.</div>';
        return;
      }

      recordingsRows = Array.isArray(data.rows) ? data.rows : [];
      populateChannelFilter(recordingsRows);
      applyRecordingsView();
    }

    async function loadConfig() {
      showStatus('Loading...');
      const { response, data } = await fetchJson('/api/config');
      if (!response.ok) {
        showStatus(data.error || 'Failed to load config', true);
        return;
      }
      loadForm(data.config);
      showStatus('Loaded');
      loadRuntimeStatus().catch(() => {});
    }

    async function saveConfig() {
      const payload = readForm();
      showStatus('Saving...');
      const { response, data } = await fetchJson('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!response.ok) {
        showStatus(data.error || 'Save failed', true);
        return;
      }
      loadForm(data.config);
      showStatus('Saved and applied');
      loadRuntimeStatus().catch(() => {});
    }

    async function loadRuntimeStatus() {
      const { response, data } = await fetchJson('/api/status');
      if (!response.ok) {
        runtimeStatusEl.textContent = data.error || 'Failed to load runtime status';
        setServiceBadge('status-stopped', 'Stopped');
        return;
      }
      runtimeStatusEl.textContent = JSON.stringify(data, null, 2);
      const badge = deriveServiceState(data);
      setServiceBadge(badge.state, badge.label);
    }

    formEls.addChannel.addEventListener('click', () => addChannelCard({}));
    formEls.reload.addEventListener('click', () => loadConfig().catch(err => showStatus(String(err), true)));
    formEls.save.addEventListener('click', () => saveConfig().catch(err => showStatus(String(err), true)));
    formEls.refreshRecordings.addEventListener('click', () => loadRecordings().catch(err => showRecordingsStatus(String(err), true)));
    downloadFilteredAudioEl.addEventListener('click', () => downloadFiltered('recording').catch(err => showRecordingsStatus(String(err), true)));
    downloadFilteredCsvEl.addEventListener('click', () => downloadFiltered('log').catch(err => showRecordingsStatus(String(err), true)));
    [recFilterChannelEl, recFilterSenderEl, recFilterSearchEl, recSortByEl, recSortDirEl].forEach(el => {
      el.addEventListener('input', applyRecordingsView);
      el.addEventListener('change', applyRecordingsView);
    });
    tabButtons.forEach(btn => {
      btn.addEventListener('click', () => switchTab(btn.dataset.tab));
    });
    applyTopLevelHelp();
    updateBatchDownloadButtonLabels();
    setServiceBadge('status-unknown', 'Checking...');
    switchTab('config');
    loadConfig().catch(err => showStatus(String(err), true));
    loadRuntimeStatus().catch(() => {});
    setInterval(() => loadRuntimeStatus().catch(() => {}), 3000);
  </script>
</body>
</html>
"""

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
            with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
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
