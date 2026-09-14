"""
Streamlit control plane for the job-hunting engine.

Tabs:
  1. Live Monitor Status  — start/stop the hourly worker
  2. Discovered Jobs Feed — scored listings from every board
  3. Application Queue    — auto-prepped roles waiting for bulk review
  4. Tracker Sync Status  — rows pushed to Google Sheets
"""

from __future__ import annotations

import traceback

import pandas as pd
import streamlit as st

try:
    from common import (
        BOT_LOG_PATH,
        STATUS_APPLIED,
        STATUS_FAILED,
        STATUS_PENDING,
        STATUS_PREPPED,
        STATUS_PREPPING,
        STATUS_SKIPPED,
        STATUS_SYNCED,
        WORKER_LOG_PATH,
        auto_submit_enabled,
        bot_is_running,
        bot_lock_info,
        count_by_status,
        env,
        find_match,
        google_sheets_id,
        jobs_found_on,
        load_match_jobs,
        load_prep_queue_ids,
        load_profile,
        load_seen_jobs,
        load_tracker_log,
        load_worker_status,
        local_now,
        request_auto_prep,
        service_account_path,
        status_label,
        update_match_status,
    )
except Exception as exc:
    st.set_page_config(page_title="Job Hunt Engine", layout="wide")
    st.error("Failed to import `common`. Streamlit Cloud was hiding this error:")
    st.code("".join(traceback.format_exception(exc)))
    st.stop()

SHEET_URL = f"https://docs.google.com/spreadsheets/d/{google_sheets_id()}/edit"


def _worker_controls():
    """Import worker lazily so the dashboard still boots on Streamlit Cloud."""
    from worker import start_worker_process, stop_worker_process, worker_is_running

    return start_worker_process, stop_worker_process, worker_is_running


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


