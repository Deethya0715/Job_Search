"""
Google Sheets application tracker.

Appends one row per approved application:

    Company | Role Title | Location | Date Applied | Status | Contact Person |
    Contact Email | Notes | Job Link

Requires a Google Cloud service account JSON (see README) that has been
shared on the spreadsheet as an Editor.
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any

from common import (
    SHEET_HEADERS,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_SYNCED,
    JobPosting,
    append_tracker_log,
    env,
    find_match,
    google_sheets_id,
    load_match_jobs,
    load_profile,
    local_today,
    service_account_path,
    update_match_status,
    utc_now,
)

log = logging.getLogger("tracker_sync")

SHEET_SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)


class TrackerError(RuntimeError):
    """Raised when the spreadsheet cannot be reached or updated."""


def _authorize():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError as exc:
        raise TrackerError(
            "Missing Google libraries. Install with: pip install gspread google-auth"
        ) from exc

    creds_path = service_account_path()
    if not creds_path.is_file():
        raise TrackerError(
            f"Service account file not found: {creds_path}\n"
            "Download the JSON key from Google Cloud and save it as credentials.json "
            "(or set GOOGLE_SERVICE_ACCOUNT_FILE)."
        )

    credentials = Credentials.from_service_account_file(str(creds_path), scopes=SHEET_SCOPES)
    return gspread.authorize(credentials)


def open_worksheet():
    """Open the configured spreadsheet tab, creating headers if the sheet is empty."""
    client = _authorize()
    sheet_id = google_sheets_id()
    try:
        spreadsheet = client.open_by_key(sheet_id)
    except Exception as exc:
        raise TrackerError(
            f"Could not open spreadsheet {sheet_id}. "
            "Share the sheet with the service account client_email as Editor. "
            f"Details: {exc}"
        ) from exc

    tab_name = env("GOOGLE_SHEETS_WORKSHEET")
    if tab_name:
        try:
            worksheet = spreadsheet.worksheet(tab_name)
        except Exception as exc:
            raise TrackerError(f"Worksheet {tab_name!r} was not found: {exc}") from exc
    else:
        worksheet = spreadsheet.sheet1

    existing = worksheet.row_values(1)
    if not existing:
        worksheet.append_row(SHEET_HEADERS, value_input_option="USER_ENTERED")
        log.info("Wrote tracker headers to %s", worksheet.title)
    return spreadsheet, worksheet


def already_synced(worksheet, job: JobPosting) -> bool:
    """Skip append if this job link is already present in the Job Link column."""
    link = job.best_url().strip()
    if not link:
        return False
    try:
        headers = [str(value).strip() for value in worksheet.row_values(1)]
        if "Job Link" in headers:
            col_index = headers.index("Job Link") + 1
        else:
            col_index = SHEET_HEADERS.index("Job Link") + 1
        values = worksheet.col_values(col_index)
    except Exception:
        return False
    return any((cell or "").strip() == link for cell in values[1:])


def append_application(
    job: JobPosting,
    *,
    status: str = "Applied",
    date_applied: str | None = None,
    contact_person: str = "",
    contact_email: str = "",
    notes: str = "",
) -> dict[str, Any]:
    """
    Append one tracker row and record the result in tracker_log.json.

    Never overwrites existing sheet rows; duplicates by job link are skipped.
    """
    if contact_person:
        job.contact_person = contact_person
    if contact_email:
        job.contact_email = contact_email
    if notes:
        job.notes = notes
    job.fill_status = STATUS_APPLIED
    job.applied_at = job.applied_at or utc_now()

    applied_on = date_applied or local_today()
    row = job.sheet_row(applied_on)
    row[4] = status

    result: dict[str, Any] = {
        "job_id": job.id,
        "company": job.company,
        "title": job.title,
        "location": job.location,
        "date_applied": applied_on,
        "status": status,
        "contact_person": job.contact_person,
        "contact_email": job.contact_email,
        "notes": job.notes,
        "job_link": job.best_url(),
        "synced_at": utc_now(),
        "ok": False,
        "error": "",
        "spreadsheet_id": google_sheets_id(),
    }

    try:
        _spreadsheet, worksheet = open_worksheet()
        if already_synced(worksheet, job):
            result["ok"] = True
            result["error"] = "already in sheet"
            job.fill_status = STATUS_SYNCED
            job.synced_at = utc_now()
            job.sheet_error = ""
        else:
            worksheet.append_row(row, value_input_option="USER_ENTERED")
            result["ok"] = True
            job.fill_status = STATUS_SYNCED
            job.synced_at = utc_now()
            job.sheet_error = ""
            log.info("Synced %s — %s", job.company, job.title)
    except Exception as exc:
        result["error"] = str(exc)
        job.fill_status = STATUS_FAILED
        job.sheet_error = str(exc)
        log.warning("Sheet sync failed for %s: %s", job.id, exc)

    append_tracker_log(result)
    update_match_status(
        job.id,
        fill_status=job.fill_status,
        applied_at=job.applied_at,
        synced_at=job.synced_at,
        sheet_error=job.sheet_error,
        contact_person=job.contact_person,
        contact_email=job.contact_email,
        notes=job.notes,
    )
    if not result["ok"]:
        raise TrackerError(result["error"])
    return result


def sync_job_id(job_id: str, **kwargs: Any) -> dict[str, Any]:
    job = find_match(job_id)
    if job is None:
        raise TrackerError(f"Job {job_id} was not found in matches.json / seen_jobs.json.")
    return append_application(job, **kwargs)


def test_connection() -> str:
    spreadsheet, worksheet = open_worksheet()
    title = spreadsheet.title
    tab = worksheet.title
    creds = service_account_path()
    return f"Connected to '{title}' tab '{tab}' using {creds.name}."


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync approved applications to Google Sheets.")
    parser.add_argument("--job-id", help="Sync a single queued job by id.")
    parser.add_argument(
        "--pending-applied",
        action="store_true",
        help="Sync every job whose fill_status is 'applied' but not yet 'synced'.",
    )
    parser.add_argument("--test", action="store_true", help="Verify service-account access and exit.")
    parser.add_argument("--contact-person", default="", help="Optional recruiter name.")
    parser.add_argument("--contact-email", default="", help="Optional recruiter email.")
    parser.add_argument("--notes", default="", help="Optional notes cell.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    load_profile()

    if args.test:
        print(test_connection())
        return

    if args.job_id:
        result = sync_job_id(
            args.job_id,
            contact_person=args.contact_person,
            contact_email=args.contact_email,
            notes=args.notes,
        )
        print(f"[tracker] Synced {result['company']} — {result['title']}")
        return

    if args.pending_applied:
        due = [job for job in load_match_jobs() if job.fill_status == STATUS_APPLIED]
        if not due:
            print("[tracker] No applied-but-unsynced jobs.")
            return
        ok = 0
        for job in due:
            try:
                append_application(job)
                ok += 1
            except TrackerError as exc:
                print(f"[tracker] Failed {job.company}: {exc}")
        print(f"[tracker] Synced {ok}/{len(due)} rows.")
        return

    parser_error = "Pass --job-id, --pending-applied, or --test."
    sys.exit(f"[tracker] {parser_error}")


if __name__ == "__main__":
    try:
        main()
    except TrackerError as exc:
        sys.exit(f"[tracker] {exc}")
    except KeyboardInterrupt:
        sys.exit("\n[tracker] Interrupted.")
