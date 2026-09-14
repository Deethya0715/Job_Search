"""
Run the job monitor on a schedule.

Default: scrape every board, then email the Markdown summary at 9:00 PM
America/Chicago every night. Leave this process running (or register it
with Windows Task Scheduler — see README.md).
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import schedule

from aggregator import discover
from common import load_profile
from reporter import report

log = logging.getLogger("scheduler")
LOCAL_TZ = ZoneInfo("America/Chicago")
REPORT_TIME = "21:00"


def run_pipeline() -> None:
    started = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d %I:%M %p %Z")
    log.info("Nightly pipeline started at %s", started)
    profile = load_profile()
    try:
        fresh = discover(profile)
        log.info("Discovery finished: %s new matches.", len(fresh))
        report(fresh, send=True)
    except Exception:
        log.exception("Nightly pipeline failed.")
        raise
    log.info("Nightly pipeline finished.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Schedule the job monitor for 9:00 PM daily.")
    parser.add_argument(
        "--now",
        action="store_true",
        help="Run the scrape + report pipeline immediately, then keep the 9:00 PM schedule.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Run the pipeline once now and exit (no daemon).",
    )
    parser.add_argument("--at", default=REPORT_TIME, help="Daily time as HH:MM (24h, local Chicago time).")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.once:
        run_pipeline()
        return

    schedule.every().day.at(args.at).do(run_pipeline)
    log.info("Armed. Daily report will fire at %s America/Chicago.", args.at)
    log.info("Leave this window open. Press Ctrl+C to stop.")
    if args.now:
        run_pipeline()

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n[scheduler] Stopped.")
