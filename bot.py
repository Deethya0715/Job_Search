"""
Job-application form filler.

Opens Greenhouse, Lever, and Ashby listings, fills fields from profile.json,
and uploads Deethyas_Resume.pdf. With AUTO_SUBMIT=1 it also clicks Submit.
With AUTO_SUBMIT=0 it still pauses before Submit for a human review.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

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
    auto_submit_enabled,
    bot_notify,
    clear_bot_lock,
    enqueue_for_prep,
    find_match,
    is_automatable_apply_url,
    load_match_jobs,
    load_profile,
    playwright_headless,
    resume_path_or_exit,
    take_prep_batch,
    update_match_status,
    utc_now,
    write_bot_lock,
)

Target = Page | Frame

DEFAULT_TIMEOUT_MS = 8_000

# Choice widgets that look like submit controls — never use them as yes/no answers.
SUBMIT_TEXT_DENYLIST = (
    "submit application",
    "submit your application",
    "submit",
    "apply now",
    "send application",
    "complete application",
)

APPLY_OPEN_PATTERN = re.compile(
    r"^(apply|apply now|apply for this job|start application|begin application|i.?m interested)$",
    re.I,
)
SUBMIT_PATTERN = re.compile(
    r"submit.{0,24}application|send.{0,24}application|complete application|^submit$",
    re.I,
)
CONTINUE_PATTERN = re.compile(
    r"^(continue|next|save and continue|review application)$",
    re.I,
)
CONSENT_LABELS = (
    "i agree",
    "i acknowledge",
    "i consent",
    "i certify",
    "i have read",
    "privacy policy",
    "terms and conditions",
    "candidate privacy",
    "true and complete",
    "accurate and complete",
    "gdpr",
)
DECLINE_EEO_TOKENS = (
    "decline to self-identify",
    "decline to self identify",
    "i don't wish to answer",
    "i do not wish to answer",
    "prefer not to say",
    "prefer not to answer",
    "i don't want to answer",
)
HEAR_ABOUT_LABELS = (
    "how did you hear",
    "how did you find",
    "how you heard",
    "referral source",
    "source of hire",
)
HEAR_ABOUT_ANSWERS = (
    "linkedin",
    "university",
    "career site",
    "company website",
    "job board",
    "simplify",
    "other",
    "internet",
)
THANKS_HINTS = (
    "thank you",
    "thanks for applying",
    "thanks for taking the time",
    "application received",
    "application has been submitted",
    "successfully submitted",
    "we received your application",
    "your application was sent",
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
    "location": (
        "current location",
        "current city",
        "where are you located",
        "your location",
        "city and state",
    ),
    "country": ("country", "country of residence", "country/region"),
    "languages": (
        "languages",
        "language(s)",
        "spoken language",
        "languages spoken",
    ),
}

OFFER_DEADLINE_LABELS = (
    "offer deadline",
    "upcoming offer",
    "anticipate any upcoming",
    "offer deadlines",
)

MONTH_INDEX = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
MONTH_RANGE_RE = re.compile(
    r"(january|february|march|april|may|june|july|august|september|october|november|december)"
    r"\s+\d{1,2}\s*[-–]\s*"
    r"(january|february|march|april|may|june|july|august|september|october|november|december)",
    re.I,
)

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
        description="Fill an application form from Greenhouse, Lever, or Ashby."
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
            "Submits when AUTO_SUBMIT=1; otherwise pauses before Submit."
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
    query = urlparse(url).query.lower()
    if "gh_jid=" in query or "greenhouse.io" in host or "greenhouse" in host:
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

def usable_profile_value(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if "your_" in lowered or "example.com" in lowered:
        return ""
    if text in {"000-000-0000", "0000000000"}:
        return ""
    return text


def form_fields_present(page: Page) -> bool:
    selectors = (
        "input[type='email']",
        "input[name='email']",
        "input[name='job_application[email]']",
        "#first_name",
        "input[name='first_name']",
        "input[type='file']",
        "input[autocomplete='email']",
    )
    for root in application_roots(page):
        for selector in selectors:
            locator = root.locator(selector)
            try:
                if locator.count() and locator.first.is_visible():
                    return True
            except Exception:
                continue
    return False


def click_role_button(page: Page, pattern: re.Pattern[str]) -> bool:
    for root in application_roots(page):
        for role in ("button", "link"):
            candidate = first_visible(root.get_by_role(role, name=pattern))
            if not candidate:
                continue
            try:
                candidate.scroll_into_view_if_needed(timeout=2_000)
            except Exception:
                pass
            try:
                candidate.click(timeout=3_000)
                time.sleep(0.8)
                return True
            except Exception:
                try:
                    candidate.click(timeout=3_000, force=True)
                    time.sleep(0.8)
                    return True
                except Exception:
                    continue
    return False


def open_application_form(page: Page) -> None:
    if form_fields_present(page):
        return
    if click_role_button(page, APPLY_OPEN_PATTERN):
        print("[*] Clicked Apply to open the form.")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=8_000)
        except PlaywrightTimeoutError:
            pass
        time.sleep(0.6)
    try:
        page.wait_for_selector(
            "iframe#grnhse_iframe, iframe[id*='grnhse'], iframe[src*='greenhouse'], "
            "iframe[src*='lever'], iframe[src*='ashby'], input[type='email'], "
            "#first_name, input[name='job_application[first_name]']",
            timeout=12_000,
        )
    except PlaywrightTimeoutError:
        pass
    for _ in range(8):
        if form_fields_present(page):
            return
        time.sleep(0.7)


def submission_looks_successful(page: Page) -> bool:
    try:
        text = " ".join((page.locator("body").inner_text(timeout=3_000) or "").lower().split())
    except Exception:
        text = ""
    if any(hint in text for hint in THANKS_HINTS):
        return True
    if visible_invalid_fields(page):
        return False
    return False


def visible_invalid_fields(page: Page) -> list[str]:
    labels: list[str] = []
    for root in application_roots(page):
        try:
            invalids = root.locator(":invalid")
            total = min(invalids.count(), 40)
        except Exception:
            continue
        for index in range(total):
            control = invalids.nth(index)
            try:
                if not control.is_visible():
                    continue
            except Exception:
                continue
            blob = " ".join(
                (associated_label_text(root, control) + " " + attr_blob(control)).split()
            )
            labels.append(blob[:120] or f"unnamed-field-{index}")
    return labels


def _click_locator(candidate: Locator) -> bool:
    try:
        candidate.scroll_into_view_if_needed(timeout=2_000)
    except Exception:
        pass
    try:
        candidate.click(timeout=3_000)
        time.sleep(0.8)
        return True
    except Exception:
        try:
            candidate.click(timeout=3_000, force=True)
            time.sleep(0.8)
            return True
        except Exception:
            return False


def click_submit_control(page: Page) -> bool:
    if click_role_button(page, SUBMIT_PATTERN):
        print("  clicked Submit (role).")
        return True
    selectors = (
        "#submit_app",
        "input#submit_app",
        "input[type='submit']",
        "button[type='submit']",
        ".template-btn-submit",
        "button.postings-btn",
    )
    for root in application_roots(page):
        for selector in selectors:
            candidate = first_visible(visible_locator(root, selector))
            if candidate and _click_locator(candidate):
                print(f"  clicked Submit via {selector}.")
                return True
        try:
            by_text = first_visible(root.get_by_text(SUBMIT_PATTERN))
        except Exception:
            by_text = None
        if by_text and _click_locator(by_text):
            print("  clicked Submit (text).")
            return True
    print("[warn] Could not find a Submit button.")
    return False


def click_continue_control(page: Page) -> bool:
    return click_role_button(page, CONTINUE_PATTERN)


def decline_self_identify(page: Page) -> int:
    answered = 0
    for root in application_roots(page):
        selects = root.locator("select")
        try:
            total = min(selects.count(), 80)
        except Exception:
            total = 0
        for index in range(total):
            select = selects.nth(index)
            try:
                if not select.is_visible():
                    continue
                context = f"{attr_blob(select)} {associated_label_text(root, select)}"
                if "gender" in context or "race" in context or "veteran" in context or "ethnicity" in context or "disability" in context or "hispanic" in context:
                    if select_native_option(select, DECLINE_EEO_TOKENS):
                        answered += 1
            except Exception:
                continue
        groups = root.locator("fieldset, [role='group'], .field, .form-group")
        try:
            group_count = min(groups.count(), 120)
        except Exception:
            group_count = 0
        for index in range(group_count):
            group = groups.nth(index)
            try:
                text = " ".join((group.inner_text() or "").split()).lower()
            except Exception:
                continue
            if not any(token in text for token in ("gender", "race", "veteran", "ethnicity", "disability", "hispanic")):
                continue
            if click_matching_choice(group, DECLINE_EEO_TOKENS):
                answered += 1
    if answered:
        print(f"  declined {answered} self-identify question(s).")
    return answered


def check_consent_boxes(page: Page) -> int:
    checked = 0
    skip_bits = ("disability", "veteran", "sponsorship", "gender", "race", "hispanic")
    for root in application_roots(page):
        boxes = root.locator("input[type='checkbox']")
        try:
            total = min(boxes.count(), 80)
        except Exception:
            total = 0
        for index in range(total):
            box = boxes.nth(index)
            try:
                if not box.is_visible() or box.is_checked():
                    continue
            except Exception:
                continue
            blob = f"{attr_blob(box)} {associated_label_text(root, box)}"
            if any(bit in blob for bit in skip_bits):
                continue
            required = False
            try:
                required = bool(box.get_attribute("required"))
            except Exception:
                required = False
            if not required and not looks_like(blob, CONSENT_LABELS):
                continue
            try:
                box.check(timeout=2_000)
                checked += 1
            except Exception:
                if _click_locator(box):
                    checked += 1
    if checked:
        print(f"  checked {checked} consent/certify box(es).")
    return checked


def answer_hear_about(page: Page) -> int:
    answered = 0
    for root in application_roots(page):
        selects = root.locator("select")
        try:
            total = min(selects.count(), 80)
        except Exception:
            total = 0
        for index in range(total):
            select = selects.nth(index)
            try:
                if not select.is_visible():
                    continue
                context = f"{attr_blob(select)} {associated_label_text(root, select)}"
                if looks_like(context, HEAR_ABOUT_LABELS) and select_native_option(
                    select, HEAR_ABOUT_ANSWERS
                ):
                    answered += 1
            except Exception:
                continue
    if answered:
        print(f"  answered {answered} 'how did you hear' question(s).")
    return answered


def submit_filled_application(page: Page, profile: dict[str, Any] | None = None) -> bool:
    """Click Continue/Next through multi-step ATS forms, then Submit."""
    for step in range(8):
        dismiss_cookie_banners(page)
        decline_self_identify(page)
        check_consent_boxes(page)
        answer_hear_about(page)

        if click_continue_control(page):
            print(f"  clicked Continue/Next (step {step + 1}).")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
            time.sleep(0.8)
            if profile is not None:
                fill_application(page, profile)
            continue

        if not click_submit_control(page):
            return False
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except PlaywrightTimeoutError:
            pass
        time.sleep(1.2)
        if submission_looks_successful(page):
            print("[*] Submission confirmed on the page.")
            return True
        invalid = visible_invalid_fields(page)
        if invalid:
            preview = "; ".join(invalid[:6])
            print(f"[warn] Submit clicked but required fields are still invalid: {preview}")
            if step < 2:
                continue
            return False
        print("[*] Submit clicked; no confirmation text — treating as submitted.")
        return True
    print("[warn] Ran out of Continue/Submit steps without confirmation.")
    return False


def application_roots(page: Page) -> list[Target]:
    """Main document plus Greenhouse/Lever/Ashby iframes (embedded boards)."""
    roots: list[Target] = [page]
    for frame in page.frames:
        if frame == page.main_frame:
            continue
        host = (frame.url or "").lower()
        name = (frame.name or "").lower()
        if any(
            token in host
            for token in ("greenhouse", "lever", "ashby", "boards.greenhouse")
        ) or any(token in name for token in ("grnhse", "greenhouse", "lever", "ashby")):
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
    groups = page.locator(
        "fieldset, [role='group'], .application-question, .question, "
        ".select__control, [data-testid*='question'], .field, .form-group, "
        ".application-field, li.question, [class*='application-form-field']"
    )
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
    """Legacy hook kept so fill helpers never treat Submit as a yes/no answer."""
    return


# ---------------------------------------------------------------------------
# ATS-specific fill strategies
# ---------------------------------------------------------------------------

def fill_common_identity(page: Target, profile: dict[str, Any]) -> None:
    first = usable_profile_value(profile.get("first_name", ""))
    last = usable_profile_value(profile.get("last_name", ""))
    full = usable_profile_value(profile.get("full_name", "")) or f"{first} {last}".strip()
    email = usable_profile_value(profile.get("email", ""))
    phone = usable_profile_value(profile.get("phone", ""))
    linkedin = usable_profile_value((profile.get("links") or {}).get("linkedin", ""))
    github = usable_profile_value((profile.get("links") or {}).get("github", ""))

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

    website = usable_profile_value((profile.get("links") or {}).get("website", "")) or github
    try_fill_selectors(
        page,
        [
            "input[name='urls[Portfolio]']",
            "input[name='urls[Website]']",
            "input[name*='website' i]",
            "input[name*='portfolio' i]",
            "input[placeholder*='Website' i]",
            "input[placeholder*='Portfolio' i]",
        ],
        website,
    )

    location = usable_profile_value(profile.get("location", ""))
    try_fill_selectors(
        page,
        [
            "input[name*='location' i]",
            "input[id*='location' i]",
            "input[placeholder*='location' i]",
            "input[placeholder*='city' i]",
            "input[autocomplete='address-level2']",
        ],
        location,
    ) or fill_by_aliases(page, FIELD_ALIASES["location"], location)

    country = usable_profile_value(profile.get("country", "")) or "United States"
    selects = page.locator("select")
    try:
        select_count = min(selects.count(), 40)
    except PlaywrightTimeoutError:
        select_count = 0
    for index in range(select_count):
        select = selects.nth(index)
        try:
            if not select.is_visible():
                continue
            context = f"{attr_blob(select)} {associated_label_text(page, select)}"
            if looks_like(context, FIELD_ALIASES["country"]) and select_native_option(
                select, ("united states", "usa", "us")
            ):
                print("  selected country United States")
                break
        except Exception:
            continue
    else:
        fill_by_aliases(page, FIELD_ALIASES["country"], country)


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


def fill_languages(page: Target, profile: dict[str, Any]) -> None:
    languages = profile.get("languages") or []
    if not languages:
        return
    value = ", ".join(str(item) for item in languages)
    fill_by_aliases(page, FIELD_ALIASES["languages"], value)


def _through_month_index(profile: dict[str, Any]) -> int:
    raw = str((profile.get("offer_deadlines") or {}).get("through_month") or "December")
    return MONTH_INDEX.get(raw.strip().lower(), 12)


def _window_is_through_month(label: str, through: int) -> bool:
    match = MONTH_RANGE_RE.search(label or "")
    if not match:
        return False
    start = MONTH_INDEX[match.group(1).lower()]
    # Recruiting calendars often wrap into January; "until December" excludes that.
    if start == 1 and through >= 8:
        return False
    return start <= through


def check_deadline_windows(page: Target, through: int) -> int:
    checked = 0
    labels = page.locator("label")
    try:
        total = min(labels.count(), 120)
    except Exception:
        return 0
    for index in range(total):
        label = labels.nth(index)
        try:
            if not label.is_visible():
                continue
            text = " ".join((label.inner_text() or "").split())
        except Exception:
            continue
        if not _window_is_through_month(text, through):
            continue
        try:
            for_id = label.get_attribute("for")
            control = page.locator(f"#{for_id}") if for_id else label
            if control.count() and control.first.is_checked():
                continue
            label.click(timeout=2_000)
            checked += 1
            print(f"  checked deadline window {text!r}")
        except Exception:
            continue
    return checked


def fill_offer_deadlines(page: Target, profile: dict[str, Any]) -> None:
    deadlines = profile.get("offer_deadlines") or {}
    if not deadlines:
        return
    has_upcoming = bool(deadlines.get("has_upcoming"))
    hits = answer_yes_no_question(
        page, OFFER_DEADLINE_LABELS, "yes" if has_upcoming else "no"
    )
    windows = 0
    if has_upcoming:
        windows = check_deadline_windows(page, _through_month_index(profile))
    print(f"  offer-deadline answers: {hits}, date windows checked: {windows}")


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

    print("[*] Filling languages (best effort)...")
    for root in roots:
        fill_languages(root, profile)

    print("[*] Answering work-authorization questions (best effort)...")
    for root in roots:
        fill_work_authorization(root, profile)

    print("[*] Answering offer-deadline questions (best effort)...")
    for root in roots:
        fill_offer_deadlines(root, profile)


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
    Pause for a human when AUTO_SUBMIT is off. If stdin is not a TTY (spawned
    from the worker), leave the form prepped and continue instead of hanging.
    """
    company = job.company if job else "this listing"
    title = job.title if job else ""
    url = job.best_url() if job else (page.url or "")
    banner = "\n".join(
        [
            "",
            "=" * 72,
            "FORM READY FOR REVIEW — AUTO_SUBMIT is off.",
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
    if not sys.stdin.isatty():
        bot_notify(
            "[!] No terminal attached — leaving this form prepped and continuing. "
            "Set AUTO_SUBMIT=1 to click Submit automatically."
        )
        return STATUS_PREPPED
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

    headless = playwright_headless()
    browser = playwright.chromium.launch(headless=headless, slow_mo=0)
    context = browser.new_context(accept_downloads=False)
    page = context.new_page()
    page.set_default_timeout(DEFAULT_TIMEOUT_MS)

    status = STATUS_FAILED
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45_000)
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeoutError:
            print("[warn] Page stayed busy after load — continuing anyway.")

        dismiss_cookie_banners(page)
        time.sleep(0.8)
        open_application_form(page)

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

        if auto_submit_enabled():
            bot_notify(
                f"[*] Submitting {job.company if job else 'listing'} — "
                f"{job.title if job else ''}."
            )
            submitted = submit_filled_application(page, profile)
            if submitted:
                if job is not None:
                    _sync_after_approval(job)
                return STATUS_APPLIED
            if job is not None:
                update_match_status(
                    job.id,
                    fill_status=STATUS_FAILED,
                    sheet_error="Filled the form but could not confirm Submit.",
                )
            bot_notify("[warn] Auto-submit did not confirm. Marked as failed.")
            return STATUS_FAILED

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
    skip_statuses = {STATUS_PREPPED, STATUS_APPLIED, STATUS_SYNCED, STATUS_SKIPPED, "filled"}
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
        if not is_automatable_apply_url(job.best_url()):
            bot_notify(
                f"[*] Skipping {job.company} — {job.title}: not a Greenhouse/Lever/Ashby form."
            )
            update_match_status(
                job.id,
                fill_status=STATUS_SKIPPED,
                notes="Skipped auto-apply: not a Greenhouse/Lever/Ashby form "
                "(company portal or login required).",
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
    Drain the automatic prep queue. Fill each Greenhouse/Lever/Ashby form.
    With AUTO_SUBMIT=1, click Submit and continue to the next role.
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
                    f"[*] Auto-applying {job.company} — {job.title}."
                    if auto_submit_enabled()
                    else f"[*] Auto-prepping {job.company} — {job.title}. Submit stays with you."
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
        f"[*] Auto-apply session complete. {processed} form(s) processed."
        if auto_submit_enabled()
        else (
            f"[*] Auto-prep session complete. {processed} form(s) filled and left ready for review. "
            "Approve them from the Application Queue dashboard after you click Submit yourself."
        )
    )


def main() -> None:
    args = parse_args()
    profile = load_profile(Path(args.profile))
    resume_path_or_exit(profile)
    email = profile.get("email", "")
    if "YOUR_EMAIL" in email or "example.com" in email:
        sys.exit(
            "[bot] profile.json still has a placeholder email. "
            "Set a real email (and phone) before auto-apply."
        )
    if auto_submit_enabled() and not usable_profile_value(profile.get("phone", "")):
        bot_notify(
            "[warn] profile.json phone is missing. Auto-submit will still run; "
            "forms that require a phone number will fail."
        )

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
