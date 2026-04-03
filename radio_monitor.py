#!/usr/bin/env python3
"""CLI entrypoint for the radio monitor service."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app_manager import RadioMonitorApp

DEFAULT_CONFIG_PATH = Path("config.json")


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