def queue_for_playwright(job_ids: list[str]) -> str:
    ids = [job_id for job_id in job_ids if job_id]
    if not ids:
        return "Select at least one role first."
    return request_auto_prep(ids) or "Nothing new to prep — those roles are already filled or have no apply URL."


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
    try:
        start_worker_process, stop_worker_process, worker_is_running = _worker_controls()
        running = worker_is_running()
    except Exception as exc:
        st.error(f"Worker module could not load in this environment: {exc}")
        st.info("The queue and tracker still work. Run the monitor on your local machine.")
        return
    if status.get("state") == "running" and not running:
        status["state"] = "stopped"
        status["message"] = "Worker process is not running."

    pill = (
        '<span class="status-pill status-on">ACTIVE</span>'
        if running
        else '<span class="status-pill status-off">STOPPED</span>'
    )
    st.markdown(f"### Engine {pill}", unsafe_allow_html=True)
    st.caption(
        status.get("message")
        or "Scrapes hourly from 8:00 AM to 7:00 PM CT. Email digest at 9:00 PM CT."
    )

    left, right = st.columns([1, 2])
    with left:
        wanted = st.toggle("Run monitor", value=running)
        if wanted and not running:
            try:
                pid = start_worker_process(initial_scrape=True)
                st.success(
                    f"Worker started (pid {pid}). Scrapes hourly 8:00 AM–7:00 PM CT."
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Could not start worker: {exc}")
        if not wanted and running:
            stop_worker_process()
            st.info("Stop requested. Heartbeat will go idle.")
            st.rerun()

        scrape_now = st.button("Scrape once now", use_container_width=True)
        if scrape_now:
            try:
                with st.spinner("Querying every board…"):
                    from aggregator import discover

                    fresh = discover(load_profile())
                extra = (
                    f" Playwright will auto-apply {len(fresh)} new $150k+ Greenhouse/Lever/Ashby role(s)."
                    if fresh
                    else ""
                )
                st.success(f"Scrape finished. {len(fresh)} new matching role(s).{extra}")
                st.rerun()
            except Exception as exc:
                st.error(f"Scrape is not available here: {exc}")
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
                "Next scrape": status.get("next_scrape_at") or "—",
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
        if job.fill_status
        in {STATUS_PENDING, STATUS_PREPPING, STATUS_PREPPED, STATUS_APPLIED, STATUS_FAILED}
    ]
    queue.sort(key=lambda job: (-job.match_score, job.company.lower()))

    pending_n = sum(1 for job in queue if job.fill_status == STATUS_PENDING)
    prepping_n = sum(1 for job in queue if job.fill_status == STATUS_PREPPING)
    ready_n = sum(1 for job in queue if job.fill_status == STATUS_PREPPED)
    applied_n = sum(1 for job in queue if job.fill_status == STATUS_APPLIED)
    failed_n = sum(1 for job in queue if job.fill_status == STATUS_FAILED)
    waiting_ids = load_prep_queue_ids()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Queued for prep", pending_n)
    c2.metric("Prepping now", prepping_n)
    c3.metric("Ready for review", ready_n)
    c4.metric("Submitted / failed", f"{applied_n} / {failed_n}")

    if auto_submit_enabled():
        st.success(
            "AUTO_SUBMIT is on. Playwright will fill Greenhouse, Lever, and Ashby forms "
            "and click Submit. Company portals (Workday, TikTok, Google, etc.) stay skipped."
        )

    if bot_is_running():
        lock = bot_lock_info()
        current = find_match(str(lock.get("job_id") or ""))
        current_label = (
            f"{current.company} — {current.title}"
            if current
            else (lock.get("job_id") or "a listing")
        )
        if auto_submit_enabled():
            st.success(f"Playwright is applying to **{current_label}**.")
        else:
            st.success(
                f"Playwright is filling **{current_label}**. "
                "The form will pause before Submit — review that browser window, then approve here."
            )
    elif waiting_ids:
        action = "auto-apply" if auto_submit_enabled() else "automatic Playwright prep"
        st.info(f"{len(waiting_ids)} role(s) are queued for {action}.")
        button_label = (
            "Start auto-apply (fill + Submit)"
            if auto_submit_enabled()
            else "Start Playwright for queued roles"
        )
        if st.button(button_label, use_container_width=False):
            st.info(request_auto_prep() or "Queue is already empty.")
            st.rerun()

    if not queue:
        st.info(
            "Queue is empty. Start the monitor to discover $150k+ New Grad / Full Stack roles. "
            "New matches are auto-prepped in Playwright and show up here for bulk review."
        )
        st.subheader("Playwright log")
        st.code(_tail_log(BOT_LOG_PATH), language="text")
        return

    filters = {
        "All active": None,
        "Ready for review": {STATUS_PREPPED},
        "Queued / prepping": {STATUS_PENDING, STATUS_PREPPING},
        "Submitted (not synced)": {STATUS_APPLIED},
        "Prep failed": {STATUS_FAILED},
    }
    filter_label = st.selectbox("Show", list(filters))
    wanted = filters[filter_label]
    visible = [job for job in queue if wanted is None or job.fill_status in wanted]

    st.caption(
        "Greenhouse, Lever, and Ashby matches are auto-applied when AUTO_SUBMIT=1. "
        "Workday, TikTok, Amazon, Apple, Google, and similar portals are skipped "
        "(they need a login). Failed rows can be re-queued below."
    )

    rows = []
    for job in visible:
        rows.append(
            {
                "Select": False,
                "Status": status_label(job.fill_status),
                "Score": job.match_score,
                "Company": job.company,
                "Role": job.title,
                "Location": job.location or "—",
                "Salary": job.salary_label(),
                "Source": job.source,
                "Link": job.best_url(),
                "id": job.id,
                "_status": job.fill_status,
            }
        )
    frame = pd.DataFrame(rows)
    selected_ids: list[str] = []
    if frame.empty:
        st.info("No roles in this filter.")
    else:
        edited = st.data_editor(
            frame,
            use_container_width=True,
            hide_index=True,
            height=min(560, 52 + 36 * max(len(frame), 3)),
            column_order=[
                "Select",
                "Status",
                "Score",
                "Company",
                "Role",
                "Location",
                "Salary",
                "Source",
                "Link",
            ],
            column_config={
                "Select": st.column_config.CheckboxColumn("Select", default=False),
                "Link": st.column_config.LinkColumn("Job link", display_text="Open"),
                "Score": st.column_config.NumberColumn(format="%.1f"),
                "Status": st.column_config.TextColumn("Status", width="medium"),
            },
            disabled=[col for col in frame.columns if col != "Select"],
            key=f"application_queue_editor_{filter_label}_{len(visible)}",
        )
        selected_ids = [
            str(job_id)
            for job_id, chosen in zip(edited["id"], edited["Select"])
            if bool(chosen)
        ]
        st.caption(f"{len(selected_ids)} selected · {len(visible)} shown · {len(queue)} in queue")

    contact_person = st.text_input("Contact person (optional, applied to approvals)")
    contact_email = st.text_input("Contact email (optional, applied to approvals)")
    notes = st.text_input("Notes (optional, applied to approvals)")

    a1, a2, a3, a4 = st.columns(4)
    approve_selected = a1.button(
        "Approve selected & sync",
        type="primary",
        use_container_width=True,
        help="For leftovers you submitted yourself. Auto-submit already writes Sheets when it succeeds.",
    )
    approve_ready = a2.button(
        "Approve all ready for review",
        use_container_width=True,
    )
    skip_selected = a3.button("Skip selected", use_container_width=True)
    refresh = a4.button("Refresh queue", use_container_width=True)
    if refresh:
        st.rerun()

    if approve_ready:
        selected_ids = [job.id for job in queue if job.fill_status == STATUS_PREPPED]
        approve_selected = True

    if approve_selected:
        if not selected_ids:
            st.warning("Select at least one ready-for-review role, or use Approve all ready for review.")
        else:
            ok = 0
            skipped_unready = 0
            errors: list[str] = []
            for job_id in selected_ids:
                job = find_match(job_id)
                if job is None:
                    continue
                if job.fill_status not in {STATUS_PREPPED, STATUS_APPLIED}:
                    skipped_unready += 1
                    continue
                message = mark_submitted_and_sync(
                    job.id,
                    contact_person or job.contact_person,
                    contact_email or job.contact_email,
                    notes or job.notes,
                )
                if message.startswith("Synced"):
                    ok += 1
                else:
                    errors.append(message)
            if ok:
                st.success(f"Approved and synced {ok} application(s).")
            if skipped_unready:
                st.info(
                    f"Skipped {skipped_unready} selected row(s) that are not ready for review yet "
                    "(still queued, prepping, or failed)."
                )
            for err in errors[:6]:
                st.warning(err)
            if ok:
                st.rerun()

    if skip_selected:
        if not selected_ids:
            st.warning("Select at least one role to skip.")
        else:
            for job_id in selected_ids:
                update_match_status(job_id, fill_status=STATUS_SKIPPED)
            st.success(f"Skipped {len(selected_ids)} role(s).")
            st.rerun()

    with st.expander("Advanced: re-queue selected for Playwright"):
        st.caption(
            "Only needed for failed preps or leftover pending rows. "
            "New $150k+ matches are already auto-queued after each scrape."
        )
        if st.button("Send selected to Playwright", use_container_width=True):
            st.info(queue_for_playwright(selected_ids))

    st.subheader("Playwright log")
    st.code(_tail_log(BOT_LOG_PATH, lines=50), language="text")


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
        f"{local_now().strftime('%A %I:%M %p %Z')} · Playwright auto-applies Greenhouse/Lever/Ashby matches"
    )

    running = False
    try:
        _, _, worker_is_running = _worker_controls()
        running = worker_is_running()
    except Exception:
        running = False
    today = jobs_found_on()
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Monitor", "Active" if running else "Stopped")
    m2.metric("Jobs found today", len(today))
    m3.metric("Ready for review", count_by_status(STATUS_PREPPED))
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


if __name__ == "__main__":
    main()
