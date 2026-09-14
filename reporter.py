"""Daily email + Markdown report of jobs discovered across all boards."""

from __future__ import annotations

import argparse
import html
import logging
import smtplib
import ssl
import sys
from collections import Counter
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from zoneinfo import ZoneInfo

from common import (
    MATCHES_PATH,
    REPORTS_DIR,
    SEEN_JOBS_PATH,
    JobPosting,
    env,
    google_sheets_id,
    load_json,
    load_profile,
    load_tracker_log,
    local_today,
)

log = logging.getLogger("reporter")

LOCAL_TZ = ZoneInfo("America/Chicago")


def load_jobs_for_report(only_new: bool = True) -> list[JobPosting]:
    matches = load_json(MATCHES_PATH, {"jobs": []})
    queued = [JobPosting.from_dict(raw) for raw in matches.get("jobs") or []]
    if queued and only_new:
        generated_at = str(matches.get("generated_at") or "")
        today = datetime.now(LOCAL_TZ).date().isoformat()
        if generated_at.startswith(today) or generated_at[:10] == datetime.now().date().isoformat():
            new_ids = {job.id for job in queued[: int(matches.get("new_count") or len(queued))]}
            fresh = [job for job in queued if job.id in new_ids] or queued
            return sorted(fresh, key=lambda job: (-job.match_score, job.company.lower()))

    if queued and not only_new:
        return sorted(queued, key=lambda job: (-job.match_score, job.company.lower()))

    seen = load_json(SEEN_JOBS_PATH, {"jobs": {}})
    jobs = [JobPosting.from_dict(raw) for raw in (seen.get("jobs") or {}).values()]
    jobs.sort(key=lambda job: (-job.match_score, job.company.lower()))
    return jobs


