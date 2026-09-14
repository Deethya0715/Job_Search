"""
Supervised job-application form filler.

Opens a listing from any board (Greenhouse, Lever, Ashby, or a generic ATS),
fills candidate fields from profile.json, uploads Deethyas_Resume.pdf, then
ALWAYS pauses so you can review the page. This script never clicks a final
Submit / Apply button.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Frame, Locator, Page, Playwright, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from common import (
    MATCHES_PATH,
    PROFILE_PATH,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PREPPED,
    STATUS_PREPPING,
    STATUS_SKIPPED,
    STATUS_SYNCED,
    JobPosting,
    bot_notify,
    clear_bot_lock,
    enqueue_for_prep,
    find_match,
    load_match_jobs,
    load_profile,
    resume_path_or_exit,
    take_prep_batch,
    update_match_status,
    utc_now,
    write_bot_lock,
)

Target = Page | Frame

DEFAULT_TIMEOUT_MS = 8_000

# Buttons that must never be clicked by this bot.
SUBMIT_TEXT_DENYLIST = (
    "submit application",
    "submit your application",
    "submit",
    "apply now",
    "send application",
    "complete application",
)

# Human-readable field aliases used for label / name / placeholder matching.
FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "first_name": ("first name", "given name", "firstname"),
    "last_name": ("last name", "family name", "surname", "lastname"),
    "full_name": ("full name", "your name", "legal name"),
    "email": ("email", "e-mail", "email address"),
    "phone": ("phone", "phone number", "mobile", "telephone", "tel"),
    "linkedin": ("linkedin", "linkedin url", "linkedin profile"),
    "github": ("github", "github url", "github profile", "portfolio", "website"),
    "school": ("school", "university", "college", "institution"),
    "degree": ("degree", "major"),
    "graduation_date": (
        "graduation",
        "graduation date",
        "grad date",
        "expected graduation",
    ),
    "skills": ("skills", "technical skills", "key skills"),
}

AUTH_YES_LABELS = (
    "authorized to work",
    "legally authorized",
    "eligible to work",
    "work authorization",
    "us citizen",
    "u.s. citizen",
    "united states citizen",
    "citizenship",
)

SPONSOR_NO_LABELS = (
    "sponsorship",
    "visa sponsorship",
    "require sponsorship",
    "require visa",
    "future require",
    "immigration sponsorship",
    "h-1b",
    "h1b",
)


# ---------------------------------------------------------------------------
# Profile + CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fill an application form from any board. Never auto-submits."
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Target job application URL (Greenhouse, Lever, Ashby, or generic ATS).",
    )
    parser.add_argument(
        "--profile",
        default=str(PROFILE_PATH),
        help="Path to profile.json (default: ./profile.json).",
    )
    parser.add_argument(
        "--from-matches",
        action="store_true",
        help="Open pending jobs from matches.json one at a time.",
    )
    parser.add_argument(
        "--auto-prep",
        action="store_true",
        help=(
            "Automatically fill queued $150k+ matches without confirmation prompts. "
            "Still pauses before Submit for human review."
        ),
    )
    parser.add_argument(
        "--job-id",
        default="",
        help="Open a single queued job from matches.json by id (used by the dashboard).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Max number of queued matches to open (0 = all pending).",
    )
    return parser.parse_args()


def detect_ats(url: str) -> str:
    host = urlparse(url).netloc.lower()
    path = urlparse(url).path.lower()
    if "greenhouse.io" in host or "greenhouse" in host:
        return "greenhouse"
    if "lever.co" in host or "lever" in host:
        return "lever"
    if "ashbyhq.com" in host or "ashby" in host:
        return "ashby"
    if "myworkdayjobs.com" in host or "workday" in host:
        return "workday"
    if "linkedin.com" in host:
        return "linkedin"
    if "indeed.com" in host:
        return "indeed"
    if "jobs" in path or "apply" in path:
        return "generic"
    return "generic"


# ---------------------------------------------------------------------------
# Low-level Playwright helpers
# ---------------------------------------------------------------------------

def application_roots(page: Page) -> list[Target]:
    """Main document plus Greenhouse/Lever/Ashby iframes (embedded boards)."""
    roots: list[Target] = [page]
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        host = (frame.url or "").lower()
        if any(
            token in host
            for token in ("greenhouse", "lever", "ashby", "boards.greenhouse")
        ):
            roots.append(frame)
    return roots


def visible_locator(root: Target | Locator, selector: str) -> Locator:
    return root.locator(selector).locator("visible=true")


def first_visible(locator: Locator) -> Locator | None:
    try:
        count = locator.count()
    except PlaywrightTimeoutError:
        return None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible():
                return candidate
        except PlaywrightTimeoutError:
            continue
    return None


def fill_if_empty(locator: Locator, value: str) -> bool:
    """Type into an input only when it is visible and currently blank."""
    if not value:
        return False
    try:
        if not locator.is_visible():
            return False
        current = (locator.input_value(timeout=1_500) or "").strip()
        if current:
            return False
        locator.scroll_into_view_if_needed()
        locator.fill(value)
        return True
    except Exception:
        return False


def try_fill_selectors(page: Target, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        target = first_visible(visible_locator(page, selector))
        if target and fill_if_empty(target, value):
            print(f"  filled via {selector}")
            return True
    return False


def attr_blob(locator: Locator) -> str:
    bits: list[str] = []
    for name in ("name", "id", "placeholder", "aria-label", "autocomplete"):
        try:
            value = locator.get_attribute(name) or ""
        except Exception:
            value = ""
        if value:
            bits.append(value)
    return " ".join(bits).lower()


def looks_like(text: str, aliases: tuple[str, ...]) -> bool:
    haystack = " ".join(text.lower().split())
    return any(alias in haystack for alias in aliases)


def associated_label_text(page: Target, control: Locator) -> str:
    """Best-effort label text for an input (for= id, wrapping label, or nearby)."""
    texts: list[str] = []
    try:
        control_id = control.get_attribute("id")
        if control_id:
            label = page.locator(f'label[for="{control_id}"]').first
            if label.count() and label.inner_text().strip():
                texts.append(label.inner_text())
    except Exception:
        pass

    try:
        wrapping = control.locator("xpath=ancestor::label[1]")
        if wrapping.count():
            texts.append(wrapping.inner_text())
    except Exception:
        pass

    try:
        nearby = control.locator(
            "xpath=ancestor::*[self::div or self::li or self::fieldset][1]"
            "//label[1]"
        )
        if nearby.count():
            texts.append(nearby.first.inner_text())
    except Exception:
        pass

    return " ".join(texts).lower()


def fill_by_aliases(page: Target, aliases: tuple[str, ...], value: str) -> bool:
    if not value:
        return False

    controls = page.locator(
        "input:not([type='hidden']):not([type='file']):not([type='submit']):not([type='button']), textarea"
    )
    try:
        total = controls.count()
    except PlaywrightTimeoutError:
        return False

    for index in range(total):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
        except Exception:
            continue

        blob = f"{attr_blob(control)} {associated_label_text(page, control)}"
        if looks_like(blob, aliases) and fill_if_empty(control, value):
            print(f"  filled field matching {aliases[0]!r}")
            return True
    return False


def upload_resume(page: Target, resume_path: str) -> bool:
    file_inputs = page.locator("input[type='file']")
    try:
        count = file_inputs.count()
    except PlaywrightTimeoutError:
        count = 0

    for index in range(count):
        file_input = file_inputs.nth(index)
        try:
            blob = attr_blob(file_input)
            label = associated_label_text(page, file_input)
            combined = f"{blob} {label}"
            # Prefer resume/CV inputs; skip cover-letter-only pickers.
            if "cover" in combined and "resume" not in combined and "cv" not in combined:
                continue
            file_input.set_input_files(resume_path)
            print(f"  uploaded resume -> {resume_path}")
            return True
        except Exception:
            continue

    # Some Greenhouse boards hide the real file input behind an Attach button.
    attach = first_visible(
        page.get_by_role("button", name=re.compile(r"attach|upload|resume|cv", re.I))
    )
    if attach:
        try:
            with page.expect_file_chooser(timeout=3_000) as chooser_info:
                attach.click()
            chooser_info.value.set_files(resume_path)
            print("  uploaded resume via file chooser")
            return True
        except Exception:
            pass

    return False


def select_native_option(select: Locator, wanted: tuple[str, ...]) -> bool:
    try:
        options = select.locator("option")
        for index in range(options.count()):
            option = options.nth(index)
            label = (option.inner_text() or "").strip()
            value = (option.get_attribute("value") or "").strip()
            combined = f"{label} {value}".lower()
            if any(token in combined for token in wanted):
                select.select_option(value=value or label)
                return True
    except Exception:
        return False
    return False


def click_matching_choice(container: Locator, wanted: tuple[str, ...]) -> bool:
    """Click a radio/checkbox/option whose label matches one of the tokens."""
    choices = container.locator("label, [role='radio'], [role='option'], option")
    try:
        total = choices.count()
    except Exception:
        return False

    for index in range(total):
        choice = choices.nth(index)
        try:
            text = (choice.inner_text() or "").strip().lower()
        except Exception:
            continue
        if any(deny in text for deny in SUBMIT_TEXT_DENYLIST):
            continue
        if any(token == text or token in text.split() for token in wanted):
            try:
                choice.click(timeout=2_000)
                return True
            except Exception:
                continue
    return False


def answer_yes_no_question(page: Target, question_aliases: tuple[str, ...], answer: str) -> int:
    """
    Answer yes/no style questions (native select, radio, or custom list).
    Returns how many questions were answered.
    """
    answered = 0
    wanted = ("yes", "true") if answer == "yes" else ("no", "false")

    # Native <select> controls whose surrounding text matches the question.
    selects = page.locator("select")
    try:
        select_count = selects.count()
    except PlaywrightTimeoutError:
        select_count = 0

    for index in range(select_count):
        select = selects.nth(index)
        try:
            if not select.is_visible():
                continue
            context = f"{attr_blob(select)} {associated_label_text(page, select)}"
            if looks_like(context, question_aliases) and select_native_option(select, wanted):
                answered += 1
        except Exception:
            continue

    # Fieldsets / question cards that contain radios or checkboxes.
    groups = page.locator("fieldset, .application-question, .question, li, div")
    try:
        group_count = min(groups.count(), 250)
    except PlaywrightTimeoutError:
        group_count = 0

    seen: set[str] = set()
    for index in range(group_count):
        group = groups.nth(index)
        try:
            if not group.is_visible():
                continue
            text = " ".join((group.inner_text() or "").split()).lower()
        except Exception:
            continue
        if len(text) < 12 or len(text) > 400:
            continue
        if not looks_like(text, question_aliases):
            continue
        fingerprint = text[:120]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        if click_matching_choice(group, wanted):
            answered += 1

    return answered


def dismiss_cookie_banners(page: Target) -> None:
    labels = ("Accept", "Accept all", "I agree", "Got it", "Close")
    for label in labels:
        button = first_visible(page.get_by_role("button", name=label, exact=False))
        if button:
            try:
                button.click(timeout=1_500)
                time.sleep(0.3)
                return
            except Exception:
                continue


def assert_no_submit_clicked() -> None:
    """Submit / Apply clicks are intentionally omitted from this script."""
    return


# ---------------------------------------------------------------------------
# ATS-specific fill strategies
# ---------------------------------------------------------------------------

def fill_common_identity(page: Target, profile: dict[str, Any]) -> None:
    first = profile.get("first_name", "")
    last = profile.get("last_name", "")
    full = f"{first} {last}".strip()
    email = profile.get("email", "")
    phone = profile.get("phone", "")
    linkedin = (profile.get("links") or {}).get("linkedin", "")
    github = (profile.get("links") or {}).get("github", "")

    try_fill_selectors(
        page,
        [
            "#first_name",
            "input[name='job_application[first_name]']",
            "input[name='first_name']",
            "input[autocomplete='given-name']",
            "input[placeholder*='First' i]",
        ],
        first,
    ) or fill_by_aliases(page, FIELD_ALIASES["first_name"], first)

    try_fill_selectors(
        page,
        [
            "#last_name",
            "input[name='job_application[last_name]']",
            "input[name='last_name']",
            "input[autocomplete='family-name']",
            "input[placeholder*='Last' i]",
        ],
        last,
    ) or fill_by_aliases(page, FIELD_ALIASES["last_name"], last)

    # Lever (and some Greenhouse embeds) use a single Name field.
    try_fill_selectors(
        page,
        [
            "input[name='name']",
            "input[autocomplete='name']",
            "input[placeholder='Name']",
        ],
        full,
    ) or fill_by_aliases(page, FIELD_ALIASES["full_name"], full)

    try_fill_selectors(
        page,
        [
            "#email",
            "input[name='job_application[email]']",
            "input[name='email']",
            "input[type='email']",
            "input[autocomplete='email']",
        ],
        email,
    ) or fill_by_aliases(page, FIELD_ALIASES["email"], email)

    try_fill_selectors(
        page,
        [
            "#phone",
            "input[name='job_application[phone]']",
            "input[name='phone']",
            "input[type='tel']",
            "input[autocomplete='tel']",
        ],
        phone,
    ) or fill_by_aliases(page, FIELD_ALIASES["phone"], phone)

    try_fill_selectors(
        page,
        [
            "input[name='urls[LinkedIn]']",
            "input[name*='linkedin' i]",
            "input[id*='linkedin' i]",
            "input[placeholder*='LinkedIn' i]",
        ],
        linkedin,
    ) or fill_by_aliases(page, FIELD_ALIASES["linkedin"], linkedin)

    try_fill_selectors(
        page,
        [
            "input[name='urls[GitHub]']",
            "input[name*='github' i]",
            "input[id*='github' i]",
            "input[placeholder*='GitHub' i]",
        ],
        github,
    ) or fill_by_aliases(page, FIELD_ALIASES["github"], github)


def fill_education(page: Target, profile: dict[str, Any]) -> None:
    fill_by_aliases(page, FIELD_ALIASES["school"], profile.get("school", ""))
    fill_by_aliases(page, FIELD_ALIASES["degree"], profile.get("degree", ""))
    fill_by_aliases(
        page, FIELD_ALIASES["graduation_date"], profile.get("graduation_date", "")
    )


def fill_skills(page: Target, profile: dict[str, Any]) -> None:
    skills = profile.get("skills") or []
    if not skills:
        return
    fill_by_aliases(page, FIELD_ALIASES["skills"], ", ".join(str(item) for item in skills))


def fill_work_authorization(page: Target, profile: dict[str, Any]) -> None:
    auth = profile.get("work_authorization") or {}
    authorized = bool(auth.get("authorized_to_work", True) or auth.get("us_citizen", True))
    needs_sponsor = bool(auth.get("requires_sponsorship", False))

    yes_hits = answer_yes_no_question(
        page, AUTH_YES_LABELS, "yes" if authorized else "no"
    )
    no_hits = answer_yes_no_question(
        page, SPONSOR_NO_LABELS, "yes" if needs_sponsor else "no"
    )
    print(f"  authorization matches: {yes_hits}, sponsorship matches: {no_hits}")


def fill_application(page: Page, profile: dict[str, Any]) -> None:
    """Fill the main document and any Greenhouse/Lever iframe."""
    roots = application_roots(page)
    print("[*] Filling identity fields...")
    for root in roots:
        fill_common_identity(root, profile)

    print("[*] Uploading resume...")
    uploaded = any(upload_resume(root, profile["_resume_abs"]) for root in roots)
    if not uploaded:
        print("  [warn] No resume file input found — upload it manually.")

    print("[*] Filling education fields (best effort)...")
    for root in roots:
        fill_education(root, profile)

    print("[*] Filling skills (best effort)...")
    for root in roots:
        fill_skills(root, profile)

    print("[*] Answering work-authorization questions (best effort)...")
    for root in roots:
        fill_work_authorization(root, profile)


def fill_greenhouse(page: Page, profile: dict[str, Any]) -> None:
    print("[*] Detected Greenhouse application.")
    fill_application(page, profile)


def fill_lever(page: Page, profile: dict[str, Any]) -> None:
    print("[*] Detected Lever application.")
    fill_application(page, profile)


def fill_ashby(page: Page, profile: dict[str, Any]) -> None:
    print("[*] Detected Ashby application.")
    fill_application(page, profile)


def fill_generic(page: Page, profile: dict[str, Any]) -> None:
    print("[*] Unknown ATS — using generic field matching.")
    fill_application(page, profile)


def warn_if_walled_garden(ats: str) -> None:
    if ats in {"linkedin", "indeed", "workday"}:
        print(
            f"[warn] {ats} often requires a login or extra widgets. "
            "Fill what you can, then complete the rest during the review pause."
        )


# ---------------------------------------------------------------------------
# Safety pause
# ---------------------------------------------------------------------------

def pause_for_manual_review(page: Page, job: JobPosting | None = None) -> str:
    """
    SAFETY LOCK: never click Submit. Pause for a human, then optionally
    sync to Google Sheets only after they confirm they submitted themselves.
    Returns fill_status: prepped | applied | pending.
    """
    assert_no_submit_clicked()
    company = job.company if job else "this listing"
    title = job.title if job else ""
    url = job.best_url() if job else (page.url or "")
    banner = "\n".join(
        [
            "",
            "=" * 72,
            "FORM READY FOR REVIEW — this bot does NOT submit applications.",
            "=" * 72,
            f"  Company : {company}",
            f"  Role    : {title or '—'}",
            f"  URL     : {url}",
            "",
            "The form fields have been filled. Do all of the following in the browser:",
            "  1. Read every auto-filled value (name, email, phone, links).",
            "  2. Confirm the resume upload and any authorization answers.",
            "  3. Complete remaining required questions yourself.",
            "  4. If everything looks correct, click Submit yourself.",
            "  5. If something looks wrong, edit or close the tab.",
            "",
            "This window stays open until you come back here and press Enter.",
            "You can also leave it prepped and bulk-approve later from the Application Queue.",
            "=" * 72,
            "",
        ]
    )
    bot_notify(banner)
    try:
        input(">>> Press Enter in this terminal when you are finished reviewing... ")
    except (EOFError, KeyboardInterrupt):
        bot_notify("[!] Interrupted or no TTY — closing the browser without submitting.")
        return STATUS_PREPPED

    submitted = False
    try:
        submitted = _confirm(
            "Did you click Submit yourself? Sync this application to Google Sheets? [y/N] ",
            default_yes=False,
        )
    except (EOFError, KeyboardInterrupt):
        submitted = False

    try:
        page.context.close()
    except Exception:
        pass

    if submitted and job is not None:
        _sync_after_approval(job)
        return "applied"
    bot_notify(
        f"[*] Left {company} — {title or 'listing'} in the Application Queue as ready for review."
    )
    return STATUS_PREPPED


def _sync_after_approval(job: JobPosting) -> None:
    """Called only after the human confirms they submitted the form."""
    job.fill_status = "applied"
    job.applied_at = utc_now()
    update_match_status(job.id, fill_status="applied", applied_at=job.applied_at)
    try:
        from tracker_sync import append_application

        append_application(job)
        print("[*] Google Sheets tracker updated.")
    except Exception as exc:
        print(f"[warn] Could not sync Google Sheets: {exc}")
        print("       Retry later with: python tracker_sync.py --job-id", job.id)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(
    playwright: Playwright,
    url: str,
    profile: dict[str, Any],
    job: JobPosting | None = None,
    *,
    release_lock: bool = True,
) -> str:
    ats = detect_ats(url)
    bot_notify(f"[*] Opening {url}")
    bot_notify(f"[*] ATS guess: {ats}")
    write_bot_lock(job.id if job else "")
    if job is not None:
        update_match_status(job.id, fill_status=STATUS_PREPPING)

    browser = playwright.chromium.launch(headless=False, slow_mo=80)
    context = browser.new_context(accept_downloads=False)
    page = context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT_MS)

    try:
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            print("[warn] Page stayed busy after load — continuing anyway.")

        dismiss_cookie_banners(page)
        time.sleep(0.8)

        handlers = {
            "greenhouse": fill_greenhouse,
            "lever": fill_lever,
            "ashby": fill_ashby,
            "workday": fill_generic,
            "linkedin": fill_generic,
            "indeed": fill_generic,
            "generic": fill_generic,
        }
        warn_if_walled_garden(ats)
        handlers.get(ats, fill_generic)(page, profile)
        if job is not None:
            update_match_status(
                job.id,
                fill_status=STATUS_PREPPED,
                prepped_at=utc_now(),
                sheet_error="",
            )
            bot_notify(
                f"[*] Form prepped for {job.company} — {job.title}. "
                "Pausing before Submit for your review."
            )
        status = pause_for_manual_review(page, job)
    finally:
        try:
            browser.close()
        except Exception:
            pass
        if release_lock:
            clear_bot_lock()
        else:
            write_bot_lock("")
    return status


def _confirm(prompt: str, default_yes: bool = True) -> bool:
    try:
        answer = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not answer:
        return default_yes
    return answer in {"y", "yes"}


def load_pending_matches() -> list[JobPosting]:
    return [
        job
        for job in load_match_jobs()
        if job.fill_status in {STATUS_PENDING, STATUS_PREPPING} and job.best_url()
    ]


def _announce_job(job: JobPosting, index: int, total: int) -> None:
    bot_notify(
        "\n".join(
            [
                "",
                "-" * 72,
                f"[{index}/{total}] {job.company} — {job.title}",
                f"    source : {job.source}",
                f"    score  : {job.match_score}",
                f"    salary : {job.salary_label()}",
                f"    url    : {job.best_url()}",
                "-" * 72,
            ]
        )
    )


def _prep_one(
    playwright: Playwright,
    profile: dict[str, Any],
    job: JobPosting,
    *,
    release_lock: bool,
) -> str:
    try:
        status = run(
            playwright,
            job.best_url(),
            profile,
            job=job,
            release_lock=release_lock,
        )
        if status != "applied":
            update_match_status(job.id, fill_status=status or STATUS_PREPPED)
        return status
    except Exception as exc:
        bot_notify(f"[warn] Could not fill {job.company} — {job.title}: {exc}")
        update_match_status(job.id, fill_status=STATUS_FAILED, sheet_error=str(exc))
        return STATUS_FAILED


def run_from_matches(playwright: Playwright, profile: dict[str, Any], limit: int) -> None:
    pending = load_pending_matches()
    if limit > 0:
        pending = pending[:limit]
    if not pending:
        sys.exit("[bot] No pending matches. Run python aggregator.py first.")

    bot_notify(f"[*] {len(pending)} pending match(es) in {MATCHES_PATH.name}")
    write_bot_lock()
    try:
        for index, job in enumerate(pending, start=1):
            _announce_job(job, index, len(pending))
            if not _confirm("Open and fill this application? [Y/n] "):
                update_match_status(job.id, fill_status=STATUS_SKIPPED)
                bot_notify("[*] Skipped (set fill_status back to pending to reopen).")
                continue
            _prep_one(playwright, profile, job, release_lock=False)
    finally:
        clear_bot_lock()


def run_job_id(playwright: Playwright, profile: dict[str, Any], job_id: str) -> None:
    job = find_match(job_id)
    if job is None or not job.best_url():
        sys.exit(f"[bot] Job {job_id} was not found or has no apply URL.")
    _announce_job(job, 1, 1)
    status = _prep_one(playwright, profile, job, release_lock=True)
    if status == STATUS_FAILED:
        raise RuntimeError(job.sheet_error or f"Could not fill job {job_id}")


def _jobs_from_ids(job_ids: list[str]) -> list[JobPosting]:
    jobs: list[JobPosting] = []
    seen: set[str] = set()
    skip_statuses = {STATUS_PREPPED, STATUS_APPLIED, STATUS_SYNCED, STATUS_SKIPPED}
    for job_id in job_ids:
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        job = find_match(job_id)
        if job is None or not job.best_url():
            bot_notify(f"[warn] Skipping {job_id}: missing listing or apply URL.")
            continue
        if job.fill_status in skip_statuses:
            bot_notify(
                f"[*] Skipping {job.company} — {job.title} (already {job.fill_status})."
            )
            continue
        jobs.append(job)
    return jobs


def run_auto_prep(
    playwright: Playwright,
    profile: dict[str, Any],
    limit: int,
    *,
    include_pending: bool = False,
) -> None:
    """
    Drain the automatic prep queue. Fill each form, never click Submit, pause
    for human review, then continue to the next queued $150k+ match.
    """
    write_bot_lock()
    processed = 0
    try:
        while True:
            batch_ids = take_prep_batch()
            if not batch_ids and include_pending and processed == 0:
                batch_ids = [job.id for job in load_pending_matches()]
                include_pending = False
            if not batch_ids:
                break

            jobs = _jobs_from_ids(batch_ids)
            leftover: list[str] = []
            for index, job in enumerate(jobs, start=1):
                if limit > 0 and processed >= limit:
                    leftover.extend(remaining.id for remaining in jobs[index - 1 :])
                    break
                _announce_job(job, processed + 1, processed + len(jobs) - index + 1)
                bot_notify(
                    f"[*] Auto-prepping {job.company} — {job.title}. "
                    "Submit stays with you."
                )
                _prep_one(playwright, profile, job, release_lock=False)
                processed += 1

            if leftover:
                enqueue_for_prep(leftover)
                bot_notify(
                    f"[bot] Reached --limit {limit}. {len(leftover)} job(s) stay queued."
                )
                break
    finally:
        clear_bot_lock()

    if processed == 0:
        sys.exit(
            "[bot] Auto-prep queue is empty. New $150k+ matches are queued automatically after a scrape."
        )
    bot_notify(
        f"[*] Auto-prep session complete. {processed} form(s) filled and left ready for review. "
        "Approve them from the Application Queue dashboard after you click Submit yourself."
    )


def main() -> None:
    args = parse_args()
    profile = load_profile(Path(args.profile))
    resume_path_or_exit(profile)
    email = profile.get("email", "")
    if "YOUR_EMAIL" in email or "example.com" in email:
        print("[warn] profile.json still has placeholder contact details. Update it first.")

    with sync_playwright() as playwright:
        if args.auto_prep:
            run_auto_prep(
                playwright,
                profile,
                args.limit,
                include_pending=args.from_matches,
            )
            return
        if args.job_id:
            run_job_id(playwright, profile, args.job_id.strip())
            return
        if args.from_matches:
            run_from_matches(playwright, profile, args.limit)
            return

        url = (args.url or "").strip() or input("Paste the job application URL: ").strip()
        if not url.startswith(("http://", "https://")):
            sys.exit("[error] Please provide a full http(s) job URL.")
        run(playwright, url, profile)


if __name__ == "__main__":
    main()
