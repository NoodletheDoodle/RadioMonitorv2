#!/usr/bin/env python3
"""Small HTTP UI for viewing and editing the capture config JSON."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable


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
def _json_response(handler: BaseHTTPRequestHandler, code: int, payload: dict[str, Any]) -> None:
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

    def __init__(self, config_path: Path, host: str, port: int, status_provider: Callable[[], dict[str, Any]] | None = None) -> None:
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
        <pre id="runtime-status" style="margin:0;max-height:260px;overflow:auto;background:#fff;border:1px solid var(--border);border-radius:8px;padding:10px;font-size:0.82rem;"></pre>
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
    const statusEl = document.getElementById('status');
    const serviceStatusBadgeEl = document.getElementById('service-status-badge');
    const runtimeStatusEl = document.getElementById('runtime-status');
    const channelsEl = document.getElementById('channels');
    const channelTpl = document.getElementById('channel-template');
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

    function applyTopLevelHelp() {
      Object.entries(FIELD_HELP_BY_ID).forEach(([id, help]) => {
        const el = document.getElementById(id);
        if (el) {
          el.title = help;
        }
      });
    }

    function showStatus(msg, isError = false) {
      statusEl.textContent = msg;
      statusEl.className = isError ? 'error' : '';
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

    function setSelect(id, value) {
      document.getElementById(id).value = value ? 'true' : 'false';
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
      document.getElementById('multicast_interface').value = cfg.multicast_interface || '';
      document.getElementById('gstreamer_bin').value = cfg.gstreamer_bin || '';
      document.getElementById('recordings_base').value = cfg.recordings_base || './recordings';
      document.getElementById('logs_base').value = cfg.logs_base || './logs';
      document.getElementById('ptt_end_silence_threshold').value = cfg.ptt_end_silence_threshold || 2.0;
      document.getElementById('poll_interval').value = cfg.poll_interval || 0.5;
      setSelect('web_enabled', !!(cfg.web_ui && cfg.web_ui.enabled));
      document.getElementById('web_host').value = (cfg.web_ui && cfg.web_ui.host) || '0.0.0.0';
      document.getElementById('web_port').value = (cfg.web_ui && cfg.web_ui.port) || 12345;

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
        multicast_interface: document.getElementById('multicast_interface').value,
        gstreamer_bin: document.getElementById('gstreamer_bin').value,
        recordings_base: document.getElementById('recordings_base').value,
        logs_base: document.getElementById('logs_base').value,
        ptt_end_silence_threshold: numValue(document.getElementById('ptt_end_silence_threshold'), 2.0),
        poll_interval: numValue(document.getElementById('poll_interval'), 0.5),
        web_ui: {
          enabled: boolValue(document.getElementById('web_enabled').value),
          host: document.getElementById('web_host').value,
          port: numValue(document.getElementById('web_port'), 12345),
        },
        channels: cards.map(readChannelCard),
      };
    }

    async function loadConfig() {
      showStatus('Loading...');
      const r = await fetch('/api/config');
      const data = await r.json();
      if (!r.ok) {
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
      const r = await fetch('/api/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await r.json();
      if (!r.ok) {
        showStatus(data.error || 'Save failed', true);
        return;
      }
      loadForm(data.config);
      showStatus('Saved and applied');
      loadRuntimeStatus().catch(() => {});
    }

    async function loadRuntimeStatus() {
      const r = await fetch('/api/status');
      const data = await r.json();
      if (!r.ok) {
        runtimeStatusEl.textContent = data.error || 'Failed to load runtime status';
        setServiceBadge('status-stopped', 'Stopped');
        return;
      }
      runtimeStatusEl.textContent = JSON.stringify(data, null, 2);
      const badge = deriveServiceState(data);
      setServiceBadge(badge.state, badge.label);
    }

    document.getElementById('add-channel').addEventListener('click', () => addChannelCard({}));
    document.getElementById('reload').addEventListener('click', () => loadConfig().catch(err => showStatus(String(err), true)));
    document.getElementById('save').addEventListener('click', () => saveConfig().catch(err => showStatus(String(err), true)));
    applyTopLevelHelp();
    setServiceBadge('status-unknown', 'Checking...');
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
            _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
            return
        _json_response(handler, HTTPStatus.OK, {"config": raw, "path": str(self.config_path)})

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
                _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
                return
        _json_response(handler, HTTPStatus.OK, payload)

    def _handle_post_config(self, handler: BaseHTTPRequestHandler) -> None:
        """Validate and persist posted config payload."""

        length_raw = handler.headers.get("Content-Length", "0")
        try:
            length = int(length_raw)
        except ValueError:
            length = 0
        if length <= 0:
            _json_response(handler, HTTPStatus.BAD_REQUEST, {"error": "Empty request body."})
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
            _json_response(handler, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})
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
