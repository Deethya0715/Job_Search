"""
24/7 background engine.

Runs the multi-board aggregator on a timer, keeps the 9:00 PM America/Chicago
email scheduler armed, and writes a heartbeat to worker_status.json so the
Streamlit dashboard can show live status.

Playwright form-filling is intentionally NOT run here. Headed browsers need
a human at the keyboard; start them from the dashboard or `python bot.py`.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time

import schedule

from aggregator import discover
from common import (
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


def _interval_minutes() -> int:
    raw = env("WORKER_INTERVAL_MINUTES", "45")
    try:
        return max(10, int(raw))
    except ValueError:
        return 45


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


def loop(report_at: str, run_immediately: bool) -> None:
    interval = _interval_minutes()
    _clear_stop_flag()
    save_worker_status(
        state="running",
        pid=os.getpid(),
        started_at=utc_now(),
        next_report_at=f"{report_at} America/Chicago",
        message=f"Armed. Scraping every {interval} min; report at {report_at} CT.",
        last_scrape_error="",
    )
    log.info(
        "Worker pid=%s interval=%s min report=%s America/Chicago",
        os.getpid(),
        interval,
        report_at,
    )

    schedule.clear()
    schedule.every().day.at(report_at).do(nightly_report)
    schedule.every(interval).minutes.do(scrape_only)

    if run_immediately:
        scrape_only()

    while not stop_requested():
        schedule.run_pending()
        save_worker_status(
            state="running",
            pid=os.getpid(),
            next_report_at=f"{report_at} America/Chicago",
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
