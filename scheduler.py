
#!/usr/bin/env python3
"""Randomized scheduler for authorized website monitoring and UI tests.

Examples (Windows PowerShell):
    py scheduler.py
    py scheduler.py --min 120 --max 300
    py scheduler.py --browser-every 10
    py scheduler.py --once
    py scheduler.py --monitor-args "--url https://abdulsamad.shop --verbose"

The monitor runs immediately, then repeats after a randomized delay. Browser
UI tests run every N cycles when --browser-every is greater than zero.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import random
import shlex
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("scheduler")
HERE = Path(__file__).resolve().parent
STOP_REQUESTED = False
CHILD_TIMEOUT_SECONDS = 600


def handle_signal(signum: int, _frame: object) -> None:
    """Ask the scheduler to stop after the current child process finishes."""
    global STOP_REQUESTED
    STOP_REQUESTED = True
    LOG.warning("received signal %s; stopping after the current cycle", signum)


def setup_logging(log_dir: Path, verbose: bool) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s",
        "%Y-%m-%dT%H:%M:%SZ",
    )
    logging.Formatter.converter = time.gmtime

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    console.setLevel(logging.DEBUG if verbose else logging.INFO)

    file_handler = logging.FileHandler(
        log_dir / "scheduler.log",
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.DEBUG)
    root.addHandler(console)
    root.addHandler(file_handler)


def resolve_from_project(path_text: str) -> Path:
    """Resolve relative paths against this script's directory."""
    path = Path(path_text).expanduser()
    return path.resolve() if path.is_absolute() else (HERE / path).resolve()


def load_scheduler_config(config_path: Path) -> dict:
    if not config_path.exists():
        LOG.warning(
            "config file not found at %s; using scheduler defaults",
            config_path,
        )
        return {}

    try:
        with config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)

        if not isinstance(data, dict):
            raise ValueError("top-level JSON value must be an object")

        scheduler_config = data.get("scheduler", {})

        if not isinstance(scheduler_config, dict):
            raise ValueError("'scheduler' must be a JSON object")

        return scheduler_config

    except (OSError, json.JSONDecodeError, ValueError) as exc:
        LOG.warning(
            "could not read %s (%s); using scheduler defaults",
            config_path,
            exc,
        )
        return {}


def positive_number(
    value: object,
    fallback: float,
    setting_name: str,
) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        LOG.warning(
            "invalid %s; using %.0f seconds",
            setting_name,
            fallback,
        )
        return fallback

    if not math.isfinite(result) or result <= 0:
        LOG.warning(
            "invalid %s; using %.0f seconds",
            setting_name,
            fallback,
        )
        return fallback

    return result


