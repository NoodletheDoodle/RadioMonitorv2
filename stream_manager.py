#!/usr/bin/env python3
"""GStreamer process lifecycle and per-channel monitoring."""

from __future__ import annotations

import datetime
import os
import socket
import subprocess
import sys
import threading
import time
from typing import Any

from config_manager import Channel, ConfigManager
from csv_manager import CsvManager


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
        if sys.platform.lower().startswith("linux"):
            return [
                runtime_settings["gstreamer_bin"],
                "-e",
                "udpsrc",
                f"uri=udp://{channel['ip']}:{channel['port']}",
                "multicast-iface=eth0",
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
        return [
            runtime_settings["gstreamer_bin"],
            "-e",
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
        iface_raw = str(self.config_manager.snapshot()["multicast_interface"]).strip()
        iface_ip = self.resolve_membership_iface_ip(iface_raw)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("0.0.0.0", port))
            except OSError:
                sock.bind((group_ip, port))

            mreq = socket.inet_aton(group_ip) + socket.inet_aton(iface_ip)
            try:
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            except OSError as exc:
                if exc.errno == 19 and iface_ip != "0.0.0.0":
                    mreq_any = socket.inet_aton(group_ip) + socket.inet_aton("0.0.0.0")
                    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq_any)
                else:
                    raise
            sock.setblocking(False)
            return sock
        except OSError as exc:
            print(f"[{channel['name']}] WARNING: sender IP capture disabled ({exc}).")
            try:
                sock.close()
            except OSError:
                pass
            return None

    def resolve_membership_iface_ip(self, iface_value: str) -> str:
        """Return an IPv4 address suitable for IP_ADD_MEMBERSHIP interface field."""
        if not iface_value:
            return "0.0.0.0"

        try:
            socket.inet_aton(iface_value)
            return iface_value
        except OSError:
            pass

        if sys.platform.lower().startswith("linux"):
            try:
                import importlib

                netifaces = importlib.import_module("netifaces")

                addrs = netifaces.ifaddresses(iface_value)
                for addr_info in addrs.get(netifaces.AF_INET, []):
                    ip_addr = str(addr_info.get("addr", "")).strip()
                    if ip_addr:
                        return ip_addr
            except ImportError:
                pass
            except Exception:
                pass

        return "0.0.0.0"

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
    ) -> tuple[bool, str, datetime.datetime | None]:
        """Track growth/silence and return call activity, sender IP, and first-audio time."""
        last_size = 0
        last_growth_time = time.monotonic()
        call_had_audio = False
        call_sender_ip = ""
        first_audio_dt: datetime.datetime | None = None

        while (
            not self.shutdown_event.is_set()
            and not self.reload_channels_event.is_set()
        ):
            snap = self.config_manager.snapshot()
            poll_interval = snap["poll_interval"]
            silence_threshold = snap["ptt_end_silence_threshold"]
            time.sleep(poll_interval)

            if sender_sock is not None:
                while True:
                    try:
                        _payload, (sender_ip, _sender_port) = sender_sock.recvfrom(4096)
                        call_sender_ip = sender_ip
                    except BlockingIOError:
                        break
                    except OSError:
                        break

            try:
                current_size = os.path.getsize(staging)
            except FileNotFoundError:
                current_size = 0

            if current_size > last_size:
                last_size = current_size
                last_growth_time = time.monotonic()
                if first_audio_dt is None:
                    first_audio_dt = datetime.datetime.now()
                call_had_audio = True
            elif call_had_audio:
                silence_duration = time.monotonic() - last_growth_time
                if silence_duration >= silence_threshold:
                    break

        return call_had_audio, call_sender_ip, first_audio_dt

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
                    if sender_sock is None:
                        now = time.monotonic()
                        if now >= next_sender_sock_retry_at:
                            sender_sock = self.build_sender_socket(channel)
                            if sender_sock is None:
                                next_sender_sock_retry_at = now + 5.0
                            else:
                                next_sender_sock_retry_at = now

                    runtime_settings = self.config_manager.snapshot()
                    recordings_base = runtime_settings["recordings_base"]
                    staging = self.prepare_staging_file(name, recordings_base)

                    proc = self.start_call_process(
                        channel,
                        name,
                        staging,
                        runtime_settings,
                    )
                    if proc is None:
                        continue

                    call_had_audio, call_sender_ip, first_audio_dt = self.monitor_call_activity(
                        staging,
                        sender_sock,
                    )

                    call_start_dt = first_audio_dt or datetime.datetime.now()

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
