#!/usr/bin/env python3
"""CSV row formatting and persistence helpers."""

from __future__ import annotations

import csv
import datetime
import os
import threading
from typing import Any

from config_manager import ConfigManager

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


class CsvManager:
    """Handles all CSV pathing and writing behavior."""

    def __init__(
        self, config_manager: ConfigManager, script_start_time: datetime.datetime
    ) -> None:
        self.config_manager = config_manager
        self.script_start_time = script_start_time
        self._write_lock = threading.Lock()

    def elapsed_str(self, dt: datetime.datetime) -> str:
        """Return elapsed runtime clock string relative to script start."""
        delta = dt - self.script_start_time
        total_seconds = int(delta.total_seconds())
        hours, remainder = divmod(total_seconds, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def csv_path(self, channel_name: str) -> str:
        """Return path to the per-channel daily CSV file for the current day."""
        logs_base = self.config_manager.snapshot()["logs_base"]
        log_dir = os.path.join(logs_base, datetime.date.today().strftime("%Y-%m-%d"))
        os.makedirs(log_dir, exist_ok=True)
        return os.path.join(log_dir, f"{channel_name}.csv")

    def recover_max_unique_id(self, channel_name: str) -> int:
        """Return max unique_id seen for channel in today's CSV logs, else 0."""
        logs_base = self.config_manager.snapshot()["logs_base"]
        log_dir = os.path.join(logs_base, datetime.date.today().strftime("%Y-%m-%d"))
        if not os.path.isdir(log_dir):
            return 0

        max_uid = 0
        daily_name = f"{channel_name}.csv"
        legacy_prefix = f"{channel_name}_"
        for name in os.listdir(log_dir):
            if not name.endswith(".csv"):
                continue
            if name != daily_name and not name.startswith(legacy_prefix):
                continue

            path = os.path.join(log_dir, name)
            if not os.path.isfile(path):
                continue

            try:
                with open(path, "r", newline="") as csvfile:
                    reader = csv.DictReader(csvfile)
                    for row in reader:
                        try:
                            value = int(str(row.get("unique_id", "")).strip())
                        except (TypeError, ValueError):
                            continue
                        if value > max_uid:
                            max_uid = value
            except OSError:
                continue

        return max_uid

    def write_row(self, channel_name: str, row_data: dict[str, Any]) -> None:
        """Append one call metadata row, creating header when file is new."""
        path = self.csv_path(channel_name)
        with self._write_lock:
            file_exists = os.path.isfile(path)
            with open(path, "a", newline="") as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=CSV_COLUMNS)
                if not file_exists:
                    writer.writeheader()
                writer.writerow(row_data)
