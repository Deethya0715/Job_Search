"""
Streamlit control plane for the job-hunting engine.

Tabs:
  1. Live Monitor Status  — start/stop the 24/7 worker
  2. Discovered Jobs Feed — scored listings from every board
  3. Application Queue    — Playwright-prepped roles waiting for review
  4. Tracker Sync Status  — rows pushed to Google Sheets
"""

from __future__ import annotations

import os
import subprocess
import sys
import pandas as pd
import streamlit as st

from common import (
    BOT_LOCK_PATH,
    ROOT,
    STATUS_APPLIED,
    STATUS_PENDING,
    STATUS_PREPPED,
    STATUS_SKIPPED,
    STATUS_SYNCED,
    WORKER_LOG_PATH,
    count_by_status,
    env,
    find_match,
    google_sheets_id,
    jobs_found_on,
    load_match_jobs,
    load_profile,
    load_seen_jobs,
    load_tracker_log,
    load_worker_status,
    local_now,
    service_account_path,
    update_match_status,
)
from worker import start_worker_process, stop_worker_process, worker_is_running

SHEET_URL = f"https://docs.google.com/spreadsheets/d/{google_sheets_id()}/edit"


def _inject_css() -> None:
    st.markdown(
        """
        <style>
          .block-container { padding-top: 1.4rem; max-width: 1400px; }
          div[data-testid="stMetric"] {
            background: #111827;
            border: 1px solid #1f2937;
            border-radius: 14px;
            padding: 12px 16px;
          }
          div[data-testid="stMetric"] label { color: #9ca3af !important; }
          .status-pill {
            display: inline-block;
            padding: 4px 12px;
            border-radius: 999px;
            font-size: 0.85rem;
            font-weight: 600;
          }
          .status-on { background: #064e3b; color: #6ee7b7; }
          .status-off { background: #3f3f46; color: #d4d4d8; }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _jobs_to_frame(jobs, *, include_status: bool = True) -> pd.DataFrame:
    rows = []
    for job in jobs:
        rows.append(
            {
                "Score": job.match_score,
                "Company": job.company,
                "Role": job.title,
                "Location": job.location or "—",
                "Salary": job.salary_label(),
                "Source": job.source,
                "Status": job.fill_status,
                "Skills": ", ".join(job.matched_skills[:6]),
                "Link": job.best_url(),
                "id": job.id,
            }
        )
    frame = pd.DataFrame(rows)
    if frame.empty:
        return frame
    if not include_status:
        frame = frame.drop(columns=["Status"])
    return frame


def _show_table(frame: pd.DataFrame) -> None:
    if frame.empty:
        st.info("Nothing to show yet. Start the monitor or run a scrape.")
        return
    visible = frame.drop(columns=["id"]) if "id" in frame.columns else frame
    column_config = {
        "Link": st.column_config.LinkColumn("Job link", display_text="Open"),
        "Score": st.column_config.NumberColumn(format="%.1f"),
    }
    st.dataframe(
        visible,
        use_container_width=True,
        hide_index=True,
        column_config=column_config,
        height=min(560, 52 + 36 * max(len(visible), 3)),
    )


def _tail_log(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return "No worker log yet."
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return f"Could not read log: {exc}"
    return "\n".join(text.splitlines()[-lines:]) or "Log is empty."


def launch_playwright(job_id: str) -> str:
    if BOT_LOCK_PATH.exists():
        return "A Playwright window is already open. Finish that review first."
    job = find_match(job_id)
    if job is None or not job.best_url():
        return "That job has no apply URL."
    command = [sys.executable, str(ROOT / "bot.py"), "--job-id", job_id]
    kwargs: dict = {"cwd": str(ROOT)}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_CONSOLE
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(command, **kwargs)
    update_match_status(job_id, fill_status=STATUS_PREPPED)
    return f"Opened Playwright for {job.company}. Review the form, submit yourself, then confirm in the console."


def mark_submitted_and_sync(job_id: str, contact_person: str, contact_email: str, notes: str) -> str:
    job = find_match(job_id)
    if job is None:
        return "Job not found."
    job.contact_person = contact_person
    job.contact_email = contact_email
    job.notes = notes or job.notes
    update_match_status(
        job_id,
        fill_status=STATUS_APPLIED,
        contact_person=contact_person,
        contact_email=contact_email,
        notes=job.notes,
    )
    try:
        from tracker_sync import append_application

        append_application(
            job,
            contact_person=contact_person,
            contact_email=contact_email,
            notes=job.notes,
        )
        return f"Synced {job.company} — {job.title} to Google Sheets."
    except Exception as exc:
        return f"Could not sync Google Sheets: {exc}"


def render_monitor() -> None:
    status = load_worker_status()
    running = worker_is_running()
    if status.get("state") == "running" and not running:
        status["state"] = "stopped"
        status["message"] = "Worker process is not running."

    pill = (
        '<span class="status-pill status-on">ACTIVE</span>'
        if running
        else '<span class="status-pill status-off">STOPPED</span>'
    )
    st.markdown(f"### Engine {pill}", unsafe_allow_html=True)
    st.caption(status.get("message") or "")

    left, right = st.columns([1, 2])
    with left:
        wanted = st.toggle("Run 24/7 monitor", value=running)
        if wanted and not running:
            try:
                pid = start_worker_process(initial_scrape=True)
                st.success(f"Worker started (pid {pid}). First scrape is in progress.")
                st.rerun()
            except Exception as exc:
                st.error(f"Could not start worker: {exc}")
        if not wanted and running:
            stop_worker_process()
            st.info("Stop requested. Heartbeat will go idle.")
            st.rerun()

        scrape_now = st.button("Scrape once now", use_container_width=True)
        if scrape_now:
            with st.spinner("Querying every board…"):
                from aggregator import discover

                fresh = discover(load_profile())
            st.success(f"Scrape finished. {len(fresh)} new matching role(s).")
            st.rerun()
        if st.button("Refresh status", use_container_width=True):
            st.rerun()

    with right:
        st.write(
            {
                "PID": status.get("pid"),
                "Started": status.get("started_at") or "—",
                "Heartbeat": status.get("last_heartbeat") or "—",
                "Last scrape": status.get("last_scrape_at") or "—",
                "New last scrape": status.get("last_scrape_new"),
                "Nightly report": status.get("next_report_at"),
                "Last report": status.get("last_report_at") or "—",
            }
        )
        if status.get("last_scrape_error"):
            st.error(status["last_scrape_error"])

    st.subheader("Worker log")
    st.code(_tail_log(WORKER_LOG_PATH), language="text")


def render_feed() -> None:
    jobs = load_seen_jobs()
    jobs.sort(key=lambda job: (-job.match_score, job.company.lower()))
    sources = sorted({job.source for job in jobs})
    col_a, col_b, col_c = st.columns(3)
    min_score = col_a.slider("Minimum match score", 0, 100, 40)
    source = col_b.selectbox("Board", ["All"] + sources)
    search = col_c.text_input("Search company or title")

    filtered = []
    for job in jobs:
        if job.match_score < min_score:
            continue
        if source != "All" and job.source != source:
            continue
        hay = f"{job.company} {job.title} {job.location}".lower()
        if search and search.lower() not in hay:
            continue
        filtered.append(job)

    st.caption(f"{len(filtered)} of {len(jobs)} discovered roles")
    _show_table(_jobs_to_frame(filtered[:250]))


def render_queue() -> None:
    jobs = load_match_jobs()
    queue = [
        job
        for job in jobs
        if job.fill_status in {STATUS_PENDING, STATUS_PREPPED, STATUS_APPLIED}
    ]
    queue.sort(key=lambda job: (-job.match_score, job.company.lower()))
    if not queue:
        st.info("Queue is empty. Start the monitor to discover $150k+ New Grad / Full Stack roles.")
        return

    labels = {
        f"{job.match_score:.0f}  {job.company} — {job.title}  ({job.fill_status})": job.id
        for job in queue
    }
    selected_label = st.selectbox("Select a role", list(labels))
    job_id = labels[selected_label]
    job = find_match(job_id)
    if job is None:
        st.warning("Job disappeared from the queue.")
        return

    st.markdown(f"**{job.company} — {job.title}**")
    meta1, meta2, meta3 = st.columns(3)
    meta1.write(f"Score: **{job.match_score:.1f}**")
    meta2.write(f"Salary: **{job.salary_label()}**")
    meta3.write(f"Status: `{job.fill_status}`")
    st.write(f"Location: {job.location or '—'}  ·  Source: `{job.source}`")
    st.link_button("Open job posting", job.best_url(), use_container_width=False)

    if job.matched_skills:
        st.write("Matched skills:", ", ".join(job.matched_skills))

    contact_person = st.text_input("Contact person (optional)", value=job.contact_person)
    contact_email = st.text_input("Contact email (optional)", value=job.contact_email)
    notes = st.text_input("Notes (optional)", value=job.notes)

    c1, c2, c3 = st.columns(3)
    if c1.button("Prep with Playwright", type="primary", use_container_width=True):
        st.info(launch_playwright(job.id))
    if c2.button("I submitted — sync tracker", use_container_width=True):
        st.success(mark_submitted_and_sync(job.id, contact_person, contact_email, notes))
        st.rerun()
    if c3.button("Skip this role", use_container_width=True):
        update_match_status(job.id, fill_status=STATUS_SKIPPED)
        st.rerun()

    st.divider()
    st.caption("Full application queue")
    _show_table(_jobs_to_frame(queue))


def render_tracker() -> None:
    creds = service_account_path()
    st.write(f"Spreadsheet: {SHEET_URL}")
    st.caption(f"Service account file: `{creds}` — exists: {creds.is_file()}")
    st.link_button("Open Google Sheet", SHEET_URL)

    t1, t2 = st.columns(2)
    if t1.button("Test Google Sheets connection"):
        try:
            from tracker_sync import test_connection

            st.success(test_connection())
        except Exception as exc:
            st.error(str(exc))
    if t2.button("Retry unsynced applied jobs"):
        try:
            from tracker_sync import TrackerError, append_application

            due = [job for job in load_match_jobs() if job.fill_status == STATUS_APPLIED]
            ok = 0
            for job in due:
                try:
                    append_application(job)
                    ok += 1
                except TrackerError:
                    pass
            st.success(f"Synced {ok}/{len(due)} pending applied jobs.")
        except Exception as exc:
            st.error(str(exc))

    log_payload = load_tracker_log()
    rows = log_payload.get("rows") or []
    if not rows:
        st.info("No tracker rows yet. Confirm a submission after Playwright review.")
        return

    frame = pd.DataFrame(rows)
    keep = [
        col
        for col in [
            "ok",
            "date_applied",
            "company",
            "title",
            "location",
            "status",
            "contact_person",
            "contact_email",
            "job_link",
            "error",
            "synced_at",
        ]
        if col in frame.columns
    ]
    view = frame[keep].rename(
        columns={
            "ok": "OK",
            "date_applied": "Date applied",
            "company": "Company",
            "title": "Role",
            "location": "Location",
            "status": "Status",
            "contact_person": "Contact",
            "contact_email": "Contact email",
            "job_link": "Job link",
            "error": "Error",
            "synced_at": "Synced at",
        }
    )
    st.dataframe(
        view,
        use_container_width=True,
        hide_index=True,
        column_config={"Job link": st.column_config.LinkColumn("Job link", display_text="Open")},
        height=420,
    )


def main() -> None:
    st.set_page_config(
        page_title="Job Hunt Engine",
        page_icon="🎯",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    _inject_css()
    profile = load_profile()
    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    floor = int((profile.get("preferences") or {}).get("salary_floor") or 150000)

    st.title("Autonomous Job Hunt Engine")
    st.caption(
        f"{name} · New Grad / Full Stack SWE · ${floor:,.0f}+ floor · "
        f"{local_now().strftime('%A %I:%M %p %Z')}"
    )

    running = worker_is_running()
    today = jobs_found_on()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Monitor", "Active" if running else "Stopped")
    m2.metric("Jobs found today", len(today))
    m3.metric("Apps prepped", count_by_status(STATUS_PREPPED, STATUS_APPLIED, STATUS_SYNCED))
    m4.metric("Sheets synced", count_by_status(STATUS_SYNCED))

    monitor, feed, queue, tracker = st.tabs(
        [
            "Live Monitor Status",
            "Discovered Jobs Feed",
            "Application Queue",
            "Tracker Sync Status",
        ]
    )
    with monitor:
        render_monitor()
    with feed:
        render_feed()
    with queue:
        render_queue()
    with tracker:
        render_tracker()

    st.divider()
    resume_ok = profile.get("_resume_exists")
    email = str(profile.get("email") or "")
    placeholder_email = "YOUR_EMAIL" in email or "example.com" in email
    warn_bits = []
    if not resume_ok:
        warn_bits.append("Place `Deethyas_Resume.pdf` in this folder.")
    if placeholder_email:
        warn_bits.append("Update `email` / `phone` / LinkedIn in profile.json.")
    if not env("SMTP_USER"):
        warn_bits.append("Set SMTP_USER and SMTP_PASSWORD in `.env` for the 9:00 PM email.")
    if not service_account_path().is_file():
        warn_bits.append("Add `credentials.json` and share the Google Sheet with the service account.")
    if warn_bits:
        st.warning("Setup remaining: " + " ".join(warn_bits))
