"""
Job-application form filler.

Opens Greenhouse, Lever, and Ashby listings, fills fields from profile.json,
uploads Deethyas_Resume.pdf, Sem5_Transcript.pdf, and an AI cover letter when the form asks,
writes leftover answers with an LLM, and submits unless a CAPTCHA is on the page.
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
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

from ai_answers import (
    ai_answers_enabled,
    ai_cover_letters_enabled,
    answer_questions,
    cover_letter_pdf_path,
    generate_cover_letter,
)
from common import (
    ANSWERS_PATH,
    MATCHES_PATH,
    PROFILE_PATH,
    STATUS_APPLIED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_PREPPED,
    STATUS_PREPPING,
    STATUS_REVIEWING,
    STATUS_SKIPPED,
    STATUS_SYNCED,
    JobPosting,
    auto_submit_enabled,
    bot_notify,
    clear_bot_lock,
    clear_review_signal,
    enqueue_for_prep,
    find_match,
    is_automatable_apply_url,
    load_json,
    load_match_jobs,
    load_profile,
    load_review_signal,
    playwright_headless,
    resume_path_or_exit,
    take_prep_one,
    update_match_status,
    utc_now,
    write_bot_lock,
    write_review_signal,
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
    r"^(apply|apply now|apply for this job|apply to this job|start application|"
    r"begin application|i.?m interested)$",
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
    "how did you learn about",
    "where did you hear",
    "referral source",
    "source of hire",
)
HEAR_ABOUT_ANSWERS = (
    "google job search",
    "google",
    "indeed",
    "linkedin",
    "handshake",
    "university",
    "career site",
    "company website",
    "job board",
    "simplify",
    "other",
    "internet",
)
THANKS_HINTS = (
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "thanks for taking the time",
    "application received",
    "application has been submitted",
    "application submitted",
    "successfully submitted",
    "we received your application",
    "we've received your application",
    "we have received your application",
    "your application was sent",
    "your application has been received",
)
CONFIRMATION_URL_HINTS = (
    "confirmation",
    "application_confirmation",
    "thanks-for-applying",
    "thank-you",
)
CONFIRMATION_SELECTORS = (
    "#flash_notice",
    ".flash",
    "#application_confirmation",
    ".application--confirmation",
    "[data-provides='confirmation']",
)
CAPTCHA_FRAME_BITS = (
    "recaptcha",
    "hcaptcha",
    "h-captcha",
    "challenges.cloudflare.com",
    "turnstile",
    "arkoselabs",
    "funcaptcha",
    "geo.captcha-delivery.com",
)
CAPTCHA_SELECTORS = (
    "iframe[src*='recaptcha']",
    "iframe[title*='reCAPTCHA' i]",
    "iframe[src*='hcaptcha']",
    "iframe[src*='turnstile']",
    ".g-recaptcha",
    "#g-recaptcha",
    ".h-captcha",
    ".cf-turnstile",
    "#cf-challenge-running",
)
CAPTCHA_TEXT_HINTS = (
    "i'm not a robot",
    "im not a robot",
    "verify you are human",
    "complete the captcha",
    "please verify you are a human",
    "checking your browser before accessing",
    "select all images",
    "select all squares",
    "solve this puzzle",
)
AI_SKIP_FIELD_BITS = (
    "password",
    "captcha",
    "resume",
    "curriculum vitae",
    "dropbox",
    "choose file",
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
        "expected grad",
    ),
    "graduation_year": (
        "graduation year",
        "year of graduation",
        "grad year",
        "expected graduation year",
        "year you will graduate",
        "year you'll graduate",
    ),
    "graduation_month": (
        "graduation month",
        "month of graduation",
        "grad month",
        "expected graduation month",
        "month you will graduate",
    ),
    "age": (
        "age",
        "years of age",
        "how old are you",
        "your age",
        "current age",
    ),
    "current_employer": (
        "current company",
        "current employer",
        "current workplace",
        "present employer",
        "where are you working",
        "where do you work now",
        "who do you work for",
        "current organization",
        "currently employed at",
        "current place of employment",
    ),
    "skills": ("skills", "technical skills", "key skills"),
    "location": (
        "current location",
        "current city",
        "city of residence",
        "where are you located",
        "where are you based",
        "where do you live",
        "where do you currently live",
        "where do you reside",
        "your location",
        "your city",
        "home city",
        "hometown",
        "city and state",
        "city/state",
        "what city",
        "which city",
        "candidate location",
        "applicant location",
        "located in",
    ),
    "country": ("country", "country of residence", "country/region"),
    "country_code": (
        "country code",
        "dial code",
        "calling code",
        "phone country",
        "phone code",
        "tel-country-code",
        "country prefix",
        "country calling",
        "area code",
    ),
    "languages": (
        "language skills",
        "languages spoken",
        "spoken language",
        "languages you speak",
        "what languages",
        "which languages",
        "fluent language",
        "language(s)",
        "languages",
        "bilingual",
        "multilingual",
        "native language",
        "native tongue",
        "additional language",
        "second language",
        "other language",
        "language proficiency",
        "linguistic",
    ),
    "start_date": (
        "start date",
        "available to start",
        "when can you start",
        "earliest start",
        "when would you be available",
        "availability date",
        "date available",
    ),
    "pronouns": ("pronouns", "preferred pronouns", "personal pronouns"),
    "salary": (
        "salary expectation",
        "expected salary",
        "desired salary",
        "compensation expectation",
        "expected compensation",
        "desired compensation",
        "salary requirements",
        "compensation requirements",
        "desired pay",
        "expected pay",
    ),
    "years_of_experience": (
        "years of experience",
        "years of professional",
        "years of relevant",
        "total years of experience",
        "how many years",
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
    "need sponsorship",
    "now, or will you in the future",
    "extend or renew",
    "future require",
    "immigration sponsorship",
    "h-1b",
    "h1b",
    "work visa",
    "sponsor your",
)

PREVIOUS_EMPLOYEE_LABELS = (
    "previously employed",
    "former employee",
    "previously worked",
    "have you worked",
    "have you ever worked",
    "already work",
    "already worked",
    "worked at this company",
    "worked for this company",
    "worked for us",
    "current or former employee",
    "ever been employed",
    "prior employee",
    "ex-employee",
    "interned here",
    "interned with us",
)

YESNO_SKIP_BITS = (
    "export control",
    "protected individual",
    "security clearance",
    "clearance eligibility",
    "clearance level",
    "conflict of interest",
    "previously applied",
    "applied to a position",
    "1324b",
    "immigration and naturalization",
)


def _skip_generic_yesno(text: str) -> bool:
    blob = " ".join((text or "").lower().split())
    return any(bit in blob for bit in YESNO_SKIP_BITS)


AGE_18_YES_LABELS = (
    "at least 18",
    "18 years of age or older",
    "18 years or older",
    "over 18",
    "older than 18",
    "18 or older",
    "age of 18",
    "are you 18",
)

GENDER_LABELS = ("gender identity", "gender", "sex")
PRONOUN_CHOICE_LABELS = ("pronouns", "preferred pronouns")
VETERAN_LABELS = (
    "veteran status",
    "protected veteran",
    "veteran",
    "military status",
    "served in the military",
)
VETERAN_YESNO_LABELS = (
    "are you a veteran",
    "are you a protected veteran",
    "have you ever served",
    "are you a disabled veteran",
)
DISABILITY_LABELS = (
    "disability status",
    "voluntary self-identification of disability",
    "have a disability",
    "ofccp",
    "section 503",
    "disability",
)
RACE_LABELS = ("race/ethnicity", "race or ethnicity", "racial identity", "ethnicity", "race")
HISPANIC_LABELS = ("hispanic or latino", "hispanic/latino", "hispanic", "latino")
FEMALE_TOKENS = ("female", "woman")
PRONOUN_TOKENS = ("she/her", "she / her", "she, her")
NOT_VETERAN_TOKENS = (
    "i am not a protected veteran",
    "i am not a veteran",
    "not a protected veteran",
    "no, i am not a veteran",
    "i have never served",
)
DISABILITY_YES_TOKENS = (
    "yes, i have a disability, or have a history",
    "yes, i have a disability, or have had",
    "yes, i have a disability (or previously",
    "yes, i have a disability",
    "i have a disability, or have had",
    "i have a disability (or previously",
)
EEO_CHOICE_IDS = {
    "gender",
    "pronouns",
    "veteran",
    "disability",
    "race",
    "hispanic",
}


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
            "Automatically fill queued $150k+ matches one at a time. "
            "Each form stays open for you to edit and Submit before the next one opens."
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


def _looks_like_submit(candidate: Locator) -> bool:
    """True when a control would submit the application. Never click these while filling."""
    try:
        type_attr = (candidate.get_attribute("type") or "").lower()
        ident = " ".join(
            [
                candidate.get_attribute("id") or "",
                candidate.get_attribute("name") or "",
                candidate.get_attribute("value") or "",
                candidate.get_attribute("aria-label") or "",
                candidate.inner_text() or "",
            ]
        ).lower()
    except Exception:
        return False
    if type_attr == "submit":
        return True
    if "submit_app" in ident:
        return True
    if SUBMIT_PATTERN.search(ident):
        return True
    return any(deny in ident for deny in SUBMIT_TEXT_DENYLIST)


def click_role_button(page: Page, pattern: re.Pattern[str]) -> bool:
    for root in application_roots(page):
        for role in ("button", "link"):
            candidate = first_visible(root.get_by_role(role, name=pattern))
            if not candidate:
                continue
            if pattern is CONTINUE_PATTERN and _looks_like_submit(candidate):
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
    if not form_fields_present(page):
        for selector in (
            "#apply_button",
            "a#apply_button",
            "a[href*='apply']",
            "a.postings-btn",
            ".application-form a.button",
        ):
            candidate = first_visible(visible_locator(page, selector))
            if candidate and _click_locator(candidate):
                print(f"[*] Clicked Apply via {selector}.")
                time.sleep(0.8)
                break
    try:
        page.wait_for_selector(
            "iframe#grnhse_iframe, iframe[id*='grnhse'], iframe[src*='greenhouse'], "
            "iframe[src*='lever'], iframe[src*='ashby'], input[type='email'], "
            "#first_name, input[name='job_application[first_name]']",
            timeout=12_000,
        )
    except PlaywrightTimeoutError:
        pass
    for _ in range(10):
        if form_fields_present(page):
            return
        time.sleep(0.7)


def submission_looks_successful(page: Page) -> bool:
    """True on a confirmation page. Greenhouse says 'Thank you for applying', not 'thanks'."""
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if any(hint in url for hint in CONFIRMATION_URL_HINTS):
        return True

    for selector in CONFIRMATION_SELECTORS:
        try:
            node = page.locator(selector).first
            if node.count() and node.is_visible():
                notice = " ".join((node.inner_text(timeout=1_500) or "").lower().split())
                if notice and any(hint in notice for hint in THANKS_HINTS):
                    return True
        except Exception:
            continue

    try:
        text = " ".join((page.locator("body").inner_text(timeout=3_000) or "").lower().split())
    except Exception:
        text = ""
    return any(hint in text for hint in THANKS_HINTS)


def captcha_present(page: Page) -> bool:
    """True when a human must solve a visible CAPTCHA / bot-check before Submit.

    Ignores invisible reCAPTCHA v3 badges and hidden g-recaptcha-response fields.
    """
    frames: list[Target] = [page]
    try:
        frames.extend(page.frames)
    except Exception:
        pass
    for root in frames:
        try:
            url = (getattr(root, "url", "") or "").lower()
        except Exception:
            url = ""
        if any(
            bit in url
            for bit in (
                "challenges.cloudflare.com",
                "geo.captcha-delivery.com",
                "arkoselabs",
                "funcaptcha",
            )
        ):
            return True
        for selector in (
            "iframe[src*='recaptcha']",
            "iframe[src*='hcaptcha']",
            "iframe[src*='turnstile']",
            "iframe[src*='challenges.cloudflare']",
            ".h-captcha",
            ".cf-turnstile",
            "#cf-challenge-running",
        ):
            try:
                loc = root.locator(selector)
                total = min(loc.count(), 8)
            except Exception:
                total = 0
            for index in range(total):
                node = loc.nth(index)
                try:
                    if not node.is_visible():
                        continue
                    box = node.bounding_box()
                except Exception:
                    continue
                if box and box.get("width", 0) >= 160 and box.get("height", 0) >= 60:
                    return True
        try:
            html = (root.content() if hasattr(root, "content") else "") or ""
        except Exception:
            html = ""
        if "cf-challenge-running" in html.lower() or "cf-browser-verification" in html.lower():
            return True
    try:
        text = " ".join((page.locator("body").inner_text(timeout=2_000) or "").lower().split())
    except Exception:
        text = ""
    return any(hint in text for hint in CAPTCHA_TEXT_HINTS)


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
    """Click Continue/Next only. Never click a Submit control."""
    return click_role_button(page, CONTINUE_PATTERN)


def fill_self_identify(page: Page) -> int:
    """Fill EEO / self-ID from profile.json (female, not a veteran, disability have-or-had)."""
    profile = load_json(PROFILE_PATH, {})
    ident = profile.get("self_identification") or {}
    gender = str(ident.get("gender") or profile.get("gender") or "female").lower()
    veteran = bool(ident.get("veteran", False))
    disability = bool(ident.get("disability", True))
    race_mode = str(ident.get("race") or ident.get("ethnicity") or "decline").lower()

    gender_tokens = FEMALE_TOKENS if gender in {"female", "woman"} else (gender,)
    veteran_tokens = NOT_VETERAN_TOKENS if not veteran else (
        "i identify as one or more",
        "i am a protected veteran",
        "yes, i am a veteran",
    )
    if disability:
        disability_tokens = DISABILITY_YES_TOKENS
        disability_yn = "yes"
    else:
        disability_tokens = (
            "no, i don't have a disability",
            "no, i do not have a disability",
            "i don't have a disability",
            "i do not have a disability",
        )
        disability_yn = "no"
    race_tokens = DECLINE_EEO_TOKENS if race_mode in {"decline", "prefer not", ""} else (race_mode,)

    answered = 0
    for root in application_roots(page):
        answered += answer_choice_question(root, GENDER_LABELS, gender_tokens)
        answered += answer_choice_question(root, PRONOUN_CHOICE_LABELS, PRONOUN_TOKENS)
        answered += answer_yes_no_question(root, VETERAN_YESNO_LABELS, "yes" if veteran else "no")
        answered += answer_choice_question(root, VETERAN_LABELS, veteran_tokens)
        answered += answer_yes_no_question(root, DISABILITY_LABELS, disability_yn)
        answered += answer_choice_question(root, DISABILITY_LABELS, disability_tokens)
        answered += answer_choice_question(root, RACE_LABELS, race_tokens)
        answered += answer_choice_question(root, HISPANIC_LABELS, DECLINE_EEO_TOKENS)
        answered += _fill_named_eeo_selects(
            root, gender_tokens, veteran_tokens, disability_tokens, race_tokens
        )
    if answered:
        print(
            f"  filled {answered} self-identify answer(s) "
            f"(gender={gender}, veteran={'yes' if veteran else 'no'}, "
            f"disability={'yes/have-or-had' if disability else 'no'}; race={race_mode})."
        )
    return answered


def _fill_named_eeo_selects(
    root: Target,
    gender_tokens: tuple[str, ...],
    veteran_tokens: tuple[str, ...],
    disability_tokens: tuple[str, ...],
    race_tokens: tuple[str, ...],
) -> int:
    filled = 0
    pairs = (
        ("select#gender, select[name*='gender' i], select[id*='gender' i]", gender_tokens),
        (
            "select#veteran_status, select[name*='veteran' i], select[id*='veteran' i]",
            veteran_tokens,
        ),
        (
            "select#disability_status, select[name*='disability' i], select[id*='disability' i]",
            disability_tokens,
        ),
        (
            "select#race, select[name*='race' i], select[id*='race' i], "
            "select#ethnicity, select[name*='ethnicity' i]",
            race_tokens,
        ),
        (
            "select#hispanic_ethnicity, select[name*='hispanic' i], select[id*='hispanic' i]",
            DECLINE_EEO_TOKENS,
        ),
    )
    for selector, wanted in pairs:
        try:
            loc = root.locator(selector)
            total = min(loc.count(), 12)
        except Exception:
            continue
        for index in range(total):
            select = loc.nth(index)
            try:
                if not select.is_visible():
                    continue
                if select_native_option(select, wanted):
                    filled += 1
            except Exception:
                continue
    return filled


def decline_self_identify(page: Page) -> int:
    """Back-compat alias: we now answer self-ID instead of declining it."""
    return fill_self_identify(page)


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
        answered += answer_choice_question(root, HEAR_ABOUT_LABELS, HEAR_ABOUT_ANSWERS)
    if answered:
        print(f"  answered {answered} 'how did you hear' question(s).")
    return answered


def submit_filled_application(
    page: Page,
    profile: dict[str, Any] | None = None,
    job: JobPosting | None = None,
) -> str:
    """Click Continue/Next, then Submit. Returns submitted | captcha | failed."""
    for step in range(8):
        dismiss_cookie_banners(page)
        if captcha_present(page):
            print("[*] CAPTCHA appeared before Submit.")
            return "captcha"
        fill_self_identify(page)
        check_consent_boxes(page)
        answer_hear_about(page)
        if profile is not None:
            fill_remaining_with_ai(page, profile, job)

        if click_continue_control(page):
            print(f"  clicked Continue/Next (step {step + 1}).")
            try:
                page.wait_for_load_state("domcontentloaded", timeout=8_000)
            except PlaywrightTimeoutError:
                pass
            time.sleep(0.8)
            if captcha_present(page):
                return "captcha"
            if profile is not None:
                fill_application(page, profile, job)
                fill_remaining_with_ai(page, profile, job)
            continue

        if captcha_present(page):
            return "captcha"
        if not click_submit_control(page):
            return "failed"
        try:
            page.wait_for_load_state("networkidle", timeout=12_000)
        except PlaywrightTimeoutError:
            pass
        time.sleep(1.2)
        if captcha_present(page):
            print("[*] CAPTCHA appeared after Submit.")
            return "captcha"
        if submission_looks_successful(page):
            print("[*] Submission confirmed on the page.")
            return "submitted"
        invalid = visible_invalid_fields(page)
        if invalid:
            preview = "; ".join(invalid[:6])
            print(f"[warn] Submit clicked but required fields are still invalid: {preview}")
            if profile is not None:
                fill_remaining_with_ai(page, profile, job)
            if step < 3:
                continue
            return "failed"
        print("[*] Submit clicked; no confirmation text — treating as submitted.")
        return "submitted"
    print("[warn] Ran out of Continue/Submit steps without confirmation.")
    return "failed"


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


def _current_value(locator: Locator) -> str:
    try:
        return (locator.input_value(timeout=1_000) or "").strip()
    except Exception:
        try:
            return (locator.inner_text(timeout=800) or "").strip()
        except Exception:
            return ""


def fill_if_empty(locator: Locator, value: str) -> bool:
    """Type into an input only when it is visible and currently blank."""
    if not value:
        return False
    try:
        if not locator.is_visible():
            return False
    except Exception:
        return False
    if _current_value(locator):
        return False
    try:
        js_val = locator.evaluate("el => String(el.value || '').trim()")
        if js_val:
            return False
    except Exception:
        pass
    try:
        locator.scroll_into_view_if_needed()
    except Exception:
        pass
    try:
        locator.click(timeout=2_000)
    except Exception:
        pass
    try:
        locator.fill(value, timeout=3_000)
        return True
    except Exception:
        try:
            locator.press_sequentially(value, delay=12)
            return True
        except Exception:
            try:
                locator.evaluate(
                    """(el, v) => {
                        el.focus();
                        el.value = v;
                        el.dispatchEvent(new Event('input', { bubbles: true }));
                        el.dispatchEvent(new Event('change', { bubbles: true }));
                    }""",
                    value,
                )
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
        "input:not([type='hidden']):not([type='file']):not([type='submit']):not([type='button']), "
        "textarea, [contenteditable='true']"
    )
    try:
        total = min(controls.count(), 120)
    except PlaywrightTimeoutError:
        return False

    filled = False
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
            filled = True
    return filled


LOCATION_SKIP_BITS = (
    "relocat",
    "job location",
    "role location",
    "office location",
    "this job",
    "this role",
    "position location",
)


def candidate_location_values(profile: dict[str, Any]) -> tuple[str, str]:
    city = usable_profile_value(profile.get("city", "")) or "McKinney"
    location = (
        usable_profile_value(profile.get("location", "")) or f"{city}, TX, United States"
    )
    if "dallas" in city.lower():
        city = "McKinney"
    if "dallas" in location.lower():
        location = "McKinney, TX, United States"
    return city, location


def is_candidate_location_question(text: str) -> bool:
    blob = " ".join((text or "").lower().split())
    if not blob or any(bit in blob for bit in LOCATION_SKIP_BITS):
        return False
    if looks_like(blob, FIELD_ALIASES["location"]):
        return True
    if blob.rstrip(" *:") in {"location", "city"}:
        return True
    if blob.startswith("location") or blob.startswith("city"):
        return "job" not in blob and "role" not in blob
    return False


def fill_location_control(page: Target, control: Locator, city: str, location: str) -> bool:
    """Fill a location/city input, including typeaheads that need a list pick."""
    blob = f"{attr_blob(control)} {associated_label_text(page, control)}"
    short = city
    try:
        current = (control.input_value(timeout=1_000) or "").strip()
    except Exception:
        current = ""
    if current:
        return False
    try:
        control.scroll_into_view_if_needed()
        control.click(timeout=2_000)
        control.fill(short)
        time.sleep(0.45)
    except Exception:
        return fill_if_empty(control, location)

    option_pat = re.compile(rf"{re.escape(city)}", re.I)
    try:
        option = first_visible(page.get_by_role("option", name=option_pat))
        if option:
            option.click(timeout=2_000)
            print(f"  selected location option matching {city!r}")
            return True
    except Exception:
        pass
    try:
        suggestion = first_visible(
            page.locator("[role='option'], .select__option, li[class*='option']").filter(
                has_text=option_pat
            )
        )
        if suggestion:
            suggestion.click(timeout=2_000)
            print(f"  clicked location suggestion matching {city!r}")
            return True
    except Exception:
        pass
    print(f"  typed location {short!r} (left for you if the list did not open)")
    return True


def fill_candidate_location(page: Target, profile: dict[str, Any]) -> None:
    city, location = candidate_location_values(profile)
    filled = False

    selectors = (
        "input[autocomplete='address-level2']",
        "input[name*='location' i]",
        "input[id*='location' i]",
        "input[placeholder*='location' i]",
        "input[name*='city' i]",
        "input[id*='city' i]",
        "input[placeholder*='city' i]",
        "input[placeholder*='Current location' i]",
    )
    for selector in selectors:
        target = first_visible(visible_locator(page, selector))
        if not target:
            continue
        blob = f"{attr_blob(target)} {associated_label_text(page, target)}"
        if any(bit in blob for bit in LOCATION_SKIP_BITS):
            continue
        if fill_location_control(page, target, city, location):
            print(f"  filled location via {selector}")
            filled = True

    controls = page.locator(
        "input:not([type='hidden']):not([type='file']):not([type='submit']):not([type='button']), "
        "textarea, [role='combobox']"
    )
    try:
        total = min(controls.count(), 80)
    except PlaywrightTimeoutError:
        total = 0
    for index in range(total):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
        except Exception:
            continue
        blob = f"{attr_blob(control)} {associated_label_text(page, control)}"
        if not is_candidate_location_question(blob):
            continue
        if fill_location_control(page, control, city, location):
            print(f"  filled location question matching {blob[:60]!r}")
            filled = True

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
            if not is_candidate_location_question(context):
                continue
            if select_native_option(select, (city.lower(), "mckinney")):
                print("  selected location McKinney")
                filled = True
        except Exception:
            continue

    if not filled:
        fill_by_aliases(page, FIELD_ALIASES["location"], location)


FILE_KIND_TRANSCRIPT = "transcript"
FILE_KIND_COVER = "cover"
FILE_KIND_RESUME = "resume"
FILE_KIND_OTHER = "other"
_TRANSCRIPT_HINTS = (
    "transcript",
    "academic record",
    "grade report",
    "academic history",
    "unofficial grades",
)


def file_field_kind(combined: str) -> str:
    text = combined.lower()
    if any(hint in text for hint in _TRANSCRIPT_HINTS):
        return FILE_KIND_TRANSCRIPT
    if "cover letter" in text or (
        "cover" in text and "resume" not in text and not re.search(r"\bcv\b", text)
    ):
        return FILE_KIND_COVER
    if "resume" in text or "curriculum vita" in text or re.search(r"\bcv\b", text):
        return FILE_KIND_RESUME
    return FILE_KIND_OTHER


def _file_already_set(file_input: Locator) -> bool:
    try:
        return bool(file_input.evaluate("el => !!(el.files && el.files.length)"))
    except Exception:
        return False


def upload_document(page: Target, path: str, *, kind: str, label: str) -> bool:
    file_inputs = page.locator("input[type='file']")
    try:
        count = file_inputs.count()
    except PlaywrightTimeoutError:
        count = 0

    for index in range(count):
        file_input = file_inputs.nth(index)
        try:
            combined = f"{attr_blob(file_input)} {associated_label_text(page, file_input)}"
            field_kind = file_field_kind(combined)
            if kind == FILE_KIND_RESUME:
                if field_kind in {FILE_KIND_TRANSCRIPT, FILE_KIND_COVER}:
                    continue
            elif kind == FILE_KIND_TRANSCRIPT:
                if field_kind != FILE_KIND_TRANSCRIPT:
                    continue
            elif kind == FILE_KIND_COVER:
                if field_kind != FILE_KIND_COVER:
                    continue
            if _file_already_set(file_input):
                return True
            file_input.set_input_files(path)
            print(f"  uploaded {label} -> {path}")
            time.sleep(1.6)
            return True
        except Exception:
            continue

    if kind == FILE_KIND_TRANSCRIPT:
        button_pattern = r"transcript|academic record|grade report"
    elif kind == FILE_KIND_COVER:
        button_pattern = r"cover letter|coverletter"
    else:
        button_pattern = r"attach|upload|resume|\bcv\b"
    buttons = page.get_by_role("button", name=re.compile(button_pattern, re.I))
    try:
        btn_count = buttons.count()
    except Exception:
        btn_count = 0
    for index in range(btn_count):
        attach = buttons.nth(index)
        try:
            if not attach.is_visible():
                continue
            parent_text = ""
            try:
                parent = attach.locator(
                    "xpath=ancestor::*[self::div or self::li or self::fieldset][1]"
                )
                parent_text = (parent.inner_text() or "")[:240]
            except Exception:
                parent_text = ""
            combined = (
                f"{attr_blob(attach)} {attach.inner_text() or ''} {parent_text}"
            )
            field_kind = file_field_kind(combined)
            if kind == FILE_KIND_RESUME and field_kind in {
                FILE_KIND_TRANSCRIPT,
                FILE_KIND_COVER,
            }:
                continue
            if kind == FILE_KIND_TRANSCRIPT and field_kind != FILE_KIND_TRANSCRIPT:
                continue
            if kind == FILE_KIND_COVER and field_kind != FILE_KIND_COVER:
                continue
            with page.expect_file_chooser(timeout=3_000) as chooser_info:
                attach.click()
            chooser_info.value.set_files(path)
            print(f"  uploaded {label} via file chooser")
            time.sleep(1.6)
            return True
        except Exception:
            continue

    return False


def upload_resume(page: Target, resume_path: str) -> bool:
    return upload_document(page, resume_path, kind=FILE_KIND_RESUME, label="resume")


def upload_transcript(page: Target, transcript_path: str) -> bool:
    return upload_document(
        page, transcript_path, kind=FILE_KIND_TRANSCRIPT, label="transcript"
    )


def upload_cover_letter(page: Target, cover_path: str) -> bool:
    return upload_document(
        page, cover_path, kind=FILE_KIND_COVER, label="cover letter"
    )


def _looks_like_cover_letter_field(page: Target, control: Locator) -> bool:
    blob = f"{attr_blob(control)} {associated_label_text(page, control)}"
    try:
        parent = control.locator(
            "xpath=ancestor::*[self::div or self::li or self::fieldset or self::section][1]"
        )
        blob = f"{blob} {(parent.inner_text() or '')[:280]}"
    except Exception:
        pass
    lowered = blob.lower()
    return file_field_kind(lowered) == FILE_KIND_COVER or "cover letter" in lowered


def fill_cover_letter(
    page: Page, profile: dict[str, Any], job: JobPosting | None = None
) -> None:
    if not ai_cover_letters_enabled():
        return
    roots = application_roots(page)
    text_controls: list[Locator] = []
    wants_file = False
    for root in roots:
        controls = root.locator("textarea, input[type='text']")
        try:
            total = min(controls.count(), 40)
        except PlaywrightTimeoutError:
            total = 0
        for index in range(total):
            control = controls.nth(index)
            try:
                if control.is_visible() and _looks_like_cover_letter_field(root, control):
                    text_controls.append(control)
            except Exception:
                continue
        file_inputs = root.locator("input[type='file']")
        try:
            file_count = file_inputs.count()
        except PlaywrightTimeoutError:
            file_count = 0
        for index in range(file_count):
            file_input = file_inputs.nth(index)
            combined = (
                f"{attr_blob(file_input)} {associated_label_text(root, file_input)}"
            )
            if file_field_kind(combined) == FILE_KIND_COVER:
                wants_file = True
                break
        if not wants_file:
            attach = first_visible(
                root.get_by_role(
                    "button", name=re.compile(r"cover letter|coverletter", re.I)
                )
            )
            if attach:
                wants_file = True

    if not text_controls and not wants_file:
        print("  no cover letter field found (ok if the form did not ask).")
        return

    print("[*] Writing cover letter...")
    letter = generate_cover_letter(profile, job)
    if not letter:
        print("  [warn] Cover letter generator returned empty.")
        return
    filled = 0
    for control in text_controls:
        if fill_if_empty(control, letter):
            filled += 1
    if filled:
        print(f"  filled {filled} cover letter text field(s)")
    if wants_file:
        pdf_path = cover_letter_pdf_path(profile, job, letter)
        if not pdf_path:
            print("  [warn] Could not write cover letter PDF.")
            return
        uploaded = any(upload_cover_letter(root, pdf_path) for root in roots)
        if not uploaded:
            print("  [warn] Cover letter file input found but upload failed.")


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


def select_yes_no_option(select: Locator, answer: str) -> bool:
    """Pick Yes/No without matching the letters 'no' inside 'not' / 'know'."""
    target = "yes" if answer == "yes" else "no"
    try:
        options = select.locator("option")
        exact = None
        prefix = None
        for index in range(options.count()):
            option = options.nth(index)
            label = (option.inner_text() or "").strip()
            value = (option.get_attribute("value") or "").strip()
            text = (label or value).strip().lower().rstrip(".")
            if text in {target, f"{target}."} or text == ("true" if target == "yes" else "false"):
                try:
                    select.select_option(label=label)
                except Exception:
                    select.select_option(value=value or label)
                return True
            if text.startswith(f"{target},") or text.startswith(f"{target} "):
                prefix = prefix or (label, value)
        if prefix:
            label, value = prefix
            try:
                select.select_option(label=label)
            except Exception:
                select.select_option(value=value or label)
            return True
        return False
    except Exception:
        return False


def pick_dropdown_value(page: Target, control: Locator, tokens: tuple[str, ...]) -> bool:
    """Native <select> first, then click a custom Select… combobox."""
    try:
        tag = (control.evaluate("el => el.tagName") or "").lower()
    except Exception:
        tag = ""
    lowered = tuple(t.lower().strip() for t in tokens)
    yn_only = set(lowered) <= {"yes", "no", "true", "false"}
    if tag == "select":
        if yn_only:
            yn = "yes" if ("yes" in lowered or "true" in lowered) else "no"
            if select_yes_no_option(control, yn):
                return True
        elif select_native_option(control, tokens):
            return True
    try:
        control.scroll_into_view_if_needed()
        control.click(timeout=2_000)
        time.sleep(0.35)
    except Exception:
        return False
    for token in tokens:
        exact = re.compile(rf"^{re.escape(token)}$", re.I)
        contains = re.compile(re.escape(token), re.I)
        option = first_visible(page.get_by_role("option", name=exact))
        if not option:
            option = first_visible(page.get_by_role("option", name=contains))
        if not option:
            option = first_visible(page.get_by_text(exact))
        if not option:
            option = first_visible(page.get_by_text(contains))
        if option and not _looks_like_submit(option) and _click_locator(option):
            return True
    return False


def _choice_matches(text: str, wanted: tuple[str, ...]) -> bool:
    blob = " ".join((text or "").lower().split())
    if not blob:
        return False
    first = re.split(r"[^a-z0-9]+", blob, maxsplit=1)[0]
    for token in wanted:
        token = token.lower().strip()
        if not token:
            continue
        if blob == token or first == token:
            return True
        if blob.startswith(token + " ") or blob.startswith(token + ",") or blob.startswith(
            token + "."
        ):
            return True
        if len(token) > 4 and token in blob:
            return True
    return False


def click_matching_choice(container: Locator, wanted: tuple[str, ...]) -> bool:
    """Click a radio/checkbox/option whose label matches one of the tokens."""
    choices = container.locator(
        "label, [role='radio'], [role='option'], option, input[type='radio'] + span"
    )
    try:
        total = min(choices.count(), 80)
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
        if not _choice_matches(text, wanted):
            continue
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
    yn = "yes" if answer == "yes" else "no"

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
            context = _question_text_for_control(page, select)
            if _skip_generic_yesno(context):
                continue
            if looks_like(context, question_aliases) and select_yes_no_option(select, yn):
                answered += 1
        except Exception:
            continue

    combos = page.locator("[role='combobox'], button:has-text('Select')")
    try:
        combo_count = min(combos.count(), 40)
    except Exception:
        combo_count = 0
    for index in range(combo_count):
        combo = combos.nth(index)
        try:
            if not combo.is_visible():
                continue
            context = _question_text_for_control(page, combo)
            if _skip_generic_yesno(context):
                continue
            if not looks_like(context, question_aliases):
                continue
            if pick_dropdown_value(page, combo, (yn,)):
                answered += 1
        except Exception:
            continue

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
        if len(text) < 12 or len(text) > 1800:
            continue
        if _skip_generic_yesno(text):
            continue
        if not looks_like(text, question_aliases):
            continue
        fingerprint = text[:160]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        if click_matching_choice(group, wanted):
            answered += 1
            continue
        dropdown = first_visible(
            group.locator("select, [role='combobox'], button, [class*='select']")
        )
        if dropdown and pick_dropdown_value(page, dropdown, (yn,)):
            answered += 1

    return answered


def _wanted_is_yes_no(wanted: tuple[str, ...]) -> str | None:
    lowered = {t.lower().strip() for t in wanted}
    if lowered <= {"yes", "true"}:
        return "yes"
    if lowered <= {"no", "false"}:
        return "no"
    return None


def answer_choice_question(
    page: Target, question_aliases: tuple[str, ...], wanted: tuple[str, ...]
) -> int:
    """Answer a labeled question by matching option/radio/select text."""
    if not question_aliases or not wanted:
        return 0
    yn = _wanted_is_yes_no(wanted)
    if yn:
        return answer_yes_no_question(page, question_aliases, yn)

    answered = 0
    selects = page.locator("select")
    try:
        select_count = min(selects.count(), 80)
    except PlaywrightTimeoutError:
        select_count = 0
    for index in range(select_count):
        select = selects.nth(index)
        try:
            if not select.is_visible():
                continue
            context = _question_text_for_control(page, select)
            if looks_like(context, question_aliases) and select_native_option(select, wanted):
                answered += 1
        except Exception:
            continue

    combos = page.locator("[role='combobox'], button:has-text('Select')")
    try:
        combo_count = min(combos.count(), 40)
    except Exception:
        combo_count = 0
    for index in range(combo_count):
        combo = combos.nth(index)
        try:
            if not combo.is_visible():
                continue
            context = _question_text_for_control(page, combo)
            if not looks_like(context, question_aliases):
                continue
            if pick_dropdown_value(page, combo, wanted):
                answered += 1
        except Exception:
            continue

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
        if len(text) < 8 or len(text) > 1800:
            continue
        if not looks_like(text, question_aliases):
            continue
        fingerprint = text[:160]
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        if click_matching_choice(group, wanted):
            answered += 1
            continue
        dropdown = first_visible(
            group.locator("select, [role='combobox'], button, [class*='select']")
        )
        if dropdown and pick_dropdown_value(page, dropdown, wanted):
            answered += 1

    return answered


def dismiss_cookie_banners(page: Target) -> None:
    cookie_name = re.compile(r"^(accept( all)?|i agree|got it|close)$", re.I)
    button = first_visible(page.get_by_role("button", name=cookie_name))
    if not button or _looks_like_submit(button):
        return
    try:
        button.click(timeout=1_500)
        time.sleep(0.3)
    except Exception:
        return


def assert_no_submit_clicked() -> None:
    """Legacy hook kept so fill helpers never treat Submit as a yes/no answer."""
    return


# ---------------------------------------------------------------------------
# ATS-specific fill strategies
# ---------------------------------------------------------------------------

US_DIAL_CODE = "+1"


def _control_blob(page: Target, control: Locator) -> str:
    return f"{attr_blob(control)} {associated_label_text(page, control)}"


def select_us_country_option(select: Locator) -> bool:
    """Prefer 'United States (+1)' without matching +1242-style NANP codes."""
    try:
        options = select.locator("option")
        ranked: list[tuple[int, str, str]] = []
        for index in range(options.count()):
            option = options.nth(index)
            label = (option.inner_text() or "").strip()
            value = (option.get_attribute("value") or "").strip()
            combined = f"{label} {value}".lower()
            score = 0
            if "united states" in combined and "minor" not in combined:
                score = 3 if "+1" in combined else 2
            elif re.search(r"(^|[^0-9])\+1([^0-9]|$)", combined) and not re.search(
                r"\+1\d", combined
            ):
                score = 1
            if score:
                ranked.append((score, value, label))
        if not ranked:
            return False
        ranked.sort(key=lambda item: -item[0])
        value, label = ranked[0][1], ranked[0][2]
        try:
            select.select_option(value=value or label)
        except Exception:
            select.select_option(label=label)
        return True
    except Exception:
        return False


def _is_residence_country_question(text: str) -> bool:
    blob = " ".join((text or "").lower().split())
    return any(
        bit in blob
        for bit in ("residence", "residency", "citizenship", "nationality", "home country")
    )


def _is_phone_country_widget(text: str) -> bool:
    blob = " ".join((text or "").lower().split())
    if not blob or _is_residence_country_question(blob):
        return False
    if looks_like(blob, FIELD_ALIASES["country_code"]):
        return True
    if "country" in blob and any(
        bit in blob for bit in ("phone", "mobile", "tel", "dial", "calling", "prefix")
    ):
        return True
    if blob.rstrip(" *:") in {"country", "country code", "code"}:
        return True
    return False


def _already_has_us_dial(control: Locator) -> bool:
    bits = [_current_value(control)]
    try:
        bits.append(control.inner_text(timeout=800) or "")
    except Exception:
        pass
    try:
        bits.append(control.get_attribute("title") or "")
        bits.append(control.get_attribute("aria-label") or "")
        bits.append(control.get_attribute("value") or "")
    except Exception:
        pass
    hay = " ".join(bits).lower()
    if not hay.strip():
        return False
    if "united states" in hay or "+1" in hay:
        # Avoid matching +1242-style codes unless US is named.
        if "united states" in hay or "usa" in hay:
            return True
        return bool(re.search(r"(^|[^0-9])\+1([^0-9]|$)", hay))
    return False


def _pick_plus_one_option(page: Target) -> bool:
    patterns = (
        re.compile(r"united states.*\+1|\+1.*united states", re.I),
        re.compile(r"^\s*\+1\s*$"),
        re.compile(r"united states|usa", re.I),
    )
    for pattern in patterns:
        try:
            option = first_visible(page.get_by_role("option", name=pattern))
        except Exception:
            option = None
        if option and not _looks_like_submit(option) and _click_locator(option):
            return True
        try:
            option = first_visible(
                page.locator(
                    "[role='option'], li[class*='option'], .iti__country, "
                    ".select__option, [data-testid*='option']"
                ).filter(has_text=pattern)
            )
        except Exception:
            option = None
        if option and not _looks_like_submit(option) and _click_locator(option):
            return True
    return False


def press_plus_one_on_control(page: Target, control: Locator) -> bool:
    """Click a country/dial widget, type +1, and click the US option. Never press Enter."""
    try:
        if _already_has_us_dial(control) or _looks_like_submit(control):
            return False
        control.scroll_into_view_if_needed()
        control.click(timeout=2_500)
        time.sleep(0.25)
    except Exception:
        return False

    search = first_visible(page.locator(".iti__search-input"))
    typer = search
    if typer is None:
        own = attr_blob(control)
        try:
            tag = (control.evaluate("el => el.tagName") or "").lower()
        except Exception:
            tag = ""
        if tag == "input" and (
            looks_like(own, FIELD_ALIASES["country_code"]) or "country" in own
        ):
            typer = control
    if typer is not None:
        try:
            typer.press_sequentially(US_DIAL_CODE, delay=25)
        except Exception:
            try:
                typer.fill(US_DIAL_CODE)
            except Exception:
                typer = None
        time.sleep(0.35)
    if _pick_plus_one_option(page):
        print("  selected country +1")
        return True
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    if typer is not None:
        print("  typed +1 on country (did not press Enter)")
        return True
    return False


def fill_country_plus_one(page: Target, profile: dict[str, Any]) -> None:
    """
    Phone country-code widgets need +1, not the words 'United States'.
    Type +1 and pick the matching option. Never press Enter (that submits the form).
    Residence country dropdowns still select United States.
    """
    answered = 0

    selects = page.locator("select")
    try:
        select_count = min(selects.count(), 50)
    except PlaywrightTimeoutError:
        select_count = 0
    for index in range(select_count):
        select = selects.nth(index)
        try:
            if not select.is_visible():
                continue
            context = _control_blob(page, select)
            if not _is_phone_country_widget(context) and not looks_like(
                context, FIELD_ALIASES["country"]
            ):
                continue
            if select_us_country_option(select):
                print("  selected country +1 / United States")
                answered += 1
        except Exception:
            continue

    widgets = page.locator(
        "[role='combobox'], .iti__selected-flag, [class*='PhoneInputCountry'], "
        "[aria-label*='country' i], button[aria-label*='country' i], "
        "input[aria-label*='country' i], input[name*='country' i], input[id*='country' i]"
    )
    try:
        total = min(widgets.count(), 80)
    except PlaywrightTimeoutError:
        total = 0
    for index in range(total):
        control = widgets.nth(index)
        try:
            if not control.is_visible():
                continue
        except Exception:
            continue
        blob = attr_blob(control)
        label = associated_label_text(page, control)
        if len(label) > 60:
            label = label[:60]
        blob = f"{blob} {label}"
        try:
            tag = (control.evaluate("el => el.tagName") or "").lower()
            role = (control.get_attribute("role") or "").lower()
        except Exception:
            tag = ""
            role = ""
        if tag == "select":
            continue
        countryish = _is_phone_country_widget(blob) or looks_like(blob, FIELD_ALIASES["country"])
        flag_only = "iti__" in blob or "phoneinputcountry" in blob.replace(" ", "")
        if not countryish and not flag_only:
            continue
        if not flag_only and tag not in {"input", "button", "div", "span"} and role != "combobox":
            continue
        if _is_residence_country_question(blob) and tag in {"textarea"}:
            continue
        if _looks_like_submit(control):
            continue
        if press_plus_one_on_control(page, control):
            answered += 1

    if answered:
        print(f"  country/dial-code +1 applied on {answered} widget(s)")

    country = usable_profile_value(profile.get("country", "")) or "United States"
    fill_by_aliases(page, FIELD_ALIASES["country"], country)


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

    fill_candidate_location(page, profile)
    fill_country_plus_one(page, profile)
    pronouns = usable_profile_value(profile.get("pronouns", "")) or "she/her"
    fill_by_aliases(page, FIELD_ALIASES["pronouns"], pronouns)


def fill_education(page: Target, profile: dict[str, Any]) -> None:
    fill_by_aliases(page, FIELD_ALIASES["school"], profile.get("school", ""))
    fill_by_aliases(page, FIELD_ALIASES["degree"], profile.get("degree", ""))
    year = str(profile.get("graduation_year") or "2026")
    month = str(profile.get("graduation_month") or "December")
    month_num = str(profile.get("graduation_month_number") or "12")
    fill_by_aliases(page, FIELD_ALIASES["graduation_date"], f"{month} {year}")
    fill_by_aliases(page, FIELD_ALIASES["graduation_year"], year)
    fill_by_aliases(page, FIELD_ALIASES["graduation_month"], month)

    selects = page.locator("select")
    try:
        select_count = min(selects.count(), 80)
    except PlaywrightTimeoutError:
        select_count = 0
    for index in range(select_count):
        select = selects.nth(index)
        try:
            if not select.is_visible():
                continue
            context = _question_text_for_control(page, select)
            if looks_like(context, FIELD_ALIASES["graduation_year"]):
                if select_native_option(select, (year,)):
                    print(f"  selected graduation year {year}")
            elif looks_like(context, FIELD_ALIASES["graduation_month"]):
                if select_native_option(select, (month.lower(), month_num, "dec")):
                    print(f"  selected graduation month {month}")
            elif looks_like(context, FIELD_ALIASES["graduation_date"]):
                if select_native_option(select, (month.lower(), month_num, year, "dec")):
                    print("  selected graduation date")
        except Exception:
            continue


def fill_structured_facts(
    page: Target, profile: dict[str, Any], job: JobPosting | None = None
) -> None:
    """Age 18, current workplace N/A, prior employer only CBRE, 18+ = Yes."""
    age = str(profile.get("age") or "18")
    employer = usable_profile_value(profile.get("current_employer", "")) or "N/A"
    prior = [str(item).lower() for item in (profile.get("prior_employers") or ["CBRE"])]
    company = (job.company if job else "") or ""
    worked_here = any(name and name in company.lower() for name in prior)

    fill_by_aliases(page, FIELD_ALIASES["age"], age)
    fill_by_aliases(page, FIELD_ALIASES["current_employer"], employer)
    start = usable_profile_value(profile.get("graduation_date", "")) or "December 2026"
    fill_by_aliases(page, FIELD_ALIASES["start_date"], start)
    years = str(profile.get("years_of_experience") or "1")
    fill_by_aliases(page, FIELD_ALIASES["years_of_experience"], years)
    salary = str((profile.get("preferences") or {}).get("preferred_salary") or "150000")
    fill_by_aliases(page, FIELD_ALIASES["salary"], salary)

    age_hits = answer_yes_no_question(page, AGE_18_YES_LABELS, "yes")
    if age_hits:
        print(f"  answered {age_hits} 18+ question(s) Yes")

    prev_hits = answer_yes_no_question(
        page, PREVIOUS_EMPLOYEE_LABELS, "yes" if worked_here else "no"
    )
    if prev_hits:
        print(
            f"  previous-employee at {company or 'this company'}: "
            f"{'Yes (CBRE)' if worked_here else 'No'}"
        )

    # Custom Select… widgets for current employer / age that aren't native.
    controls = page.locator("select, [role='combobox'], input:not([type='hidden'])")
    try:
        total = min(controls.count(), 80)
    except PlaywrightTimeoutError:
        total = 0
    for index in range(total):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
        except Exception:
            continue
        blob = _question_text_for_control(page, control)
        try:
            tag = (control.evaluate("el => el.tagName") or "").lower()
        except Exception:
            tag = ""
        if looks_like(blob, FIELD_ALIASES["graduation_year"]) and tag == "select":
            select_native_option(control, ("2026",))
        elif looks_like(blob, FIELD_ALIASES["graduation_month"]) and tag == "select":
            select_native_option(control, ("december", "12", "dec"))
        elif looks_like(blob, FIELD_ALIASES["current_employer"]):
            if tag == "select":
                select_native_option(control, ("n/a", "na", "none", "not applicable"))
            else:
                fill_if_empty(control, employer)


def fill_skills(page: Target, profile: dict[str, Any]) -> None:
    skills = profile.get("skills") or []
    if not skills:
        return
    fill_by_aliases(page, FIELD_ALIASES["skills"], ", ".join(str(item) for item in skills))


def fill_languages(page: Target, profile: dict[str, Any]) -> None:
    languages = [str(item).strip() for item in (profile.get("languages") or []) if str(item).strip()]
    if not languages:
        languages = ["English", "Telugu"]
    if not any(item.lower() == "english" for item in languages):
        languages.insert(0, "English")
    if not any(item.lower() == "telugu" for item in languages):
        languages.append("Telugu")
    wanted = tuple(item.lower() for item in languages)
    value = ", ".join(languages)
    answered = 0

    skip_bits = (
        "programming",
        "coding",
        "python",
        "javascript",
        "form language",
        "page language",
        "this application",
        "preferred language for",
        "resume language",
    )

    def is_language_question(text: str) -> bool:
        blob = " ".join((text or "").lower().split())
        if not blob or any(bit in blob for bit in skip_bits):
            return False
        if looks_like(blob, FIELD_ALIASES["languages"]):
            return True
        return blob.rstrip(" *:") in {"language", "languages"}

    def slot_value(blob: str) -> str:
        lowered = blob.lower()
        if any(token in lowered for token in ("2", "second", "additional", "other", "another")):
            return languages[1] if len(languages) > 1 else languages[0]
        if any(token in lowered for token in ("1", "first", "primary", "native")):
            return languages[0]
        return value

    def pick_language_options(select: Locator) -> int:
        picked: list[str] = []
        try:
            options = select.locator("option")
            for index in range(options.count()):
                option = options.nth(index)
                label = (option.inner_text() or "").strip()
                option_value = (option.get_attribute("value") or "").strip()
                combined = f"{label} {option_value}".lower()
                if option_value.lower() in {"en", "eng", "en-us", "en_us", "te", "tel", "te-in", "te_in"}:
                    picked.append(option_value or label)
                    continue
                if any(lang in combined for lang in wanted):
                    picked.append(option_value or label)
        except Exception:
            return 0
        if not picked:
            return 0
        unique = list(dict.fromkeys(picked))
        try:
            if select.get_attribute("multiple") is not None:
                select.select_option(value=unique)
                return len(unique)
            select.select_option(value=unique[0])
            return 1
        except Exception:
            try:
                select.select_option(label=unique[0])
                return 1
            except Exception:
                return 0

    selects = page.locator("select")
    try:
        select_count = min(selects.count(), 80)
    except PlaywrightTimeoutError:
        select_count = 0
    for index in range(select_count):
        select = selects.nth(index)
        try:
            if not select.is_visible():
                continue
            context = f"{attr_blob(select)} {associated_label_text(page, select)}"
            if not is_language_question(context):
                continue
            hits = pick_language_options(select)
            if hits:
                print(f"  selected {hits} language option(s)")
                answered += hits
        except Exception:
            continue

    groups = page.locator(
        "fieldset, [role='group'], .field, .form-group, .application-question, .question"
    )
    try:
        group_count = min(groups.count(), 120)
    except PlaywrightTimeoutError:
        group_count = 0
    for index in range(group_count):
        group = groups.nth(index)
        try:
            text = " ".join((group.inner_text() or "").split()).lower()
        except Exception:
            continue
        if not is_language_question(text):
            continue
        for lang in wanted:
            if click_matching_choice(group, (lang,)):
                answered += 1
                print(f"  checked language {lang}")

    controls = page.locator(
        "input:not([type='hidden']):not([type='file']):not([type='submit']):not([type='button']):not([type='checkbox']):not([type='radio']), "
        "textarea, [role='combobox']"
    )
    try:
        total = min(controls.count(), 80)
    except PlaywrightTimeoutError:
        total = 0
    for index in range(total):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
        except Exception:
            continue
        blob = f"{attr_blob(control)} {associated_label_text(page, control)}"
        if not is_language_question(blob):
            continue
        typed = slot_value(blob)
        try:
            current = (control.input_value(timeout=1_000) or "").strip().lower()
        except Exception:
            current = ""
        if current:
            continue
        try:
            control.scroll_into_view_if_needed()
            control.click(timeout=2_000)
            role = (control.get_attribute("role") or "").lower()
            if "combobox" in blob or role == "combobox":
                for lang in languages:
                    control.fill(lang)
                    time.sleep(0.35)
                    option = first_visible(
                        page.get_by_role(
                            "option", name=re.compile(rf"{re.escape(lang)}", re.I)
                        )
                    )
                    if option:
                        option.click(timeout=2_000)
                        answered += 1
                        print(f"  selected language typeahead {lang}")
                    else:
                        print(f"  typed language {lang!r} — pick it in the list if needed")
            elif not current:
                control.fill(typed)
                print(f"  filled languages {typed!r}")
                answered += 1
        except Exception:
            if fill_if_empty(control, typed):
                answered += 1

    if answered == 0:
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
    yes_hits = answer_yes_no_question(
        page, AUTH_YES_LABELS, "yes" if authorized else "no"
    )
    no_hits = answer_yes_no_question(page, SPONSOR_NO_LABELS, "no")
    print(f"  authorization matches: {yes_hits}, sponsorship matches: {no_hits}")


def _question_text_for_control(page: Target, control: Locator) -> str:
    chunks = [associated_label_text(page, control), attr_blob(control)]
    try:
        ancestors = control.locator(
            "xpath=ancestor::*[self::div or self::li or self::fieldset or self::section][position()<=3]"
        )
        total = min(ancestors.count(), 3)
        for index in range(total):
            text = " ".join((ancestors.nth(index).inner_text() or "").split())
            if 24 < len(text) < 1400:
                chunks.append(text)
    except Exception:
        pass
    return " ".join(chunks).lower()


def _format_bank_answer(template: str, profile: dict[str, Any], job: JobPosting | None) -> str:
    company = job.company if job else "this team"
    title = job.title if job else "this role"
    return template.format(
        company=company,
        title=title,
        name=profile.get("full_name") or "Deethya Janjanam",
        school=profile.get("school") or "The University of Texas at Dallas",
    )


def _job_looks_like_fdse(job: JobPosting | None) -> bool:
    hay = " ".join(
        [
            (job.title if job else ""),
            (job.best_url() if job else ""),
        ]
    ).lower()
    return "forward deployed" in hay or "fdse" in hay


def fill_essay_answers(page: Target, profile: dict[str, Any], job: JobPosting | None = None) -> None:
    """Fill custom/essay questions from answers.json (Palantir-style prompts included)."""
    bank = load_json(ANSWERS_PATH, {})
    questions = bank.get("questions") or []
    checkboxes = bank.get("checkboxes") or []
    if not questions and not checkboxes:
        return

    filled_ids: set[str] = set()
    controls = page.locator("textarea, input[type='text'], input[type='number']")
    try:
        total = min(controls.count(), 80)
    except PlaywrightTimeoutError:
        total = 0

    for index in range(total):
        control = controls.nth(index)
        try:
            if not control.is_visible():
                continue
            current = (control.input_value(timeout=1_000) or "").strip()
            if current:
                continue
        except Exception:
            continue
        blob = _question_text_for_control(page, control)
        if len(blob) < 12:
            continue
        for item in questions:
            qid = str(item.get("id") or "")
            if qid in filled_ids:
                continue
            phrases = [str(p).lower() for p in (item.get("any") or []) if p]
            if not phrases or not any(phrase in blob for phrase in phrases):
                continue
            if qid == "fdse_vs_swe":
                template = (
                    item.get("answer_fdse") if _job_looks_like_fdse(job) else item.get("answer_swe")
                )
            else:
                template = item.get("answer")
            if not template:
                continue
            text = _format_bank_answer(str(template), profile, job)
            try:
                control.scroll_into_view_if_needed()
                control.fill(text)
                print(f"  filled essay '{qid}'")
                filled_ids.add(qid)
                break
            except Exception:
                continue

    for item in checkboxes:
        phrases = [str(p).lower() for p in (item.get("any") or []) if p]
        choose = tuple(str(c).lower() for c in (item.get("choose") or ["yes"]))
        groups = page.locator(
            "fieldset, [role='group'], .field, .form-group, .application-question, li, div"
        )
        try:
            group_count = min(groups.count(), 180)
        except PlaywrightTimeoutError:
            group_count = 0
        for index in range(group_count):
            group = groups.nth(index)
            try:
                if not group.is_visible():
                    continue
                text = " ".join((group.inner_text() or "").split()).lower()
            except Exception:
                continue
            if len(text) < 20 or len(text) > 600:
                continue
            if not any(phrase in text for phrase in phrases):
                continue
            if click_matching_choice(group, choose):
                print(f"  checked '{item.get('id')}'")
                break

    fill_choice_bank(page)

    if filled_ids:
        print(f"  essay bank filled {len(filled_ids)} question(s): {', '.join(sorted(filled_ids))}")


def fill_choice_bank(page: Target) -> int:
    """Fill common select/radio questions from answers.json (skips EEO; those use fill_self_identify)."""
    bank = load_json(ANSWERS_PATH, {})
    items = list(bank.get("choices") or [])
    answered = 0
    for item in items:
        qid = str(item.get("id") or "")
        if qid in EEO_CHOICE_IDS:
            continue
        phrases = tuple(str(p).lower() for p in (item.get("any") or []) if p)
        choose = tuple(str(c).lower() for c in (item.get("choose") or ["yes"]))
        if not phrases or not choose:
            continue
        hits = answer_choice_question(page, phrases, choose)
        if hits:
            answered += hits
            print(f"  answered common question '{qid}'")
    if answered:
        print(f"  common-question bank filled {answered} choice(s).")
    return answered


def fill_application(page: Page, profile: dict[str, Any], job: JobPosting | None = None) -> None:
    """Fill the main document and any Greenhouse/Lever iframe."""
    roots = application_roots(page)
    print("[*] Filling identity fields...")
    for root in roots:
        fill_common_identity(root, profile)

    print("[*] Uploading resume...")
    uploaded = any(upload_resume(root, profile["_resume_abs"]) for root in roots)
    if not uploaded:
        print("  [warn] No resume file input found — upload it manually.")

    transcript_path = str(profile.get("_transcript_abs") or "")
    if profile.get("_transcript_exists") and transcript_path:
        print("[*] Uploading transcript if the form asks...")
        transcript_uploaded = any(
            upload_transcript(root, transcript_path) for root in roots
        )
        if not transcript_uploaded:
            print("  no transcript file input found (ok if the form did not ask).")
    elif profile.get("transcript_path"):
        print("  [warn] Transcript path is set but the PDF was not found — skip upload.")

    print("[*] Cover letter (if the form asks)...")
    fill_cover_letter(page, profile, job)

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

    print("[*] Filling age / employer / graduation facts...")
    for root in roots:
        fill_structured_facts(root, profile, job)

    print("[*] Answering offer-deadline questions (best effort)...")
    for root in roots:
        fill_offer_deadlines(root, profile)

    print("[*] Filling essay / custom questions from answers.json...")
    for root in roots:
        fill_essay_answers(root, profile, job)

    print("[*] Filling self-identification / EEO...")
    fill_self_identify(page)

    if uploaded:
        print("[*] Waiting for resume parse — not touching filled fields again.")
        time.sleep(1.2)


def fill_greenhouse(page: Page, profile: dict[str, Any], job: JobPosting | None = None) -> None:
    print("[*] Detected Greenhouse application.")
    fill_application(page, profile, job)


def fill_lever(page: Page, profile: dict[str, Any], job: JobPosting | None = None) -> None:
    print("[*] Detected Lever application.")
    fill_application(page, profile, job)


def fill_ashby(page: Page, profile: dict[str, Any], job: JobPosting | None = None) -> None:
    print("[*] Detected Ashby application.")
    fill_application(page, profile, job)


def fill_generic(page: Page, profile: dict[str, Any], job: JobPosting | None = None) -> None:
    print("[*] Unknown ATS — using generic field matching.")
    fill_application(page, profile, job)


def warn_if_walled_garden(ats: str) -> None:
    if ats in {"linkedin", "indeed", "workday"}:
        print(
            f"[warn] {ats} often requires a login or extra widgets. "
            "This listing may be skipped if it is not a public Greenhouse/Lever/Ashby form."
        )


def _control_maxlength(control: Locator) -> int:
    try:
        raw = control.get_attribute("maxlength")
        return int(raw) if raw else 0
    except Exception:
        return 0


def _native_select_options(select: Locator) -> list[str]:
    labels: list[str] = []
    try:
        options = select.locator("option")
        total = min(options.count(), 40)
    except Exception:
        return labels
    for index in range(total):
        try:
            text = " ".join((options.nth(index).inner_text() or "").split())
        except Exception:
            continue
        lowered = text.lower()
        if not text or lowered in {"select", "select...", "please select", "choose", "-"}:
            continue
        labels.append(text)
    return labels


def _select_looks_empty(select: Locator) -> bool:
    try:
        value = (select.input_value() or "").strip()
    except Exception:
        value = ""
    try:
        label = select.evaluate(
            "el => (el.options && el.selectedIndex >= 0) ? (el.options[el.selectedIndex].text || '') : ''"
        )
        lowered = " ".join(str(label or "").split()).lower()
    except Exception:
        lowered = ""
    if lowered in {"", "select", "select...", "please select", "choose", "-"}:
        return True
    return not value


def collect_unanswered_fields(page: Page) -> list[dict[str, Any]]:
    """Visible empty text/select fields the answer bank did not cover."""
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for root in application_roots(page):
        controls = root.locator(
            "textarea, input[type='text'], input[type='number'], "
            "input:not([type]), select"
        )
        try:
            total = min(controls.count(), 80)
        except Exception:
            total = 0
        for index in range(total):
            if len(found) >= 20:
                return found
            control = controls.nth(index)
            try:
                if not control.is_visible():
                    continue
                type_attr = (control.get_attribute("type") or "").lower()
                tag = (control.evaluate("el => el.tagName") or "").lower()
            except Exception:
                continue
            if type_attr in {"hidden", "file", "submit", "button", "checkbox", "radio", "password"}:
                continue
            question = " ".join(_question_text_for_control(root, control).split())
            if len(question) < 8:
                continue
            lowered = question.lower()
            if any(bit in lowered for bit in AI_SKIP_FIELD_BITS):
                continue
            kind = "select" if tag == "select" else ("textarea" if tag == "textarea" else "text")
            options: list[str] = []
            if kind == "select":
                if not _select_looks_empty(control):
                    continue
                options = _native_select_options(control)
            else:
                try:
                    if (control.input_value(timeout=800) or "").strip():
                        continue
                except Exception:
                    continue
            fingerprint = f"{kind}:{lowered[:160]}"
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            found.append(
                {
                    "id": f"q{len(found)+1}",
                    "kind": kind,
                    "question": question[:900],
                    "options": options,
                    "maxlength": _control_maxlength(control),
                    "locator": control,
                    "root": root,
                }
            )
    return found


def _apply_ai_field(field: dict[str, Any], answer: dict[str, str]) -> bool:
    control: Locator = field["locator"]
    root: Target = field["root"]
    kind = str(field.get("kind") or "text")
    choice = (answer.get("choice") or "").strip()
    text = (answer.get("text") or "").strip()
    maxlength = int(field.get("maxlength") or 0)
    if maxlength and text and len(text) > maxlength:
        text = text[:maxlength]
    wanted = tuple(token.lower() for token in (choice, text) if token)
    if kind == "select" and wanted:
        if set(wanted) <= {"yes", "no", "true", "false"}:
            yn = "yes" if any(token in {"yes", "true"} for token in wanted) else "no"
            if select_yes_no_option(control, yn):
                return True
        if select_native_option(control, wanted):
            return True
        if pick_dropdown_value(root, control, wanted):
            return True
    if text and fill_if_empty(control, text):
        return True
    if choice:
        try:
            parent = control.locator(
                "xpath=ancestor::*[self::div or self::li or self::fieldset or self::section][1]"
            )
            if parent.count() and click_matching_choice(parent, (choice.lower(),)):
                return True
        except Exception:
            pass
    return False


def fill_remaining_with_ai(
    page: Page, profile: dict[str, Any], job: JobPosting | None = None
) -> int:
    if not ai_answers_enabled():
        return 0
    fields = collect_unanswered_fields(page)
    if not fields:
        return 0
    print(f"[*] Asking AI to write {len(fields)} leftover answer(s)...")
    payload = [
        {
            "id": field["id"],
            "kind": field["kind"],
            "question": field["question"],
            "options": field["options"],
            "maxlength": field["maxlength"],
        }
        for field in fields
    ]
    answers = answer_questions(payload, profile, job)
    filled = 0
    for field in fields:
        answer = answers.get(field["id"]) or {}
        if not (answer.get("text") or answer.get("choice")):
            continue
        try:
            if _apply_ai_field(field, answer):
                filled += 1
                preview = (answer.get("choice") or answer.get("text") or "")[:48]
                print(f"  AI filled {field['id']}: {preview}")
        except Exception:
            continue
    if filled:
        print(f"  AI filled {filled} leftover field(s).")
    return filled


def fill_all_form_pages(
    page: Page, profile: dict[str, Any], job: JobPosting | None = None
) -> None:
    """Fill visible fields from the profile, answer bank, then AI. Never Submit."""
    dismiss_cookie_banners(page)
    fill_application(page, profile, job)
    fill_self_identify(page)
    check_consent_boxes(page)
    answer_hear_about(page)
    fill_remaining_with_ai(page, profile, job)
    if not form_fields_present(page):
        print("[warn] Could not find application fields.")
        return
    invalid = visible_invalid_fields(page)
    if invalid:
        preview = "; ".join(invalid[:6])
        print(f"[*] Remaining blank/invalid fields: {preview}")
        fill_remaining_with_ai(page, profile, job)
    print("[*] Fill pass finished.")


def _form_page_key(page: Page) -> str:
    """URL + visible field count so we can detect a new application step."""
    try:
        url = page.url or ""
    except Exception:
        url = ""
    counts: list[int] = []
    for root in application_roots(page):
        try:
            counts.append(
                root.locator(
                    "input:visible, textarea:visible, select:visible, [contenteditable='true']:visible"
                ).count()
            )
        except Exception:
            counts.append(0)
    return f"{url}|{counts}"


def _browser_alive(page: Page) -> bool:
    try:
        if page.is_closed():
            return False
        _ = page.url
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Safety pause
# ---------------------------------------------------------------------------

def wait_for_human_finish(
    page: Page,
    profile: dict[str, Any],
    job: JobPosting | None = None,
    *,
    reason: str = "",
) -> str:
    """Pause only when a human must act (CAPTCHA). Next app waits."""
    company = job.company if job else "this listing"
    title = job.title if job else ""
    url = job.best_url() if job else (page.url or "")
    job_id = job.id if job else ""
    captcha = reason == "captcha"
    heading = (
        "CAPTCHA — SOLVE IT IN CHROMIUM, THEN CLICK SUBMIT."
        if captcha
        else "FORM IS OPEN — EDIT ANYTHING YOU WANT. NEXT APP WAITS."
    )
    banner = "\n".join(
        [
            "",
            "=" * 72,
            heading,
            "=" * 72,
            f"  Company : {company}",
            f"  Role    : {title or '—'}",
            f"  URL     : {url}",
            "",
            "The next application will NOT open until this one is finished.",
            "In the Chromium window:",
            "  1. Complete the CAPTCHA / bot check." if captcha else "  1. Check auto-filled values.",
            "  2. Click Submit yourself. The bot will not click Submit on a CAPTCHA page."
            if captcha
            else "  2. Click Submit yourself when it looks right.",
            "",
            "When you are done:",
            "  • Wait until a thank-you page appears (Sheets updates automatically)",
            "  • Or click **I submitted** in the Application Queue dashboard",
            "=" * 72,
            "",
        ]
    )
    bot_notify(banner)
    if job is not None:
        update_match_status(
            job.id,
            fill_status=STATUS_REVIEWING,
            prepped_at=utc_now(),
            sheet_error="",
            notes="CAPTCHA — waiting for you to submit" if captcha else job.notes,
        )
    write_review_signal(
        job_id,
        "waiting",
        action="",
        company=company,
        title=title,
        url=url,
        reason=reason,
    )
    write_bot_lock(job_id)

    try:
        page.bring_to_front()
    except Exception:
        pass

    terminal_done = {"hit": False}

    def _stdin_wait() -> None:
        try:
            if not sys.stdin or not sys.stdin.isatty():
                return
            line = sys.stdin.readline()
            if line == "":
                return
            terminal_done["hit"] = True
        except (EOFError, KeyboardInterrupt, OSError):
            return

    try:
        stdin_interactive = bool(sys.stdin) and sys.stdin.isatty()
    except Exception:
        stdin_interactive = False
    if stdin_interactive:
        print(
            ">>> Press Enter to leave this as ready for review "
            "(does not mark submitted). Use the dashboard to mark submitted.",
            flush=True,
        )
        threading.Thread(target=_stdin_wait, daemon=True).start()

    last_url = ""
    try:
        last_url = page.url or ""
    except Exception:
        pass
    while True:
        if not _browser_alive(page):
            bot_notify("[*] Browser closed. This role stays in the queue as ready for review.")
            return STATUS_PREPPED

        if submission_looks_successful(page):
            bot_notify("[*] Thank-you page detected — treating this as submitted.")
            time.sleep(1.5)
            if job is not None:
                _sync_after_approval(
                    job,
                    notes="Submitted after CAPTCHA" if captcha else "Submitted",
                )
            return STATUS_APPLIED

        signal = load_review_signal()
        action = str(signal.get("action") or "").strip().lower()
        if action in {"submitted", "applied"}:
            if job is not None:
                _sync_after_approval(
                    job,
                    notes="Submitted after CAPTCHA" if captcha else "Submitted",
                )
            return STATUS_APPLIED
        if action == "skip":
            bot_notify(f"[*] Skipped {company} — {title or 'listing'} from the dashboard.")
            return STATUS_SKIPPED
        if action in {"next", "done", "prepped"}:
            bot_notify(
                f"[*] Left {company} — {title or 'listing'} in the queue as ready for review."
            )
            return STATUS_PREPPED

        if terminal_done["hit"]:
            if submission_looks_successful(page):
                if job is not None:
                    _sync_after_approval(
                        job,
                        notes="Submitted after CAPTCHA" if captcha else "Submitted",
                    )
                return STATUS_APPLIED
            bot_notify(
                f"[*] Left {company} — {title or 'listing'} in the queue as ready for review."
            )
            return STATUS_PREPPED

        try:
            current_url = page.url or ""
        except Exception:
            current_url = last_url
        if current_url != last_url and not submission_looks_successful(page):
            last_url = current_url
            bot_notify(
                "[*] Page URL changed. Not auto-filling again — edit the new page yourself "
                "or click Submit when ready."
            )

        time.sleep(0.8)


def _sync_after_approval(job: JobPosting, notes: str = "") -> None:
    """Mark applied and append a Google Sheets tracker row."""
    if notes:
        job.notes = notes
    job.fill_status = "applied"
    job.applied_at = utc_now()
    update_match_status(
        job.id,
        fill_status="applied",
        applied_at=job.applied_at,
        notes=job.notes,
    )
    try:
        from tracker_sync import append_application

        append_application(job, notes=job.notes)
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
    launch_kwargs: dict[str, Any] = {"headless": headless, "slow_mo": 0}
    if not headless:
        launch_kwargs["args"] = ["--start-maximized"]
    browser = playwright.chromium.launch(**launch_kwargs)
    context = browser.new_context(
        accept_downloads=False,
        no_viewport=not headless,
    )
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

        warn_if_walled_garden(ats)
        print(f"[*] Filling {ats} application...")
        fill_all_form_pages(page, profile, job)

        if captcha_present(page):
            bot_notify(
                f"[*] CAPTCHA on {job.company if job else 'listing'} — "
                "solve it and click Submit. Everything else stays automatic."
            )
            status = wait_for_human_finish(page, profile, job, reason="captcha")
        elif auto_submit_enabled():
            bot_notify(
                f"[*] Auto-submitting {job.company if job else 'listing'} — "
                f"{job.title if job else ''} (no CAPTCHA)."
            )
            outcome = submit_filled_application(page, profile, job)
            if outcome == "captcha":
                bot_notify("[*] CAPTCHA appeared during submit. Waiting for you.")
                status = wait_for_human_finish(page, profile, job, reason="captcha")
            elif outcome == "submitted":
                if job is not None:
                    _sync_after_approval(job, notes="Auto-submitted")
                status = STATUS_APPLIED
            else:
                bot_notify(
                    "[warn] Auto-submit failed and there is no CAPTCHA. "
                    "Marking failed and moving to the next role."
                )
                if job is not None:
                    update_match_status(
                        job.id,
                        fill_status=STATUS_FAILED,
                        notes="Auto-submit could not confirm (no CAPTCHA).",
                    )
                status = STATUS_FAILED
        else:
            if job is not None:
                bot_notify(
                    f"[*] Form prepped for {job.company} — {job.title}. "
                    "AUTO_SUBMIT is off, so this window stays open."
                )
            status = wait_for_human_finish(page, profile, job)
    finally:
        try:
            browser.close()
        except Exception:
            pass
        clear_review_signal()
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
    skip_statuses = {
        STATUS_REVIEWING,
        STATUS_PREPPED,
        STATUS_APPLIED,
        STATUS_SYNCED,
        STATUS_SKIPPED,
        "filled",
    }
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
    Drain the automatic apply queue one listing at a time. Submits without
    waiting unless a CAPTCHA is on the page.
    """
    write_bot_lock()
    processed = 0
    seeded_pending = False
    try:
        while True:
            if include_pending and not seeded_pending:
                pending_ids = [job.id for job in load_pending_matches()]
                if pending_ids:
                    enqueue_for_prep(pending_ids)
                seeded_pending = True
                include_pending = False

            job_id = take_prep_one()
            if not job_id:
                break
            if limit > 0 and processed >= limit:
                enqueue_for_prep([job_id])
                bot_notify(
                    f"[bot] Reached --limit {limit}. Remaining jobs stay queued."
                )
                break

            jobs = _jobs_from_ids([job_id])
            if not jobs:
                continue
            job = jobs[0]
            _announce_job(job, processed + 1, processed + 1)
            bot_notify(
                f"[*] Opening {job.company} — {job.title}. "
                "Will auto-submit unless a CAPTCHA appears."
            )
            _prep_one(playwright, profile, job, release_lock=False)
            processed += 1
    finally:
        clear_review_signal()
        clear_bot_lock()

    if processed == 0:
        sys.exit(
            "[bot] Auto-prep queue is empty. New $150k+ matches are queued automatically after a scrape."
        )
    bot_notify(
        f"[*] Session complete. {processed} application(s) processed."
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
    if auto_submit_enabled() and not ai_answers_enabled():
        bot_notify(
            "[warn] No LLM API key in .env. Leftover essays use answers.json only. "
            "Set OPENAI_API_KEY (or ANTHROPIC_API_KEY / GEMINI_API_KEY) for AI answers."
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
