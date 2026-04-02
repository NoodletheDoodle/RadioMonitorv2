# Project Documentation

## What The Project Does

This project monitors one or more multicast Opus/RTP radio channels and writes:

- one WAV file per detected call
- one CSV row per call containing metadata
- optional web UI/API for config editing, status, recordings, and downloads

## Main Files

- `radio_monitor.py`: main capture service, hot reload logic, and embedded web server startup
- `web_ui.py`: web UI and HTTP API implementation
- `config.json`: runtime configuration (channels, paths, thresholds, web settings)
- `transcribe.service`: generated/maintained systemd service unit

## Runtime Data Layout

- `recordings/<YYYY-MM-DD>/<channel_name>/<channel_name>_<timestamp>.wav`
- `logs/<YYYY-MM-DD>/<channel_name>_<run_timestamp>.csv`

## Configuration Overview

Important config fields:

- `channels`: list of `{name, ip, port}` entries
- `multicast_interface`: interface/IP used for multicast-related operations
- `gstreamer_bin`: GStreamer launcher executable (`gst-launch-1.0` or full path)
- `recordings_base`: root folder for WAV outputs
- `logs_base`: root folder for CSV logs
- `ptt_end_silence_threshold`: silence in seconds required to end an active call
- `poll_interval`: frequency of staging WAV file size checks
- `web_ui.enabled`, `web_ui.host`, `web_ui.port`: web server configuration

## Running The Service

Typical local run:

```bash
python radio_monitor.py --config config.json
```

Initialize default config:

```bash
python radio_monitor.py --init-config --config config.json
```

Generate systemd service file:

```bash
python radio_monitor.py --generate-service
```

Run web UI standalone (without capture engine):

```bash
python web_ui.py --config config.json --host 127.0.0.1 --port 8080
```

---

## Program Structure And Control Flow (radio_monitor.py)

### High-level class responsibilities

- `ConfigManager`
  - loads/validates config
  - computes defaults
  - resolves GStreamer binary
  - provides thread-safe snapshots
- `CsvManager`
  - builds per-channel/day CSV paths
  - writes call rows and headers
- `StreamManager`
  - creates GStreamer command/process
  - monitors staging WAV growth and sender IPs
  - finalizes/discards calls
- `RadioMonitorApp`
  - orchestrates startup/retry/shutdown
  - manages monitor threads
  - hosts config hot-reload loop
  - starts/stops embedded web server

### Startup flow

1. `main()` parses CLI args.
2. `RadioMonitorApp` is created with configured path.
3. If `--init-config`: write starter config and exit.
4. If `--generate-service`: write systemd unit and exit.
5. Otherwise, `RadioMonitorApp.run()` starts service loop.

### `RadioMonitorApp.run()` control flow

1. Register SIGTERM/SIGINT handlers.
2. Retry startup until config + GStreamer checks succeed (or shutdown requested).
3. Print startup summary.
4. Start one monitor thread per configured channel.
5. Start embedded web server (if enabled).
6. Enter loop:
   - periodically check config file mtime
   - apply validated hot reload on changes
   - restart channel threads if channel list changed
   - rebind web server if web config changed
   - restart monitor threads if any died unexpectedly
7. On shutdown: stop web server, stop channel threads, exit cleanly.

### Per-channel capture thread flow (`StreamManager.monitor_channel`)

1. Ensure sender capture socket is available (with retry/backoff).
2. Prepare fresh staging WAV path for this channel/date.
3. Launch GStreamer pipeline writing to staging WAV.
4. Monitor loop:
   - poll sender socket for latest sender IP
   - poll staging WAV size growth
   - detect first audio timestamp
   - stop call when silence exceeds threshold
5. Stop GStreamer process.
6. If call had valid audio:
   - rename staging WAV to timestamped final WAV
   - append metadata row to CSV
7. Repeat until reload/shutdown requested.

### How call timing is derived

- `call_start_dt`: anchored to first observed WAV growth
- `call_end_dt`: captured when call is finalized
- `duration_seconds`: `(call_end_dt - call_start_dt).total_seconds()`
- `relative_start`/`relative_end`: elapsed clock strings from script start time

### How sender IP capture works