def build_markdown(jobs: list[JobPosting], profile: dict) -> str:
    now = datetime.now(LOCAL_TZ).strftime("%A, %B %d, %Y at %I:%M %p %Z")
    name = f"{profile.get('first_name', '')} {profile.get('last_name', '')}".strip()
    floor = int((profile.get("preferences") or {}).get("salary_floor") or 100000)
    preferred = int((profile.get("preferences") or {}).get("preferred_salary") or 150000)
    sources = Counter(job.source for job in jobs)

    lines = [
        f"# Job monitor report — {now}",
        "",
        f"Candidate: **{name}**  ",
        f"Target: New Grad / Full Stack Software Engineer  ",
        f"Compensation floor: **${floor:,.0f}+** (prefer **${preferred:,.0f}**)  ",
        f"Matches in this report: **{len(jobs)}**",
        "",
        "## Boards",
        "",
    ]
    if sources:
        for source, count in sources.most_common():
            lines.append(f"- `{source}`: {count}")
    else:
        lines.append("- No matching jobs in this window.")

    lines += ["", "## Matches", ""]
    if not jobs:
        lines.append("_The aggregator found no new $100k+ New Grad / Full Stack roles since the last run._")
        lines.append("")
        return "\n".join(lines)

    lines += [
        "| Score | Salary | Source | Company | Title | Location | Apply |",
        "| ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for job in jobs:
        title = job.title.replace("|", "/")
        company = job.company.replace("|", "/")
        location = (job.location or "—").replace("|", "/")
        url = job.best_url()
        lines.append(
            f"| {job.match_score:.1f} | {job.salary_label()} | {job.source} | "
            f"{company} | {title} | {location} | [open]({url}) |"
        )

    lines += ["", "## Details", ""]
    for job in jobs:
        skills = ", ".join(job.matched_skills) or "—"
        lines += [
            f"### {job.company} — {job.title}",
            "",
            f"- Source: `{job.source}`",
            f"- Location: {job.location or '—'}",
            f"- Compensation: {job.salary_label()}",
            f"- Resume match: **{job.match_score:.1f}**",
            f"- Matched skills: {skills}",
            f"- Queue status: `{job.fill_status}`",
            f"- Link: {job.best_url()}",
            "",
        ]

    tracker = load_tracker_log()
    rows = tracker.get("rows") or []
    today = local_today()
    today_rows = [row for row in rows if str(row.get("date_applied") or "")[:10] == today]
    ok_today = sum(1 for row in today_rows if row.get("ok"))
    failed_today = [row for row in today_rows if not row.get("ok")]
    sheet_url = (
        f"https://docs.google.com/spreadsheets/d/{google_sheets_id()}/edit"
    )
    lines += [
        "## Google Sheets tracker",
        "",
        f"- Spreadsheet: [open tracker]({sheet_url})",
        f"- Rows synced (all time): **{sum(1 for row in rows if row.get('ok'))}**",
        f"- Rows synced today: **{ok_today}**",
        f"- Last log update: {tracker.get('updated_at') or 'never'}",
        "",
    ]
    if failed_today:
        lines.append("Failed syncs today:")
        lines.append("")
        for row in failed_today:
            lines.append(
                f"- {row.get('company')} — {row.get('title')}: {row.get('error') or 'unknown error'}"
            )
        lines.append("")
    elif not today_rows:
        lines.append("_No applications were approved for the tracker today._")
        lines.append("")

    lines += [
        "---",
        "",
        "This report is a digest of the hunt. Greenhouse, Lever, and Ashby "
        "matches open one at a time in Playwright so you can edit and Submit "
        "(`bot.py --auto-prep`). Workday and other login portals stay skipped. "
        "Review the Streamlit Application Queue (`streamlit run app.py`).",
        "",
    ]
    return "\n".join(lines)


def markdown_to_html(markdown_text: str) -> str:
    """Minimal Markdown-to-HTML for the email alternative part."""
    escaped = html.escape(markdown_text)
    chunks: list[str] = []
    in_table = False
    for line in escaped.splitlines():
        if line.startswith("|") and "|" in line[1:]:
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if all(set(cell) <= {"-", ":", " "} for cell in cells):
                continue
            if not in_table:
                chunks.append("<table border='1' cellpadding='6' cellspacing='0'>")
                in_table = True
                chunks.append("<tr>" + "".join(f"<th>{cell}</th>" for cell in cells) + "</tr>")
            else:
                chunks.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
            continue
        if in_table:
            chunks.append("</table>")
            in_table = False
        if line.startswith("# "):
            chunks.append(f"<h1>{line[2:]}</h1>")
        elif line.startswith("## "):
            chunks.append(f"<h2>{line[3:]}</h2>")
        elif line.startswith("### "):
            chunks.append(f"<h3>{line[4:]}</h3>")
        elif line.startswith("- "):
            chunks.append(f"<li>{line[2:]}</li>")
        elif line == "":
            chunks.append("<br>")
        else:
            chunks.append(f"<p>{line}</p>")
    if in_table:
        chunks.append("</table>")
    return (
        "<html><body style='font-family: Segoe UI, Arial, sans-serif; font-size: 14px;'>"
        + "\n".join(chunks)
        + "</body></html>"
    )


def write_report_file(markdown_text: str) -> Path:
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
    path = REPORTS_DIR / f"jobs-{stamp}.md"
    path.write_text(markdown_text, encoding="utf-8")
    return path


def send_email(subject: str, markdown_text: str, to_addr: str) -> None:
    host = env("SMTP_HOST", "smtp.gmail.com")
    port = int(env("SMTP_PORT", "587") or "587")
    user = env("SMTP_USER")
    password = env("SMTP_PASSWORD")
    from_addr = env("SMTP_FROM") or user

    if not user or not password or not to_addr:
        raise RuntimeError(
            "Email is not configured. Copy .env.example to .env and set "
            "SMTP_USER, SMTP_PASSWORD, and REPORT_TO (or profile.json email)."
        )
    if "YOUR_EMAIL" in to_addr or "example.com" in to_addr:
        raise RuntimeError("Refusing to email a placeholder address. Update profile.json or REPORT_TO.")

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = from_addr
    message["To"] = to_addr
    message.set_content(markdown_text)
    message.add_alternative(markdown_to_html(markdown_text), subtype="html")

    context = ssl.create_default_context()
    with smtplib.SMTP(host, port, timeout=30) as smtp:
        smtp.ehlo()
        smtp.starttls(context=context)
        smtp.login(user, password)
        smtp.send_message(message)
    log.info("Sent report to %s", to_addr)


def report(jobs: list[JobPosting] | None = None, send: bool = True) -> Path:
    profile = load_profile()
    jobs = jobs if jobs is not None else load_jobs_for_report(only_new=True)
    markdown_text = build_markdown(jobs, profile)
    path = write_report_file(markdown_text)
    log.info("Wrote %s", path)

    recipient = env("REPORT_TO") or str(profile.get("email") or "")
    if send:
        try:
            date_label = datetime.now(LOCAL_TZ).strftime("%Y-%m-%d")
            send_email(
                subject=f"Job monitor — {len(jobs)} matches — {date_label}",
                markdown_text=markdown_text,
                to_addr=recipient,
            )
        except Exception as exc:
            log.warning("Email not sent: %s", exc)
            print(f"[reporter] Saved {path}. Email skipped: {exc}")
    else:
        print(f"[reporter] Saved {path} (email disabled).")
    return path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Email a Markdown summary of discovered jobs.")
    parser.add_argument("--no-email", action="store_true", help="Write the Markdown file only.")
    parser.add_argument("--all-queued", action="store_true", help="Include the full pending apply queue.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    jobs = load_jobs_for_report(only_new=not args.all_queued)
    report(jobs, send=not args.no_email)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n[reporter] Interrupted.")
