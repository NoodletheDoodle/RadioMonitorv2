#!/usr/bin/env python3
"""
radio_monitor.py

Monitors multiple PTT radio channels transmitted as Opus/RTP over UDP multicast.
Each active PTT transmission is recorded as a .WAV file via GStreamer.
Metadata for each call is logged to a per-channel daily CSV file.

PTT detection is based on WAV file size growth monitoring — no audio decoding in Python.
GStreamer handles all audio work via subprocess.Popen.
"""

import csv
import datetime
import os
import shutil
import signal
import subprocess
import threading
import time

# ---------------------------------------------------------------------------
# CHANNEL CONFIGURATION — edit this list to add/remove channels
# Use underscores in names (no spaces) — names are used as folder and file names
# ---------------------------------------------------------------------------
CHANNELS = [
    {"name": "park_ranger_1",   "ip": "239.192.49.1", "port": 60322},
    {"name": "park_ranger_2",   "ip": "239.192.49.3", "port": 60326},
]

# ---------------------------------------------------------------------------
# NETWORK CONFIGURATION
# ---------------------------------------------------------------------------
MULTICAST_INTERFACE = "10.3.1.253"   # Pi's ethernet interface IP address

# ---------------------------------------------------------------------------
# PTT DETECTION TUNING
# ---------------------------------------------------------------------------
PTT_END_SILENCE_THRESHOLD = 2.0   # Seconds of no file growth before call is closed
POLL_INTERVAL = 0.5               # How often (seconds) to check the WAV file size

# ---------------------------------------------------------------------------
# GSTREAMER BINARY
# On Linux/Pi: "gst-launch-1.0" (must be on PATH)
# On Windows:  full path if not on PATH, e.g.:
#   r"C:\gstreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe"
# ---------------------------------------------------------------------------
GST_LAUNCH_BIN = r"C:\Users\kevin\AppData\Local\Programs\gstreamer\1.0\msvc_x86_64\bin\gst-launch-1.0.exe"

# ---------------------------------------------------------------------------
# OUTPUT PATHS
# ---------------------------------------------------------------------------
RECORDINGS_BASE = "./recordings"
LOGS_BASE = "./logs"

# ---------------------------------------------------------------------------
# GLOBAL STATE
# ---------------------------------------------------------------------------
script_start_time = datetime.datetime.now()
shutdown_event = threading.Event()   # Set on SIGTERM / KeyboardInterrupt


# ---------------------------------------------------------------------------
# HELPERS — paths and timestamps
# ---------------------------------------------------------------------------

def today_str():
    """Return today's date as YYYY-MM-DD (evaluated at call time for midnight rollover)."""
    return datetime.date.today().strftime("%Y-%m-%d")


def now_local():
    """Return current local datetime."""
    return datetime.datetime.now()


def elapsed_str(dt):
    """Return HH:MM:SS elapsed since script start for a given datetime."""
    delta = dt - script_start_time
    total_seconds = int(delta.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def recordings_dir(channel_name):
    """Return the directory where WAV files for a channel are stored today."""
    return os.path.join(RECORDINGS_BASE, today_str(), channel_name)


def staging_wav_path(channel_name):
    """Return the path for the temporary WAV file GStreamer writes to during a call."""
    return os.path.join(recordings_dir(channel_name), f"staging_{channel_name}.wav")


def final_wav_path(channel_name, start_dt):
    """Return the final WAV file path for a completed call."""
    timestamp = start_dt.strftime("%Y%m%d_%H%M%S")
    filename = f"{channel_name}_{timestamp}.wav"
    return os.path.join(recordings_dir(channel_name), filename), filename


def csv_path(channel_name):
    """Return the CSV log file path for a channel.
    Includes the script start timestamp so each run produces its own file."""
    log_dir = os.path.join(LOGS_BASE, today_str())
    os.makedirs(log_dir, exist_ok=True)
    run_ts = script_start_time.strftime("%Y%m%d_%H%M%S")
    return os.path.join(log_dir, f"{channel_name}_{run_ts}.csv")


# ---------------------------------------------------------------------------
# CSV LOGGING
# ---------------------------------------------------------------------------

CSV_COLUMNS = [
    "unique_id",
    "channel_name",
    "relative_start",
    "relative_end",
    "local_start_time",
    "local_end_time",
    "duration_seconds",
    "wav_filename",
]


def write_csv_row(channel_name, row_data):
    """
    Append a single call record to the channel's daily CSV file.
    Creates the file with a header row if it does not yet exist.
    """
    path = csv_path(channel_name)
    file_exists = os.path.isfile(path)
    with open(path, "a", newline="") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=CSV_COLUMNS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row_data)


# ---------------------------------------------------------------------------
# GSTREAMER SUBPROCESS
# ---------------------------------------------------------------------------