1. Build UDP socket bound to channel port.
2. Join channel multicast group using configured interface/IP with Linux fallback.
3. Set non-blocking mode.
4. During active call loop, drain pending packets and keep latest `sender_ip` for CSV row.

---

## Operational Notes

- Hot reload is config-file based and non-destructive for ongoing process state where possible.
- Invalid config updates are rejected and reported in runtime status (`last_reload_error`).
- Sender IP capture can be disabled per channel if multicast/socket join fails; service continues recording audio.
- Web UI startup/bind failures are non-fatal; capture service continues running.

# Radio Monitor API Reference

This project exposes HTTP endpoints from `web_ui.py`, and `radio_monitor.py` embeds that same server when web UI is enabled.

## Server Context

- Standalone mode: run `web_ui.py` directly.
- Embedded mode: run `radio_monitor.py` and let it start the web server with runtime status integration.
- Host and port come from config (`web_ui.host`, `web_ui.port`).

Example base URL:

- `http://127.0.0.1:12345`

---

## Endpoint Summary

| Method | Path | Description |
|---|---|---|
| GET | `/` | Returns the HTML web interface. |
| GET | `/health` | Lightweight health check. |
| GET | `/api/config` | Reads normalized current config. |
| POST | `/api/config` | Validates and saves posted config JSON. |
| GET | `/api/status` | Returns web UI status and, when embedded, radio monitor runtime status. |
| GET | `/api/recordings` | Returns flattened recording/log rows derived from CSV logs. |
| GET | `/api/download?kind=...&path=...` | Downloads one recording WAV or one log CSV file. |
| POST | `/api/download-batch` | Downloads multiple filtered files as a ZIP archive. |

---

## 1) GET /

Returns the single-page HTML UI.

### Python requests

```python
import requests

base_url = "http://127.0.0.1:12345"
r = requests.get(f"{base_url}/", timeout=10)
r.raise_for_status()
print(r.text[:200])
```

### JavaScript fetch

```javascript
const baseUrl = "http://127.0.0.1:12345";

const response = await fetch(`${baseUrl}/`);
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const html = await response.text();
console.log(html.slice(0, 200));
```

---

## 2) GET /health

Simple health endpoint.

Typical response:

```json
{
  "status": "ok"
}
```

### Python requests

```python
import requests

base_url = "http://127.0.0.1:12345"
r = requests.get(f"{base_url}/health", timeout=10)
r.raise_for_status()
print(r.json())
```

### JavaScript fetch

```javascript
const baseUrl = "http://127.0.0.1:12345";

const response = await fetch(`${baseUrl}/health`);
if (!response.ok) throw new Error(`HTTP ${response.status}`);
console.log(await response.json());
```

---

## 3) GET /api/config

Returns normalized config payload and config file path.

Typical response shape:

```json
{
  "config": {
    "multicast_interface": "10.3.1.253",
    "gstreamer_bin": "gst-launch-1.0",
    "recordings_base": "./recordings",
    "logs_base": "./logs",
    "ptt_end_silence_threshold": 2.0,
    "poll_interval": 0.5,
    "web_ui": {
      "enabled": true,
      "host": "0.0.0.0",
      "port": 12345
    },
    "channels": [
      {
        "name": "park_ranger_1",
        "ip": "239.192.49.1",
        "port": 60322
      }
    ]
  },
  "path": "config.json"
}
```

### Python requests

```python
import requests

base_url = "http://127.0.0.1:12345"
r = requests.get(f"{base_url}/api/config", timeout=10)
r.raise_for_status()
payload = r.json()
print(payload["path"])
print(payload["config"]["channels"])
```

### JavaScript fetch

```javascript
const baseUrl = "http://127.0.0.1:12345";

const response = await fetch(`${baseUrl}/api/config`);
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const payload = await response.json();
console.log(payload.path, payload.config.channels);
```

---

## 4) POST /api/config

Validates and writes config JSON.

- Request body must be a JSON object.
- Server normalizes config and validates required channel fields.

### Python requests