def run_child(
    script_name: str,
    config_path: Path,
    extra_args: list[str],
) -> int:
    """Run a project script without a shell and return its exit code."""
    script_path = HERE / script_name

    if not script_path.is_file():
        LOG.error(
            "cannot run %s: file not found at %s",
            script_name,
            script_path,
        )
        return 125

    command = [
        sys.executable,
        str(script_path),
        "--config",
        str(config_path),
        *extra_args,
    ]

    LOG.info("running %s", script_name)
    LOG.debug("command: %s", shlex.join(command))

    try:
        completed = subprocess.run(
            command,
            cwd=str(HERE),
            timeout=CHILD_TIMEOUT_SECONDS,
            check=False,
        )

        LOG.info(
            "%s exited with code %d",
            script_name,
            completed.returncode,
        )
        return completed.returncode

    except subprocess.TimeoutExpired:
        LOG.error(
            "%s exceeded the %d-second timeout",
            script_name,
            CHILD_TIMEOUT_SECONDS,
        )
        return 124

    except OSError as exc:
        LOG.error("could not start %s: %s", script_name, exc)
        return 125


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Randomized website monitoring scheduler"
    )

    parser.add_argument(
        "--config",
        default="config.json",
        help="config JSON path (default: config.json)",
    )
    parser.add_argument(
        "--min",
        dest="min_interval",
        type=float,
        default=None,
        help="minimum delay in seconds (default: config or 120)",
    )
    parser.add_argument(
        "--max",
        dest="max_interval",
        type=float,
        default=None,
        help="maximum delay in seconds (default: config or 300)",
    )
    parser.add_argument(
        "--log-dir",
        default="reports",
        help="directory for scheduler.log",
    )
    parser.add_argument(
        "--browser-every",
        type=int,
        default=0,
        help="run browser_tests.py every N cycles; 0 disables browser tests",
    )
    parser.add_argument(
        "--monitor-args",
        default="",
        help="extra arguments forwarded to monitor.py; quote as one string",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="run one cycle and exit",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="enable debug console logging",
    )

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.browser_every < 0:
        parser.error("--browser-every must be zero or a positive integer")

    if args.min_interval is not None and (
        not math.isfinite(args.min_interval) or args.min_interval <= 0
    ):
        parser.error("--min must be a finite number greater than zero")

    if args.max_interval is not None and (
        not math.isfinite(args.max_interval) or args.max_interval <= 0
    ):
        parser.error("--max must be a finite number greater than zero")

    config_path = resolve_from_project(args.config)
    log_dir = resolve_from_project(args.log_dir)

    setup_logging(log_dir, args.verbose)

    signal.signal(signal.SIGINT, handle_signal)

    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, handle_signal)

    config = load_scheduler_config(config_path)

    default_min = positive_number(
        config.get("min_interval_seconds", 120),
        120,
        "min_interval_seconds",
    )
    default_max = positive_number(
        config.get("max_interval_seconds", 300),
        300,
        "max_interval_seconds",
    )

    low = (
        args.min_interval
        if args.min_interval is not None
        else default_min
    )
    high = (
        args.max_interval
        if args.max_interval is not None
        else default_max
    )

    if low > high:
        parser.error("--min cannot be greater than --max")

    try:
        monitor_extra = shlex.split(
            args.monitor_args,
            posix=(os.name != "nt"),
        )
    except ValueError as exc:
        parser.error(f"could not parse --monitor-args: {exc}")

    LOG.info(
        "scheduler started (pid=%s, interval %.0f-%.0fs, "
        "browser_every=%s, config=%s)",
        os.getpid(),
        low,
        high,
        args.browser_every or "never",
        config_path,
    )

    cycle = 0
    monitor_failures = 0
    browser_failures = 0

    while not STOP_REQUESTED:
        cycle += 1

        LOG.info(
            "===== cycle %d | %s =====",
            cycle,
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

        monitor_rc = run_child(
            "monitor.py",
            config_path,
            monitor_extra,
        )

        if monitor_rc != 0:
            monitor_failures += 1
            LOG.error(
                "monitoring cycle %d failed (exit code %d)",
                cycle,
                monitor_rc,
            )

        if STOP_REQUESTED:
            break

        if args.browser_every and cycle % args.browser_every == 0:
            browser_rc = run_child(
                "browser_tests.py",
                config_path,
                ["--report-dir", str(log_dir)],
            )

            if browser_rc != 0:
                browser_failures += 1
                LOG.error(
                    "browser tests failed on cycle %d (exit code %d)",
                    cycle,
                    browser_rc,
                )
        else:
            LOG.info("browser tests skipped on cycle %d", cycle)

        if args.once or STOP_REQUESTED:
            break

        delay = random.uniform(low, high)

        LOG.info(
            "sleeping %.0f seconds (%.1f minutes) before the next cycle",
            delay,
            delay / 60,
        )

        deadline = time.monotonic() + delay

        while not STOP_REQUESTED:
            remaining = deadline - time.monotonic()

            if remaining <= 0:
                break

            time.sleep(min(1.0, remaining))

    LOG.info(
        "scheduler stopped: %d cycle(s), "
        "%d failed monitoring run(s), %d failed browser run(s)",
        cycle,
        monitor_failures,
        browser_failures,
    )

    return 1 if monitor_failures or browser_failures else 0


if __name__ == "__main__":
    sys.exit(main())