def build_gst_command(channel, output_path):
    """
    Build the gst-launch-1.0 command list for a single channel.
    Writes decoded Opus audio directly to a WAV file at output_path.
    """
    return [
        GST_LAUNCH_BIN,
        "udpsrc",
        f"address={channel['ip']}",
        f"port={channel['port']}",
        "auto-multicast=true",
        f"multicast-iface={MULTICAST_INTERFACE}",
        'caps=application/x-rtp, media=audio, encoding-name=OPUS, payload=112',
        "!", "queue",
        "!", "rtpopusdepay",
        "!", "queue",
        "!", "opusdec",
        "!", "queue",
        "!", "audioconvert",
        "!", "audioresample",
        "!", "wavenc",
        "!", "filesink", f"location={output_path.replace(os.sep, '/')}",
    ]


def launch_gstreamer(channel, output_path):
    """
    Start a GStreamer subprocess writing audio to output_path.
    Returns the Popen object.
    """
    cmd = build_gst_command(channel, output_path)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return proc


def terminate_gstreamer(proc):
    """
    Gracefully terminate a GStreamer subprocess, with a hard kill fallback.
    Sends SIGTERM first, waits up to 3 seconds, then SIGKILL if still running.
    """
    if proc is None or proc.poll() is not None:
        return   # Already finished
    try:
        proc.terminate()
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


# ---------------------------------------------------------------------------
# PER-CHANNEL MONITOR THREAD
# ---------------------------------------------------------------------------

def monitor_channel(channel, call_counter_lock, call_counters):
    """
    Runs in its own thread. Continuously listens on one channel.

    Lifecycle per PTT call:
      1. Create output directory and staging WAV path.
      2. Launch GStreamer subprocess writing to the staging file.
      3. Poll the staging file size every POLL_INTERVAL seconds.
      4. When size stops growing for PTT_END_SILENCE_THRESHOLD seconds:
           a. Terminate GStreamer.
           b. Rename staging file to final timestamped name.
           c. Log the call to CSV.
           d. Start over immediately for the next call.
    """
    name = channel["name"]

    while not shutdown_event.is_set():

        # --- Step 1: Prepare staging path (re-evaluated each call for rollover) ---
        out_dir = recordings_dir(name)
        os.makedirs(out_dir, exist_ok=True)
        staging = staging_wav_path(name)

        # Remove any leftover staging file from a previous run
        if os.path.exists(staging):
            os.remove(staging)

        # --- Step 2: Record the call start time and launch GStreamer ---
        call_start_dt = now_local()
        try:
            proc = launch_gstreamer(channel, staging)
        except FileNotFoundError:
            print(f"[{name}] ERROR: gst-launch-1.0 not found. "
                  "Ensure GStreamer is installed and on PATH. Retrying in 5s...")
            time.sleep(5)
            continue
        except OSError as e:
            print(f"[{name}] ERROR launching GStreamer: {e}. Retrying in 5s...")
            time.sleep(5)
            continue

        # --- Step 3: Poll file size to detect PTT activity ---
        last_size = 0
        last_growth_time = time.monotonic()
        call_had_audio = False   # Guard against logging zero-byte files

        while not shutdown_event.is_set():
            time.sleep(POLL_INTERVAL)

            # Read current file size (0 if file doesn't exist yet)
            try:
                current_size = os.path.getsize(staging)
            except FileNotFoundError:
                current_size = 0

            if current_size > last_size:
                # File is still growing — PTT is active
                last_size = current_size
                last_growth_time = time.monotonic()
                call_had_audio = True

            elif call_had_audio:
                # File stopped growing — check silence duration
                silence_duration = time.monotonic() - last_growth_time
                if silence_duration >= PTT_END_SILENCE_THRESHOLD:
                    break   # PTT call has ended

        # --- Step 4a: Terminate GStreamer ---
        terminate_gstreamer(proc)

        # --- Step 4b: Handle shutdown mid-call ---
        if shutdown_event.is_set():
            # Preserve the partial recording on shutdown
            if call_had_audio and os.path.exists(staging) and os.path.getsize(staging) > 0:
                final_path, wav_filename = final_wav_path(name, call_start_dt)
                out_dir = os.path.dirname(final_path)
                os.makedirs(out_dir, exist_ok=True)
                os.rename(staging, final_path)
                call_end_dt = now_local()
                duration = (call_end_dt - call_start_dt).total_seconds()
                # Assign a unique ID for this call
                with call_counter_lock:
                    call_counters[name] = call_counters.get(name, 0) + 1
                    uid = call_counters[name]
                write_csv_row(name, {
                    "unique_id":         uid,
                    "channel_name":      name,
                    "relative_start":    elapsed_str(call_start_dt),
                    "relative_end":      elapsed_str(call_end_dt),
                    "local_start_time":  call_start_dt.strftime("%H:%M:%S"),
                    "local_end_time":    call_end_dt.strftime("%H:%M:%S"),
                    "duration_seconds":  f"{duration:.1f}",
                    "wav_filename":      wav_filename,
                })
            elif os.path.exists(staging):
                os.remove(staging)   # Clean up empty staging file
            break

        # Skip logging if no real audio was captured (e.g., GStreamer failed to start)
        if not call_had_audio or not os.path.exists(staging) or os.path.getsize(staging) == 0:
            if os.path.exists(staging):
                os.remove(staging)
            continue

        # --- Step 4b: Rename staging file to final name ---
        call_end_dt = now_local()
        final_path, wav_filename = final_wav_path(name, call_start_dt)
        # Ensure directory exists (date may have rolled over at midnight)
        out_dir = os.path.dirname(final_path)
        os.makedirs(out_dir, exist_ok=True)
        os.rename(staging, final_path)

        # --- Step 4c: Write CSV metadata row ---
        duration = (call_end_dt - call_start_dt).total_seconds()
        with call_counter_lock:
            call_counters[name] = call_counters.get(name, 0) + 1
            uid = call_counters[name]

        write_csv_row(name, {
            "unique_id":         uid,
            "channel_name":      name,
            "relative_start":    elapsed_str(call_start_dt),
            "relative_end":      elapsed_str(call_end_dt),
            "local_start_time":  call_start_dt.strftime("%H:%M:%S"),
            "local_end_time":    call_end_dt.strftime("%H:%M:%S"),
            "duration_seconds":  f"{duration:.1f}",
            "wav_filename":      wav_filename,
        })
        # Loop immediately — GStreamer for the next call starts at the top of the while loop