```python
import requests

base_url = "http://127.0.0.1:12345"

new_config = {
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
    "channels": [
        {"name": "park_ranger_1", "ip": "239.192.49.1", "port": 60322}
    ],
}

r = requests.post(f"{base_url}/api/config", json=new_config, timeout=10)
r.raise_for_status()
print(r.json())
```

### JavaScript fetch

```javascript
const baseUrl = "http://127.0.0.1:12345";

const newConfig = {
  multicast_interface: "10.3.1.253",
  gstreamer_bin: "gst-launch-1.0",
  recordings_base: "./recordings",
  logs_base: "./logs",
  ptt_end_silence_threshold: 2.0,
  poll_interval: 0.5,
  web_ui: { enabled: true, host: "0.0.0.0", port: 12345 },
  channels: [{ name: "park_ranger_1", ip: "239.192.49.1", port: 60322 }]
};

const response = await fetch(`${baseUrl}/api/config`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(newConfig)
});
if (!response.ok) throw new Error(`HTTP ${response.status}`);
console.log(await response.json());
```

---

## 5) GET /api/status

Returns web UI service status and optional radio monitor runtime state.

In embedded mode (`radio_monitor.py`), `runtime` includes:

- script start time and uptime
- configured channels
- monitor thread counts
- active runtime settings
- config reload status

### Python requests

```python
import requests

base_url = "http://127.0.0.1:12345"
r = requests.get(f"{base_url}/api/status", timeout=10)
r.raise_for_status()
payload = r.json()
print(payload.get("service"))
print(payload.get("runtime", {}).get("uptime_seconds"))
```

### JavaScript fetch

```javascript
const baseUrl = "http://127.0.0.1:12345";

const response = await fetch(`${baseUrl}/api/status`);
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const payload = await response.json();
console.log(payload.service, payload.runtime?.uptime_seconds);
```

---

## 6) GET /api/recordings

Builds a flat recording list from per-channel CSV files under `logs_base`.

Each row includes metadata and download URLs:

- `unique_id`, `channel_name`, `sender_ip`
- `relative_start`, `relative_end`
- `local_start_time`, `local_end_time`
- `duration_seconds`, `wav_filename`
- `recording_path`, `recording_exists`, `recording_download_url`
- `log_csv_path`, `log_csv_download_url`

### Python requests

```python
import requests

base_url = "http://127.0.0.1:12345"
r = requests.get(f"{base_url}/api/recordings", timeout=30)
r.raise_for_status()
payload = r.json()
print("count:", payload["count"])
if payload["rows"]:
    print(payload["rows"][0])
```

### JavaScript fetch

```javascript
const baseUrl = "http://127.0.0.1:12345";

const response = await fetch(`${baseUrl}/api/recordings`);
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const payload = await response.json();
console.log("count:", payload.count);
console.log(payload.rows?.[0]);
```

---

## 7) GET /api/download

Downloads one file by relative path.

Query parameters:

- `kind`: `recording` or `log`
- `path`: relative path under recordings or logs root

Example:

- `/api/download?kind=recording&path=2026-03-31/park_ranger_1/park_ranger_1_20260331_160855.wav`
- `/api/download?kind=log&path=2026-03-31/park_ranger_1_20260331_160855.csv`

### Python requests

```python
import requests

base_url = "http://127.0.0.1:12345"
params = {
    "kind": "recording",
    "path": "2026-03-31/park_ranger_1/park_ranger_1_20260331_160855.wav",
}

r = requests.get(f"{base_url}/api/download", params=params, timeout=60)
r.raise_for_status()
with open("downloaded_call.wav", "wb") as f:
    f.write(r.content)
print("saved downloaded_call.wav")
```

### JavaScript fetch

```javascript
const baseUrl = "http://127.0.0.1:12345";
const params = new URLSearchParams({
  kind: "recording",
  path: "2026-03-31/park_ranger_1/park_ranger_1_20260331_160855.wav"
});

const response = await fetch(`${baseUrl}/api/download?${params.toString()}`);
if (!response.ok) throw new Error(`HTTP ${response.status}`);
const blob = await response.blob();
const url = URL.createObjectURL(blob);
const a = document.createElement("a");
a.href = url;
a.download = "downloaded_call.wav";
document.body.appendChild(a);
a.click();
a.remove();
URL.revokeObjectURL(url);
```

