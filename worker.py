"""
24/7 background engine.

Keeps the process running, scrapes hourly from 8:00 AM through 7:00 PM
America/Chicago, keeps the 9:00 PM email scheduler armed, and writes a
heartbeat to worker_status.json so the Streamlit dashboard can show live status.

New $150k+ matches on Greenhouse, Lever, and Ashby are queued automatically.
`discover()` launches `bot.py --auto-prep`, which fills forms and, when
`AUTO_SUBMIT=1`, clicks Submit. Other career portals are skipped.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta

import schedule

from aggregator import discover
from common import (
    LOCAL_TZ,
    ROOT,
    STOP_FLAG_PATH,
    WORKER_LOG_PATH,
    env,
    load_profile,
    load_worker_status,
    pid_is_alive,
    save_worker_status,
    utc_now,
)
from scheduler import REPORT_TIME, run_pipeline

log = logging.getLogger("worker")

DEFAULT_SCRAPE_START_HOUR = 8
DEFAULT_SCRAPE_END_HOUR = 19


def _scrape_hours() -> tuple[int, int]:
    """Inclusive clock hours for scrapes (default 8 AM through 7 PM CT)."""
    try:
        start = int(env("SCRAPE_HOURS_START", str(DEFAULT_SCRAPE_START_HOUR)))
        end = int(env("SCRAPE_HOURS_END", str(DEFAULT_SCRAPE_END_HOUR)))
    except ValueError:
        return DEFAULT_SCRAPE_START_HOUR, DEFAULT_SCRAPE_END_HOUR
    start = max(0, min(23, start))
    end = max(0, min(23, end))
    if end < start:
        return DEFAULT_SCRAPE_START_HOUR, DEFAULT_SCRAPE_END_HOUR
    return start, end


def _format_clock_hour(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    display = hour % 12 or 12
    return f"{display}:00 {suffix}"


def _window_label() -> str:
    start, end = _scrape_hours()
    return f"{_format_clock_hour(start)} to {_format_clock_hour(end)} CT"


def in_scrape_window(now: datetime | None = None) -> bool:
    now = now or datetime.now(LOCAL_TZ)
    start, end = _scrape_hours()
    return start <= now.hour <= end


def next_scrape_at(now: datetime | None = None) -> datetime:
    now = now or datetime.now(LOCAL_TZ)
    start, end = _scrape_hours()
    cursor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    for _ in range(36):
        if start <= cursor.hour <= end:
            return cursor
        cursor += timedelta(hours=1)
    return cursor


def _next_scrape_label(now: datetime | None = None) -> str:
    return next_scrape_at(now).strftime("%Y-%m-%d %I:%M %p %Z")


def _clear_stop_flag() -> None:
    try:
        STOP_FLAG_PATH.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass


def stop_requested() -> bool:
    return STOP_FLAG_PATH.exists()


def request_stop() -> None:
    STOP_FLAG_PATH.write_text("stop", encoding="utf-8")


def worker_is_running() -> bool:
    status = load_worker_status()
    return status.get("state") == "running" and pid_is_alive(status.get("pid"))


def start_worker_process(*, initial_scrape: bool = True) -> int:
    """Spawn worker.py as a detached child so Streamlit reruns cannot kill it."""
    if worker_is_running():
        return int(load_worker_status()["pid"])

    _clear_stop_flag()
    command = [sys.executable, str(ROOT / "worker.py")]
    if not initial_scrape:
        command.append("--no-initial-scrape")

    kwargs: dict = {
        "cwd": str(ROOT),
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "stdin": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | 0x08000000
        kwargs["close_fds"] = False
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)
    save_worker_status(
        state="running",
        pid=process.pid,
        started_at=utc_now(),
        message="Worker process launched from the dashboard.",
    )
    return process.pid


def stop_worker_process() -> None:
    request_stop()
    status = load_worker_status()
    pid = status.get("pid")
    deadline = time.time() + 8
    while time.time() < deadline:
        if not pid_is_alive(pid):
            break
        time.sleep(0.4)
    if pid_is_alive(pid) and pid:
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                os.kill(int(pid), 15)
        except Exception:
            pass
    save_worker_status(state="stopped", pid=None, message="Worker stopped from the dashboard.")
    _clear_stop_flag()


def configure_logging(to_file: bool = True) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if to_file:
        handlers.append(logging.FileHandler(WORKER_LOG_PATH, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
        handlers=handlers,
        force=True,
    )


def scrape_only() -> int:
    """Periodic discovery without sending email (the 9 PM job does that)."""
    profile = load_profile()
    save_worker_status(
        state="running",
        pid=os.getpid(),
        message="Scraping LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google, Greenhouse, Lever…",
        last_scrape_error="",
    )
    try:
        fresh = discover(profile)
        save_worker_status(
            state="running",
            pid=os.getpid(),
            last_scrape_at=utc_now(),
            last_scrape_new=len(fresh),
            last_scrape_error="",
            next_scrape_at=_next_scrape_label(),
            message=f"Last scrape found {len(fresh)} new matching role(s).",
        )
        log.info("Scrape finished: %s new matches.", len(fresh))
        return len(fresh)
    except Exception as exc:
        log.exception("Scrape failed.")
        save_worker_status(
            state="running",
            pid=os.getpid(),
            last_scrape_at=utc_now(),
            last_scrape_error=str(exc),
            next_scrape_at=_next_scrape_label(),
            message=f"Scrape failed: {exc}",
        )
        return 0


def nightly_report() -> None:
    save_worker_status(
        state="running",
        pid=os.getpid(),
        message="Running the 9:00 PM scrape + email pipeline.",
    )
    try:
        run_pipeline()
        save_worker_status(
            state="running",
            pid=os.getpid(),
            last_report_at=utc_now(),
            last_scrape_at=utc_now(),
            message="Nightly email report sent (or saved if SMTP is unset).",
        )
    except Exception as exc:
        log.exception("Nightly pipeline failed.")
        save_worker_status(
            state="running",
            pid=os.getpid(),
            last_scrape_error=str(exc),
            message=f"Nightly pipeline failed: {exc}",
        )


def _armed_message(report_at: str) -> str:
    return (
        f"Armed. Scrapes hourly {_window_label()}; "
        f"next scrape {_next_scrape_label()}; report at {report_at} CT."
    )


def loop(report_at: str, run_immediately: bool) -> None:
    start_hour, end_hour = _scrape_hours()
    _clear_stop_flag()
    save_worker_status(
        state="running",
        pid=os.getpid(),
        started_at=utc_now(),
        next_report_at=f"{report_at} America/Chicago",
        next_scrape_at=_next_scrape_label(),
        message=_armed_message(report_at),
        last_scrape_error="",
    )
    log.info(
        "Worker pid=%s hourly %s report=%s America/Chicago",
        os.getpid(),
        _window_label(),
        report_at,
    )

    schedule.clear()
    schedule.every().day.at(report_at, "America/Chicago").do(nightly_report)
    for hour in range(start_hour, end_hour + 1):
        schedule.every().day.at(f"{hour:02d}:00", "America/Chicago").do(scrape_only)

    if run_immediately and in_scrape_window():
        scrape_only()
    elif run_immediately:
        log.info(
            "Outside scrape window (%s); waiting until %s.",
            _window_label(),
            _next_scrape_label(),
        )

    while not stop_requested():
        schedule.run_pending()
        save_worker_status(
            state="running",
            pid=os.getpid(),
            next_report_at=f"{report_at} America/Chicago",
            next_scrape_at=_next_scrape_label(),
        )
        time.sleep(2)

    log.info("Stop flag detected — shutting down.")
    save_worker_status(
        state="stopped",
        pid=None,
        message="Worker stopped from the dashboard.",
    )
    _clear_stop_flag()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="24/7 job-monitor background engine.")
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run one scrape now and exit (no heartbeat loop).",
    )
    parser.add_argument(
        "--report-now",
        action="store_true",
        help="Run the nightly scrape+email pipeline once and exit.",
    )
    parser.add_argument(
        "--no-initial-scrape",
        action="store_true",
        help="Arm the schedule without scraping immediately on start.",
    )
    parser.add_argument("--at", default=REPORT_TIME, help="Daily report time HH:MM (Chicago).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_logging(to_file=True)
    os.chdir(ROOT)

    if args.once:
        scrape_only()
        save_worker_status(state="stopped", pid=None, message="Single scrape finished.")
        return
    if args.report_now:
        nightly_report()
        save_worker_status(state="stopped", pid=None, message="Single report finished.")
        return

    try:
        loop(args.at, run_immediately=not args.no_initial_scrape)
    except KeyboardInterrupt:
        save_worker_status(state="stopped", pid=None, message="Worker interrupted.")
        raise


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n[worker] Stopped.")