# ---------------------------------------------------------------------------
# STARTUP BANNER
# ---------------------------------------------------------------------------

def print_startup_info():
    """Print script start time, disk space, and configured channels."""
    print("=" * 60)
    print("  Radio Monitor — Starting")
    print(f"  Script start time : {script_start_time.strftime('%Y-%m-%d %H:%M:%S')}")

    # Disk space on the filesystem where recordings will be stored
    os.makedirs(RECORDINGS_BASE, exist_ok=True)
    usage = shutil.disk_usage(RECORDINGS_BASE)
    free_gb = usage.free / (1024 ** 3)
    total_gb = usage.total / (1024 ** 3)
    print(f"  Disk space        : {free_gb:.1f} GB free / {total_gb:.1f} GB total")

    print(f"  Interface IP      : {MULTICAST_INTERFACE}")
    print(f"  Silence threshold : {PTT_END_SILENCE_THRESHOLD}s")
    print(f"  Channels ({len(CHANNELS)}):")
    for ch in CHANNELS:
        print(f"    [{ch['name']}]  {ch['ip']}:{ch['port']}")
    print("=" * 60)
    print("  Monitoring started. Press Ctrl+C to stop.")
    print("=" * 60)


# ---------------------------------------------------------------------------
# SIGNAL HANDLING
# ---------------------------------------------------------------------------

def handle_shutdown(signum, frame):
    """Signal handler for SIGTERM and SIGINT — triggers a clean shutdown."""
    print("\n[Shutdown] Signal received — stopping all channels...")
    shutdown_event.set()


# ---------------------------------------------------------------------------
# MAIN
# ---------------------------------------------------------------------------

def check_gstreamer():
    """
    Verify gst-launch-1.0 is reachable before starting threads.
    Exits with a clear message if not found.
    """
    try:
        result = subprocess.run(
            [GST_LAUNCH_BIN, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        version_line = result.stdout.decode(errors="replace").splitlines()[0]
        print(f"  GStreamer        : {version_line}")
    except FileNotFoundError:
        print(f"\nERROR: '{GST_LAUNCH_BIN}' not found.")
        print("  - Confirm GStreamer is installed and its bin\\ folder is on PATH.")
        print(f"  - Or set GST_LAUNCH_BIN to the full path in the script.")
        print(f"  - Current PATH:\n    " + "\n    ".join(os.environ.get("PATH", "").split(os.pathsep)))
        raise SystemExit(1)


def main():
    # Register signal handlers for clean shutdown
    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    print_startup_info()
    check_gstreamer()

    # Shared counter state — each channel tracks its own call ID
    call_counter_lock = threading.Lock()
    call_counters = {}   # {channel_name: int}

    # Launch one thread per channel
    threads = []
    for channel in CHANNELS:
        t = threading.Thread(
            target=monitor_channel,
            args=(channel, call_counter_lock, call_counters),
            name=f"monitor-{channel['name']}",
            daemon=True,
        )
        t.start()
        threads.append(t)

    # Block main thread until shutdown is triggered
    try:
        while not shutdown_event.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        # Ctrl+C arrives here if the signal handler wasn't fast enough
        shutdown_event.set()

    # Wait for all channel threads to finish their cleanup
    for t in threads:
        t.join(timeout=10)

    print("[Shutdown] All channels stopped. Exiting.")


if __name__ == "__main__":
    main()