---

## 8) POST /api/download-batch

Creates and downloads a ZIP containing multiple filtered files.

Request JSON:

```json
{
  "kind": "recording",
  "paths": [
    "2026-03-31/park_ranger_1/park_ranger_1_20260331_160855.wav",
    "2026-03-31/park_ranger_1/park_ranger_1_160900.wav"
  ]
}
```

- `kind`: `recording` or `log`
- `paths`: list of relative file paths

### Python requests

```python
import requests

base_url = "http://127.0.0.1:12345"
payload = {
    "kind": "log",
    "paths": [
        "2026-03-31/park_ranger_1_20260331_160855.csv",
        "2026-03-31/park_ranger_2_20260331_160855.csv",
    ],
}

r = requests.post(f"{base_url}/api/download-batch", json=payload, timeout=120)
r.raise_for_status()
with open("filtered_logs.zip", "wb") as f:
    f.write(r.content)
print("saved filtered_logs.zip")
```

### JavaScript fetch

```javascript
const baseUrl = "http://127.0.0.1:12345";
const payload = {
  kind: "recording",
  paths: [
    "2026-03-31/park_ranger_1/park_ranger_1_20260331_160855.wav"
  ]
};

const response = await fetch(`${baseUrl}/api/download-batch`, {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(payload)
});
if (!response.ok) throw new Error(`HTTP ${response.status}`);

const blob = await response.blob();
const url = URL.createObjectURL(blob);
const a = document.createElement("a");
a.href = url;
a.download = "filtered_audio.zip";
document.body.appendChild(a);
a.click();
a.remove();
URL.revokeObjectURL(url);
```

---

## Error Response Notes

Most API failures return JSON of the form:

```json
{
  "error": "...message..."
}
```

Common HTTP status codes:

- 200 OK: success
- 400 Bad Request: invalid input
- 404 Not Found: missing endpoint/file
- 500 Internal Server Error: runtime failure

---

## Runtime Status Field Reference (/api/status)

When running embedded via `radio_monitor.py`, `/api/status` includes a `runtime` object generated by `RadioMonitorApp.status_provider()`.

### Top-level response fields

| Field | Type | Description |
|---|---|---|
| `status` | string | API request status (`ok` on success). |
| `service` | string | Always `config-web-ui`. |
| `config_path` | string | Path of config file used by web server. |
| `runtime` | object | Present when status provider is attached by `radio_monitor.py`. |

### `runtime` fields

| Field | Type | Description |
|---|---|---|
| `service` | string | Runtime service identifier (`radio-monitor`). |
| `script_start_time` | string | ISO timestamp for process start time. |
| `now` | string | ISO timestamp for current server time. |
| `uptime_seconds` | number | Service uptime in seconds. |
| `shutdown_requested` | boolean | True if shutdown event has been triggered. |
| `channels_configured` | array<object> | Current active channel configs. |
| `monitor_threads` | object | Thread health summary. |
| `runtime_settings` | object | Effective runtime settings snapshot. |
| `config` | object | Config path and hot-reload status fields. |

### `runtime.monitor_threads` fields

| Field | Type | Description |
|---|---|---|
| `total` | integer | Number of monitor threads expected for configured channels. |
| `alive` | integer | Number of monitor threads currently alive. |
| `names` | array<string> | Thread names (`monitor-<channel>`). |

### `runtime.runtime_settings` fields

| Field | Type | Description |
|---|---|---|
| `multicast_interface` | string | Current multicast interface value from runtime config. |
| `gstreamer_bin` | string | Resolved GStreamer executable path/name. |
| `recordings_base` | string | Base recordings directory. |
| `logs_base` | string | Base logs directory. |
| `ptt_end_silence_threshold` | number | Silence duration (seconds) used to close a call. |
| `poll_interval` | number | WAV growth polling interval (seconds). |

### `runtime.config` fields

| Field | Type | Description |
|---|---|---|
| `path` | string | Path to current config file. |
| `last_reload_applied_at` | string\|null | ISO timestamp of last successful hot reload. |
| `last_reload_error` | string\|null | Last rejected reload error message, if any. |

---

