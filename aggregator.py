"""
Universal job-board aggregator.

Pulls Software Engineer New Grad / Full Stack listings from LinkedIn, Indeed,
Glassdoor, ZipRecruiter, Google Jobs (via python-jobspy), Greenhouse, Lever,
Workday (banks and corporates), USAJOBS (federal), an optional JSearch API,
and the public Simplify new-grad list.

Applies a strict U.S.-only location filter, a $100k compensation floor with a
$150k preference for ranking, scores each posting against profile.json, writes
new matches to matches.json, and queues those roles for Playwright.
"""

from __future__ import annotations

import argparse
import html
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Iterable

import requests

from common import (
    ATS_COMPANIES_PATH,
    DEFAULT_PREFERRED_SALARY,
    DEFAULT_SALARY_FLOOR,
    MATCHES_PATH,
    SEEN_JOBS_PATH,
    JobPosting,
    env,
    load_json,
    load_profile,
    make_job_id,
    request_auto_prep,
    save_json,
    tokenize,
    utc_now,
)

log = logging.getLogger("aggregator")


class _JobSpyNoiseFilter(logging.Filter):
    """Drop expected JobSpy board failures so scrapes stay clean."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = str(getattr(record, "name", "") or "")
        if not name.startswith("JobSpy"):
            return True
        try:
            msg = record.getMessage().lower()
        except Exception:
            msg = str(record.msg or "").lower()
        noisy = (
            "location not parsed" in msg
            or "status code 400" in msg
            or "status code 403" in msg
            or "forbidden aa" in msg
        )
        return not noisy


logging.getLogger().addFilter(_JobSpyNoiseFilter())
if logging.lastResort is not None:
    logging.lastResort.addFilter(_JobSpyNoiseFilter())

REQUEST_TIMEOUT = 20
USER_AGENT = (
    "Mozilla/5.0 (compatible; automated-job-monitor/1.0; "
    "+https://github.com/Deethya0715)"
)
SESSION = requests.Session()
SESSION.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

# Glassdoor 400s on "United States"; ZipRecruiter is Cloudflare-403. Skip both.
JOBSPY_SITES = ["indeed", "linkedin", "google"]

SIMPLIFY_LISTING_URLS = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/nightly/.github/scripts/listings.json",
)

SWE_TITLE_RE = re.compile(
    r"("
    r"\b(software|full[\s-]?stack|fullstack|front[\s-]?end|back[\s-]?end|"
    r"swe|sde|platform engineer|web engineer|computer scientist|"
    r"computer engineer|software developer|applications? developer|"
    r"software development engineer|technology analyst)\b"
    r"|it specialist.{0,48}(appsw|software|sysanalysis)"
    r")",
    re.I,
)
NEW_GRAD_RE = re.compile(
    r"(new[\s-]*grad(?:uate)?s?|university[\s-]*grad(?:uate)?s?|"
    r"college[\s-]*grad(?:uate)?s?|early[\s-]*career|entry[\s-]*level|"
    r"class of 202[5-7]|graduating|recent[\s-]*grad(?:uate)?s?|"
    r"associate software|junior|0\s*[-to]+\s*[12]\s+years?|"
    r"pathways|campus|university hire|analyst program|"
    r"development program|software engineer\s*i\b|software engineer 1\b|"
    r"rotational)",
    re.I,
)
FULL_STACK_RE = re.compile(r"\bfull[\s-]?stack|fullstack\b", re.I)
# International countries, regions, and cities. Unknown foreign cities (e.g.
# Bucharest) are still dropped because the filter is allowlist-based.
NON_US_RE = re.compile(
    r"\b("
    r"india|bangalore|bengaluru|hyderabad|pune|mumbai|chennai|delhi|"
    r"noida|gurgaon|gurugram|united kingdom|\buk\b|england|scotland|wales|"
    r"uk only|europe|emea|apac|latam|eu only|"
    r"canada|canadian|toronto|montreal|ottawa|calgary|edmonton|waterloo|"
    r"mississauga|burnaby|ontario|british columbia|vancouver,?\s*bc|"
    r"ireland|dublin|germany|france|netherlands|spain|italy|romania|"
    r"bucharest|poland|warsaw|sweden|stockholm|switzerland|zurich|"
    r"israel|tel aviv|singapore|australia|sydney|melbourne|"
    r"london|edinburgh|manchester|oxford|bristol|"
    r"barcelona|madrid|berlin|munich|amsterdam|paris|"
    r"tokyo|seoul|hong kong|mexico|brazil|sao paulo|argentina|"
    r"china|shanghai|beijing|shenzhen|hangzhou|taiwan|taipei|"
    r"vietnam|philippines|indonesia|malaysia|thailand|"
    r"new zealand|south africa|uae|dubai"
    r")\b",
    re.I,
)
US_COUNTRY_RE = re.compile(
    r"\b(united states|usa|u\.s\.a\.|u\.s\.)\b",
    re.I,
)
US_REMOTE_RE = re.compile(
    r"\b("
    r"remote[\s\-]*(?:\(|\[)?\s*(?:us|usa|u\.s\.a?\.?|united states)"
    r"|(?:us|usa|u\.s\.|united states)[\s\-]*remote"
    r"|united states\s*\(?\s*remote"
    r")\b",
    re.I,
)
US_STATE_NAMES = (
    "alabama", "alaska", "arizona", "arkansas", "california", "colorado",
    "connecticut", "delaware", "florida", "georgia", "hawaii", "idaho",
    "illinois", "indiana", "iowa", "kansas", "kentucky", "louisiana",
    "maine", "maryland", "massachusetts", "michigan", "minnesota",
    "mississippi", "missouri", "montana", "nebraska", "nevada",
    "new hampshire", "new jersey", "new mexico", "new york",
    "north carolina", "north dakota", "ohio", "oklahoma", "oregon",
    "pennsylvania", "rhode island", "south carolina", "south dakota",
    "tennessee", "texas", "utah", "vermont", "virginia", "washington",
    "west virginia", "wisconsin", "wyoming", "district of columbia",
)
US_STATE_NAME_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in US_STATE_NAMES) + r")\b",
    re.I,
)
# Match "Austin, TX" / "WA" location tokens, not the word "in" or "or".
US_STATE_ABBREV_RE = re.compile(
    r"(?:^|,\s*|[•|/|\-]\s*|\s)("
    r"AL|AK|AZ|AR|CA|CO|CT|DC|DE|FL|GA|HI|IA|ID|IL|IN|KS|KY|LA|MA|MD|ME|"
    r"MI|MN|MO|MS|MT|NC|ND|NE|NH|NJ|NM|NV|NY|OH|OK|OR|PA|RI|SC|SD|TN|TX|"
    r"UT|VA|VT|WA|WI|WV|WY"
    r")\b",
    re.I,
)
US_CITY_RE = re.compile(
    r"\b("
    r"nyc|n\.y\.c|new york city|san francisco|sf|bay area|"
    r"los angeles|la|seattle|austin|dallas|chicago|boston|"
    r"redmond|bellevue|mountain view|palo alto|sunnyvale|cupertino|"
    r"san jose|san diego|san mateo|santa clara|santa monica|menlo park|"
    r"irvine|atlanta|miami|denver|portland|phoenix|raleigh|charlotte|"
    r"nashville|minneapolis|philadelphia|pittsburgh|houston|"
    r"washington(?:\s*,?\s*d\.?c\.?)?|arlington|reston|mclean|"
    r"annapolis junction|fort collins|broomfield|newport beach|"
    r"costa mesa|culver city|kirkland|san bruno|wakefield|westborough|"
    r"springfield|orlando|greenwich|lafayette|indianapolis|madison|"
    r"durham|northridge|brooklyn|manhattan|queens|"
    r"mckinney|plano|frisco|irving|richardson|allen|garland|fort worth"
    r")\b",
    re.I,
)
US_FED_LOCATION_RE = re.compile(
    r"(location negotiable|anywhere in the u\.?s|multiple locations|"
    r"nationwide|various locations)",
    re.I,
)
GENERIC_REMOTE_RE = re.compile(
    r"^(remote(?:[\s\-]*(?:usa?|united states|only|hybrid))?|anywhere(?: in the united states)?)$",
    re.I,
)
SALARY_RANGE_RE = re.compile(
    r"\$\s*(\d{2,3}(?:,\d{3})?|\d{2,3}\.?\d?)\s*([kK])?"
    r"\s*(?:-|to|–|—)\s*"
    r"\$?\s*(\d{2,3}(?:,\d{3})?|\d{2,3}\.?\d?)\s*([kK])?",
)
SALARY_SINGLE_RE = re.compile(r"\$\s*(\d{2,3}(?:,\d{3})?)\s*([kK])\b")
SALARY_HOUR_RE = re.compile(r"\$\s*(\d{2,3}(?:\.\d+)?)\s*(?:/|\s*)(?:hr|hour|hourly)\b", re.I)

# Companies where new-grad / early-career SWE total compensation commonly clears $150k.
HIGH_COMP_COMPANIES = {
    "google",
    "alphabet",
    "meta",
    "facebook",
    "apple",
    "amazon",
    "microsoft",
    "netflix",
    "nvidia",
    "openai",
    "anthropic",
    "xai",
    "stripe",
    "databricks",
    "snowflake",
    "palantir",
    "uber",
    "airbnb",
    "linkedin",
    "bytedance",
    "tiktok",
    "figma",
    "notion",
    "cloudflare",
    "datadog",
    "roblox",
    "snap",
    "pinterest",
    "block",
    "square",
    "coinbase",
    "robinhood",
    "instacart",
    "doordash",
    "lyft",
    "plaid",
    "brex",
    "ramp",
    "rippling",
    "anduril",
    "scale ai",
    "scale",
    "glean",
    "perplexity",
    "bloomberg",
    "salesforce",
    "adobe",
    "servicenow",
    "atlassian",
    "shopify",
    "twilio",
    "okta",
    "mongodb",
    "elastic",
    "confluent",
    "hashicorp",
    "github",
    "gitlab",
    "vercel",
    "discord",
    "reddit",
    "dropbox",
    "box",
    "slack",
    "zoom",
    "crowdstrike",
    "palo alto networks",
    "jane street",
    "citadel",
    "two sigma",
    "hudson river trading",
    "hrt",
    "jump trading",
    "tower research",
    "imc",
    "optiver",
    "susquehanna",
    "sig",
    "d.e. shaw",
    "de shaw",
    "millennium",
    "point72",
    "akuna",
    "drw",
    "virtu",
    "ctc",
    "chicago trading",
    "jpmorgan",
    "jp morgan",
    "jpmorgan chase",
    "goldman",
    "goldman sachs",
    "morgan stanley",
    "citigroup",
    "citibank",
    "citi",
    "bank of america",
    "wells fargo",
    "capital one",
    "american express",
    "visa",
    "mastercard",
    "blackrock",
    "fidelity",
    "schwab",
    "bny mellon",
    "usaa",
    "pnc",
    "truist",
    "ally",
    "vanguard",
    "state street",
    "tiaa",
    "paypal",
}

# Corporates / defense where new-grad SWE commonly clears $100k but not $150k.
CORPORATE_COMPANIES = {
    "boeing",
    "northrop",
    "lockheed",
    "leidos",
    "gdit",
    "booz allen",
    "caci",
    "parsons",
    "rtx",
    "raytheon",
    "target",
    "walmart",
    "disney",
    "humana",
    "pfizer",
    "abbott",
    "cigna",
    "chevron",
    "intel",
    "cisco",
    "micron",
    "autodesk",
    "procter",
    "nationwide",
    "prudential",
    "honeywell",
    "verizon",
    "dell",
    "ibm",
    "oracle",
}

GOVERNMENT_TOKENS = {
    "department of defense",
    "department of the navy",
    "department of the army",
    "department of the air force",
    "department of energy",
    "department of veterans",
    "department of homeland",
    "department of commerce",
    "department of the treasury",
    "department of justice",
    "department of state",
    "national security agency",
    "central intelligence",
    "federal bureau",
    "national aeronautics",
    "internal revenue service",
    "social security administration",
    "national institutes of health",
    "centers for disease",
    "cybersecurity and infrastructure",
    "general services administration",
    "national geospatial",
    "defense intelligence",
    "space force",
    "lawrence livermore",
    "los alamos",
    "oak ridge",
    "sandia",
    "argonne",
    "usajobs",
}

WORKDAY_QUERIES = (
    "new grad software engineer",
    "early career software",
)
WORKDAY_PAGE_SIZE = 20

HCOL_TOKENS = (
    "san francisco",
    "bay area",
    "sf,",
    "mountain view",
    "palo alto",
    "menlo park",
    "sunnyvale",
    "cupertino",
    "seattle",
    "redmond",
    "bellevue",
    "new york",
    "nyc",
    "manhattan",
    "brooklyn",
    "los angeles",
    "santa monica",
    "remote - us",
    "united states (remote)",
    "us remote",
)


# ---------------------------------------------------------------------------
# Title / location / salary
# ---------------------------------------------------------------------------

def _norm(text: str) -> str:
    return " ".join((text or "").lower().split())


def excluded_by_title(title: str, exclude_keywords: list[str]) -> bool:
    hay = _norm(title)
    for token in exclude_keywords:
        cleaned = token.strip().lower().strip(".")
        if cleaned and re.search(rf"\b{re.escape(cleaned)}\b", hay):
            return True
    return bool(
        re.search(
            r"\b(sr\.?|senior|staff|principal|director|manager|lead|"
            r"intern(?:ship)?|co-?op|phd|architect|engineer\s*[2-9]|level\s*[3-9])\b",
            title,
            re.I,
        )
    )


def _has_strong_us_location(location: str) -> bool:
    return bool(
        US_COUNTRY_RE.search(location)
        or US_REMOTE_RE.search(location)
        or US_STATE_NAME_RE.search(location)
        or US_CITY_RE.search(location)
    )


def is_us_role(location: str, description: str = "", url: str = "", title: str = "") -> bool:
    """Keep a posting only when its location field is U.S. (or Remote (US)).

    The location string is the sole signal: URL/title/description are ignored
    so a Stripe jobs URL cannot rescue Barcelona, and a foreign city with no
    U.S. office (Bucharest, London, Singapore) is discarded.
    Mixed multi-office strings that include a U.S. city or state are kept.
    """
    loc = _norm(location)
    if not loc:
        return True
    if loc in {"us", "usa", "u.s.", "u.s.a.", "united states"}:
        return True
    if US_FED_LOCATION_RE.search(loc) and not NON_US_RE.search(loc):
        return True
    if GENERIC_REMOTE_RE.search(loc) and not NON_US_RE.search(loc):
        return True
    if US_REMOTE_RE.search(loc):
        return True

    non_us = bool(NON_US_RE.search(loc))
    strong_us = _has_strong_us_location(loc)
    state_abbrev = bool(US_STATE_ABBREV_RE.search(location or ""))

    # "Bangalore, IN" must not count Indiana; require a real U.S. place name
    # whenever a foreign country/city is present.
    if non_us:
        return strong_us
    return strong_us or state_abbrev


def _to_annual(amount: float, interval: str | None) -> int:
    interval = (interval or "yearly").lower()
    if interval in {"hour", "hourly"}:
        return int(amount * 2080)
    if interval in {"day", "daily"}:
        return int(amount * 260)
    if interval in {"week", "weekly"}:
        return int(amount * 52)
    if interval in {"month", "monthly"}:
        return int(amount * 12)
    return int(amount)


def _parse_money_token(raw: str, k_flag: str | None) -> int:
    cleaned = raw.replace(",", "")
    value = float(cleaned)
    if k_flag or (value < 1000 and value >= 30):
        value *= 1000
    return int(value)


def parse_salary_from_text(text: str) -> tuple[int | None, int | None]:
    if not text:
        return None, None

    hourly = SALARY_HOUR_RE.search(text)
    if hourly:
        annual = int(float(hourly.group(1)) * 2080)
        return annual, annual

    ranged = SALARY_RANGE_RE.search(text)
    if ranged:
        low = _parse_money_token(ranged.group(1), ranged.group(2))
        high = _parse_money_token(ranged.group(3), ranged.group(4))
        if low > high:
            low, high = high, low
        return low, high

    single = SALARY_SINGLE_RE.search(text)
    if single:
        amount = _parse_money_token(single.group(1), single.group(2))
        return amount, amount
    return None, None


def company_matches(company: str, tokens: set[str]) -> bool:
    name = _norm(company)
    padded = f" {name} "
    for token in tokens:
        if name == token or padded.find(f" {token} ") >= 0 or name.startswith(token + " "):
            return True
    return False


def company_in_high_comp(company: str) -> bool:
    return company_matches(company, HIGH_COMP_COMPANIES)


def company_in_corporate(company: str) -> bool:
    return company_matches(company, CORPORATE_COMPANIES)


def is_government_employer(job: JobPosting) -> bool:
    if job.source == "usajobs":
        return True
    return company_matches(job.company, GOVERNMENT_TOKENS)


def location_is_hcol(location: str) -> bool:
    loc = _norm(location)
    return any(token in loc for token in HCOL_TOKENS)


def estimate_salary(job: JobPosting) -> tuple[int | None, int | None, bool]:
    listed_min, listed_max = job.salary_min, job.salary_max
    parsed_min, parsed_max = parse_salary_from_text(f"{job.title}\n{job.description}")
    salary_min = listed_min or parsed_min
    salary_max = listed_max or parsed_max
    estimated = listed_min is None and listed_max is None and (parsed_min is not None)

    if salary_min or salary_max:
        return salary_min, salary_max, estimated

    if company_in_high_comp(job.company) and (
        location_is_hcol(job.location) or "remote" in _norm(job.location)
    ):
        return 160_000, 200_000, True

    if company_in_high_comp(job.company):
        return 155_000, 190_000, True

    if company_in_corporate(job.company) or job.source == "workday":
        return 115_000, 145_000, True

    if is_government_employer(job):
        return 100_000, 130_000, True

    return None, None, False


def salary_preference_rank(job: JobPosting, preferred: int) -> int:
    """2 = at/above preferred, 1 = max could reach it, 0 = below."""
    low = job.salary_min or 0
    high = job.salary_max or low
    if low >= preferred:
        return 2
    if high >= preferred:
        return 1
    return 0


def meets_salary_floor(job: JobPosting, floor: int) -> bool:
    """
    Strict floor: the low end of listed/estimated pay must be >= floor.
    If only a max is known, require max >= floor + 20k so the range can
    realistically clear the floor.
    """
    if job.salary_min is not None:
        return job.salary_min >= floor
    if job.salary_max is not None:
        return job.salary_max >= floor + 20_000
    return False


# ---------------------------------------------------------------------------
# Resume similarity
# ---------------------------------------------------------------------------

def resume_similarity(job: JobPosting, profile: dict[str, Any]) -> tuple[float, list[str]]:
    skills = [str(skill) for skill in profile.get("skills") or []]
    extra = [str(word) for word in profile.get("keywords") or []]
    school = str(profile.get("school") or "")
    degree = str(profile.get("degree") or "")

    job_text = " ".join([job.title, job.company, job.location, job.description])
    job_tokens = tokenize(job_text)
    job_hay = _norm(job_text)

    matched: list[str] = []
    for skill in skills:
        token_set = tokenize(skill)
        if skill.lower() in job_hay or (token_set and token_set <= job_tokens):
            matched.append(skill)

    skill_score = (len(matched) / len(skills) * 100.0) if skills else 0.0

    title_bits = 0.0
    if SWE_TITLE_RE.search(job.title):
        title_bits += 40
    if NEW_GRAD_RE.search(job.title) or job.source in {"simplify", "usajobs"}:
        title_bits += 45
    if FULL_STACK_RE.search(job.title):
        title_bits += 20
    title_score = min(title_bits, 100.0)

    keyword_hits = sum(1 for word in extra if word.lower() in job_hay)
    keyword_score = (keyword_hits / len(extra) * 100.0) if extra else 0.0

    education_score = 0.0
    if "computer science" in job_hay or tokenize(degree) <= job_tokens:
        education_score += 50
    if "utd" in job_hay or "texas at dallas" in job_hay or tokenize(school) & job_tokens:
        education_score += 20
    if "patent" in job_hay:
        education_score += 30
    education_score = min(education_score, 100.0)

    # Short board listings rarely include a skill dump — don't zero them out.
    if len(job.description) < 400:
        skill_score = max(skill_score, 45.0)

    score = (
        0.40 * skill_score
        + 0.30 * title_score
        + 0.20 * keyword_score
        + 0.10 * education_score
    )
    return round(score, 1), matched


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------

def _cell(row: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        if hasattr(row, name):
            value = getattr(row, name)
        elif isinstance(row, dict):
            value = row.get(name, default)
        else:
            try:
                value = row[name]
            except Exception:
                value = default
        if value is not None and not (isinstance(value, float) and str(value) == "nan"):
            try:
                import pandas as pd

                if isinstance(value, float) and pd.isna(value):
                    continue
            except Exception:
                pass
            return value
    return default


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"nan", "none"}:
        return ""
    return text


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        import pandas as pd

        if pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def scrape_jobspy(search_terms: list[str], hours_old: int, results_wanted: int) -> list[JobPosting]:
    try:
        from jobspy import scrape_jobs
    except Exception as exc:
        log.warning("python-jobspy is unavailable (%s) — skipping board scrape.", exc)
        return []

    for name in ("JobSpy", "JobSpy:Glassdoor", "JobSpy:ZipRecruiter"):
        logging.getLogger(name).setLevel(logging.CRITICAL)

    jobs: list[JobPosting] = []
    for term in search_terms:
        google_term = f"{term} jobs in United States since last week"
        log.info("JobSpy search: %s", term)
        try:
            frame = scrape_jobs(
                site_name=JOBSPY_SITES,
                search_term=term,
                google_search_term=google_term,
                location="United States",
                results_wanted=results_wanted,
                hours_old=hours_old,
                country_indeed="USA",
                job_type="fulltime",
                linkedin_fetch_description=False,
                enforce_annual_salary=True,
                verbose=0,
            )
        except Exception as exc:
            log.warning("JobSpy failed for %r: %s", term, exc)
            time.sleep(2)
            continue

        if frame is None or getattr(frame, "empty", True):
            log.info("JobSpy returned 0 rows for %r", term)
            time.sleep(1.5)
            continue

        for _, row in frame.iterrows():
            title = _as_text(_cell(row, "title"))
            company = _as_text(_cell(row, "company"))
            url = _as_text(_cell(row, "job_url", "url"))
            apply_url = _as_text(_cell(row, "job_url_direct")) or url
            source = _as_text(_cell(row, "site")) or "jobspy"
            if not title or not url:
                continue
            interval = _as_text(_cell(row, "interval")) or "yearly"
            raw_min = _as_int(_cell(row, "min_amount"))
            raw_max = _as_int(_cell(row, "max_amount"))
            salary_min = _to_annual(raw_min, interval) if raw_min else None
            salary_max = _to_annual(raw_max, interval) if raw_max else None
            jobs.append(
                JobPosting(
                    id=make_job_id(
                        source,
                        _as_text(_cell(row, "id")),
                        url,
                        company,
                        title,
                    ),
                    title=title,
                    company=company,
                    location=_as_text(_cell(row, "location")),
                    source=source,
                    url=url,
                    apply_url=apply_url,
                    description=_as_text(_cell(row, "description")),
                    salary_min=salary_min,
                    salary_max=salary_max,
                    date_posted=_as_text(_cell(row, "date_posted")),
                )
            )
        time.sleep(1.5)
    log.info("JobSpy collected %s raw postings.", len(jobs))
    return jobs


def _strip_html(raw: str) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(raw or ""))
    return " ".join(text.split())


def fetch_greenhouse_board(token: str) -> list[JobPosting]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
    try:
        response = SESSION.get(url, params={"content": "true"}, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.debug("Greenhouse %s: %s", token, exc)
        return []

    jobs: list[JobPosting] = []
    for item in payload.get("jobs") or []:
        title = (item.get("title") or "").strip()
        apply_url = (item.get("absolute_url") or "").strip()
        if not title or not apply_url:
            continue
        location = ((item.get("location") or {}).get("name")) or ""
        company = token.replace("_", " ").title()
        jobs.append(
            JobPosting(
                id=make_job_id("greenhouse", str(item.get("id") or ""), apply_url, company, title),
                title=title,
                company=company,
                location=location,
                source="greenhouse",
                url=apply_url,
                apply_url=apply_url,
                description=_strip_html(item.get("content") or ""),
                date_posted=str(item.get("updated_at") or item.get("created_at") or ""),
            )
        )
    return jobs


def fetch_lever_board(token: str) -> list[JobPosting]:
    url = f"https://api.lever.co/v0/postings/{token}"
    try:
        response = SESSION.get(url, params={"mode": "json"}, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.debug("Lever %s: %s", token, exc)
        return []

    if not isinstance(payload, list):
        return []

    jobs: list[JobPosting] = []
    for item in payload:
        title = (item.get("text") or "").strip()
        apply_url = (item.get("hostedUrl") or item.get("applyUrl") or "").strip()
        if not title or not apply_url:
            continue
        categories = item.get("categories") or {}
        location = categories.get("location") or ""
        company = token.replace("-", " ").title()
        jobs.append(
            JobPosting(
                id=make_job_id("lever", str(item.get("id") or ""), apply_url, company, title),
                title=title,
                company=company,
                location=location,
                source="lever",
                url=apply_url,
                apply_url=apply_url,
                description=_strip_html(
                    item.get("descriptionPlain") or item.get("description") or ""
                ),
                date_posted=str(item.get("createdAt") or ""),
            )
        )
    return jobs


def fetch_ashby_board(token: str) -> list[JobPosting]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{token}"
    try:
        response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
        if response.status_code == 404:
            return []
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        log.debug("Ashby %s: %s", token, exc)
        return []

    jobs: list[JobPosting] = []
    for item in payload.get("jobs") or []:
        title = (item.get("title") or "").strip()
        apply_url = (item.get("jobUrl") or item.get("applyUrl") or "").strip()
        if not title or not apply_url:
            continue
        location_raw = item.get("location")
        if isinstance(location_raw, list):
            location = ", ".join(str(part) for part in location_raw if part)
        else:
            location = str(location_raw or "")
        company = token.replace("-", " ").title()
        jobs.append(
            JobPosting(
                id=make_job_id("ashby", str(item.get("id") or ""), apply_url, company, title),
                title=title,
                company=company,
                location=location,
                source="ashby",
                url=apply_url,
                apply_url=apply_url,
                description=_strip_html(item.get("descriptionHtml") or item.get("descriptionPlain") or ""),
                date_posted=str(item.get("publishedDate") or item.get("updatedAt") or ""),
            )
        )
    return jobs


def fetch_workday_board(board: dict[str, Any]) -> list[JobPosting]:
    name = str(board.get("name") or board.get("tenant") or "Company").strip()
    host = str(board.get("host") or "").strip()
    tenant = str(board.get("tenant") or "").strip()
    site = str(board.get("site") or "").strip()
    if not host or not tenant or not site:
        return []

    jobs: list[JobPosting] = []
    seen_paths: set[str] = set()
    for query in WORKDAY_QUERIES:
        url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
        try:
            response = SESSION.post(
                url,
                json={
                    "appliedFacets": {},
                    "limit": WORKDAY_PAGE_SIZE,
                    "offset": 0,
                    "searchText": query,
                },
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=REQUEST_TIMEOUT,
            )
            if response.status_code != 200:
                continue
            payload = response.json()
        except Exception as exc:
            log.debug("Workday %s: %s", name, exc)
            continue

        for item in payload.get("jobPostings") or []:
            title = (item.get("title") or "").strip()
            path = (item.get("externalPath") or "").strip()
            if not title or not path or path in seen_paths:
                continue
            seen_paths.add(path)
            if not path.startswith("/"):
                path = "/" + path
            apply_url = f"https://{host}/en-US/{site}{path}"
            location = (item.get("locationsText") or "").strip()
            jobs.append(
                JobPosting(
                    id=make_job_id("workday", f"{tenant}:{path}", apply_url, name, title),
                    title=title,
                    company=name,
                    location=location,
                    source="workday",
                    url=apply_url,
                    apply_url=apply_url,
                    description=f"Workday listing at {name}.",
                    date_posted=str(item.get("postedOn") or ""),
                )
            )
    return jobs


def scrape_usajobs(email: str = "") -> list[JobPosting]:
    api_key = env("USAJOBS_API_KEY")
    user_email = env("USAJOBS_EMAIL") or email
    if not api_key:
        log.info(
            "USAJOBS skipped — set USAJOBS_API_KEY in .env "
            "(free at https://developer.usajobs.gov/)."
        )
        return []

    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": user_email or "automated-job-monitor",
        "Authorization-Key": api_key,
    }
    jobs: list[JobPosting] = []
    for page in (1, 2, 3):
        try:
            response = SESSION.get(
                "https://data.usajobs.gov/api/search",
                headers=headers,
                params={
                    "JobCategoryCode": "1550;2210;0854;1560",
                    "HiringPath": "graduates;public",
                    "WhoMayApply": "public",
                    "DatePosted": "30",
                    "ResultsPerPage": "50",
                    "Page": str(page),
                    "Keyword": "software",
                    "PayGradeHigh": "12",
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            log.warning("USAJOBS page %s failed: %s", page, exc)
            break

        items = ((payload.get("SearchResult") or {}).get("SearchResultItems") or [])
        if not items:
            break
        for wrapper in items:
            descriptor = wrapper.get("MatchedObjectDescriptor") or {}
            title = (descriptor.get("PositionTitle") or "").strip()
            apply_urls = descriptor.get("ApplyURI") or []
            apply_url = (
                (apply_urls[0] if apply_urls else "")
                or descriptor.get("PositionURI")
                or ""
            ).strip()
            if not title or not apply_url:
                continue
            company = (
                descriptor.get("OrganizationName")
                or descriptor.get("DepartmentName")
                or "U.S. Government"
            )
            location = descriptor.get("PositionLocationDisplay") or ""
            pay = (descriptor.get("PositionRemuneration") or [{}])[0]
            salary_min = _as_int(pay.get("MinimumRange"))
            salary_max = _as_int(pay.get("MaximumRange"))
            details = (descriptor.get("UserArea") or {}).get("Details") or {}
            description = _strip_html(
                str(details.get("JobSummary") or descriptor.get("QualificationSummary") or "")
            )
            jobs.append(
                JobPosting(
                    id=make_job_id(
                        "usajobs",
                        str(wrapper.get("MatchedObjectId") or descriptor.get("PositionID") or ""),
                        apply_url,
                        company,
                        title,
                    ),
                    title=title,
                    company=str(company).strip(),
                    location=str(location).strip(),
                    source="usajobs",
                    url=apply_url,
                    apply_url=apply_url,
                    description=description,
                    salary_min=salary_min,
                    salary_max=salary_max,
                    date_posted=str(descriptor.get("PublicationStartDate") or ""),
                    notes="Sourced from USAJOBS.",
                )
            )
    log.info("USAJOBS collected %s raw postings.", len(jobs))
    return jobs


def scrape_ats_boards() -> list[JobPosting]:
    config = load_json(
        ATS_COMPANIES_PATH,
        {"greenhouse": [], "lever": [], "ashby": [], "workday": []},
    )
    greenhouse_tokens = list(config.get("greenhouse") or [])
    lever_tokens = list(config.get("lever") or [])
    ashby_tokens = list(config.get("ashby") or [])
    workday_boards = [board for board in (config.get("workday") or []) if isinstance(board, dict)]
    jobs: list[JobPosting] = []

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_greenhouse_board, token) for token in greenhouse_tokens]
        futures += [pool.submit(fetch_lever_board, token) for token in lever_tokens]
        futures += [pool.submit(fetch_ashby_board, token) for token in ashby_tokens]
        futures += [pool.submit(fetch_workday_board, board) for board in workday_boards]
        for future in as_completed(futures):
            try:
                jobs.extend(future.result())
            except Exception as exc:
                log.debug("ATS worker failed: %s", exc)

    log.info("ATS boards collected %s raw postings.", len(jobs))
    return jobs


def scrape_simplify_newgrad() -> list[JobPosting]:
    payload = None
    for url in SIMPLIFY_LISTING_URLS:
        try:
            response = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            if response.status_code != 200:
                continue
            payload = response.json()
            break
        except Exception as exc:
            log.debug("Simplify listings %s: %s", url, exc)

    if not isinstance(payload, list):
        log.warning("Simplify new-grad list unavailable.")
        return []

    jobs: list[JobPosting] = []
    for item in payload:
        if item.get("active") is False or item.get("is_visible") is False:
            continue
        category = _norm(str(item.get("category") or ""))
        title = (item.get("title") or "").strip()
        url = (item.get("url") or "").strip()
        company = (item.get("company_name") or "").strip()
        if not title or not url:
            continue
        if category and category not in {
            "software",
            "software engineering",
            "ai/ml/data",
            "ai",
            "quant",
        }:
            if not SWE_TITLE_RE.search(title):
                continue
        locations = item.get("locations") or []
        location = ", ".join(locations) if isinstance(locations, list) else str(locations)
        jobs.append(
            JobPosting(
                id=make_job_id("simplify", str(item.get("id") or ""), url, company, title),
                title=title,
                company=company,
                location=location,
                source="simplify",
                url=url,
                apply_url=url,
                description=f"{category} new-grad listing. Sponsorship: {item.get('sponsorship') or 'unlisted'}.",
                date_posted=str(item.get("date_posted") or item.get("date_updated") or ""),
                notes="Sourced from SimplifyJobs/New-Grad-Positions.",
            )
        )
    log.info("Simplify collected %s raw postings.", len(jobs))
    return jobs


def scrape_jsearch(search_terms: list[str], rapidapi_key: str) -> list[JobPosting]:
    if not rapidapi_key:
        return []

    jobs: list[JobPosting] = []
    headers = {
        "X-RapidAPI-Key": rapidapi_key,
        "X-RapidAPI-Host": "jsearch.p.rapidapi.com",
    }
    for term in search_terms[:3]:
        try:
            response = SESSION.get(
                "https://jsearch.p.rapidapi.com/search",
                headers=headers,
                params={
                    "query": f"{term} in United States",
                    "page": "1",
                    "num_pages": "2",
                    "date_posted": "week",
                    "employment_types": "FULLTIME",
                    "country": "us",
                },
                timeout=REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            payload = response.json()
        except Exception as exc:
            log.warning("JSearch failed for %r: %s", term, exc)
            continue

        for item in payload.get("data") or []:
            title = (item.get("job_title") or "").strip()
            url = (item.get("job_apply_link") or item.get("job_google_link") or "").strip()
            company = (item.get("employer_name") or "").strip()
            if not title or not url:
                continue
            location_bits = [
                item.get("job_city") or "",
                item.get("job_state") or "",
                item.get("job_country") or "",
            ]
            salary_min = _as_int(item.get("job_min_salary"))
            salary_max = _as_int(item.get("job_max_salary"))
            period = (item.get("job_salary_period") or "YEAR").lower()
            if salary_min:
                salary_min = _to_annual(salary_min, period)
            if salary_max:
                salary_max = _to_annual(salary_max, period)
            jobs.append(
                JobPosting(
                    id=make_job_id(
                        item.get("job_publisher") or "jsearch",
                        str(item.get("job_id") or ""),
                        url,
                        company,
                        title,
                    ),
                    title=title,
                    company=company,
                    location=", ".join(bit for bit in location_bits if bit),
                    source=(item.get("job_publisher") or "jsearch").lower(),
                    url=url,
                    apply_url=url,
                    description=_as_text(item.get("job_description")),
                    salary_min=salary_min,
                    salary_max=salary_max,
                    date_posted=_as_text(item.get("job_posted_at_datetime_utc")),
                )
            )
        time.sleep(0.8)
    log.info("JSearch collected %s raw postings.", len(jobs))
    return jobs


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

def dedupe(jobs: Iterable[JobPosting]) -> list[JobPosting]:
    unique: dict[str, JobPosting] = {}
    for job in jobs:
        unique[job.id] = job
    return list(unique.values())


def filter_and_score(
    jobs: list[JobPosting],
    profile: dict[str, Any],
    require_new_grad_signal: bool = True,
) -> list[JobPosting]:
    prefs = profile.get("preferences") or {}
    floor = int(prefs.get("salary_floor") or DEFAULT_SALARY_FLOOR)
    preferred = int(prefs.get("preferred_salary") or DEFAULT_PREFERRED_SALARY)
    min_score = float(prefs.get("min_match_score") or 0)
    exclude = [str(word).lower() for word in prefs.get("exclude_title_keywords") or []]

    kept: list[JobPosting] = []
    skipped_non_us = 0
    for job in jobs:
        if excluded_by_title(job.title, exclude):
            continue
        if not is_us_role(job.location, job.description, job.url, job.title):
            skipped_non_us += 1
            log.debug("Dropping non-U.S. location %r (%s — %s)", job.location, job.company, job.title)
            continue

        swe_ok = bool(SWE_TITLE_RE.search(job.title))
        new_grad_ok = bool(NEW_GRAD_RE.search(job.title))
        full_stack_ok = bool(FULL_STACK_RE.search(job.title))
        simplify_ok = job.source == "simplify" and swe_ok
        gov_ok = job.source == "usajobs" and swe_ok
        if require_new_grad_signal and not (new_grad_ok or full_stack_ok or simplify_ok or gov_ok):
            continue
        if not swe_ok and not full_stack_ok:
            continue

        salary_min, salary_max, estimated = estimate_salary(job)
        job.salary_min = salary_min
        job.salary_max = salary_max
        job.salary_estimated = estimated
        if not meets_salary_floor(job, floor):
            continue

        score, matched = resume_similarity(job, profile)
        job.match_score = score
        job.matched_skills = matched
        if score < min_score:
            continue
        if not job.first_seen:
            job.first_seen = utc_now()
        kept.append(job)

    if skipped_non_us:
        log.info("Discarded %s postings outside the United States.", skipped_non_us)
    kept.sort(
        key=lambda item: (
            -salary_preference_rank(item, preferred),
            -item.match_score,
            item.company.lower(),
            item.title.lower(),
        )
    )
    return kept


def load_seen() -> dict[str, Any]:
    data = load_json(SEEN_JOBS_PATH, {"jobs": {}})
    if "jobs" not in data or not isinstance(data["jobs"], dict):
        data = {"jobs": {}}
    return data


def persist_matches(new_jobs: list[JobPosting], seen: dict[str, Any]) -> list[JobPosting]:
    fresh: list[JobPosting] = []
    records = seen.setdefault("jobs", {})
    now = utc_now()
    for job in new_jobs:
        existing = records.get(job.id)
        if existing:
            existing["last_seen"] = now
            continue
        job.first_seen = now
        records[job.id] = {**job.to_dict(), "last_seen": now, "notified": False}
        fresh.append(job)

    seen["updated_at"] = now
    save_json(SEEN_JOBS_PATH, seen)

    previous = load_json(MATCHES_PATH, {"jobs": []})
    queued: dict[str, dict[str, Any]] = {}
    # Keep prior queue rows (pending / prepped / applied / synced) so the
    # dashboard and Google Sheets workflow are not wiped on each scrape.
    # Re-apply the U.S. location filter so international leftovers are dropped.
    for raw in previous.get("jobs") or []:
        job_id = raw.get("id")
        if not job_id:
            continue
        if not is_us_role(str(raw.get("location") or "")):
            continue
        queued[job_id] = raw
    for job in reversed(fresh):
        prior = queued.get(job.id) or {}
        row = job.to_dict()
        row["fill_status"] = prior.get("fill_status") or row.get("fill_status") or "pending"
        for keep in ("notes", "contact_person", "contact_email", "applied_at", "prepped_at", "synced_at"):
            if prior.get(keep) and not row.get(keep):
                row[keep] = prior[keep]
        queued[job.id] = row

    ordered = sorted(
        queued.values(),
        key=lambda raw: (
            -int(float(raw.get("salary_min") or 0) >= DEFAULT_PREFERRED_SALARY),
            -float(raw.get("match_score") or 0),
            str(raw.get("company") or ""),
        ),
    )
    payload = {
        "generated_at": now,
        "new_count": len(fresh),
        "jobs": ordered,
        "queue_count": len(ordered),
    }
    save_json(MATCHES_PATH, payload)
    return fresh


def prune_non_us_matches() -> tuple[int, int]:
    """Drop non-U.S. roles from matches.json without running a scrape."""
    previous = load_json(MATCHES_PATH, {"jobs": []})
    jobs = list(previous.get("jobs") or [])
    kept = [row for row in jobs if is_us_role(str(row.get("location") or ""))]
    dropped = len(jobs) - len(kept)
    previous["jobs"] = kept
    previous["queue_count"] = len(kept)
    previous["generated_at"] = utc_now()
    previous["new_count"] = 0
    save_json(MATCHES_PATH, previous)
    log.info("Pruned matches.json: removed %s non-U.S. roles, %s remain.", dropped, len(kept))
    return len(kept), dropped


def discover(
    profile: dict[str, Any] | None = None,
    hours_old: int = 72,
    results_wanted: int = 25,
    skip_jobspy: bool = False,
    auto_prep: bool = True,
) -> list[JobPosting]:
    profile = profile or load_profile()
    prefs = profile.get("preferences") or {}
    terms = list(prefs.get("search_terms") or ["Software Engineer New Grad"])
    floor = int(prefs.get("salary_floor") or DEFAULT_SALARY_FLOOR)

    collected: list[JobPosting] = []
    if not skip_jobspy:
        collected.extend(scrape_jobspy(terms, hours_old=hours_old, results_wanted=results_wanted))
    collected.extend(scrape_ats_boards())
    collected.extend(scrape_simplify_newgrad())
    collected.extend(scrape_usajobs(str(profile.get("email") or "")))
    collected.extend(scrape_jsearch(terms, env("RAPIDAPI_KEY")))

    unique = dedupe(collected)
    log.info("Raw unique postings: %s", len(unique))
    matched = filter_and_score(unique, profile)
    log.info("Postings after $%sk + similarity filters: %s", floor // 1000, len(matched))
    seen = load_seen()
    fresh = persist_matches(matched, seen)
    log.info("Brand-new matches written: %s", len(fresh))
    if auto_prep:
        ready = [job.id for job in fresh if job.best_url()]
        message = request_auto_prep(ready)
        if message:
            log.info("%s", message)
    return fresh


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Monitor major job boards for $100k+ New Grad / Full Stack SWE roles."
    )
    parser.add_argument("--hours-old", type=int, default=72, help="JobSpy freshness window.")
    parser.add_argument(
        "--results-wanted",
        type=int,
        default=25,
        help="Results per search term per JobSpy run.",
    )
    parser.add_argument(
        "--skip-jobspy",
        action="store_true",
        help="Skip LinkedIn/Indeed/Glassdoor/ZipRecruiter/Google (ATS + Simplify only).",
    )
    parser.add_argument(
        "--prune-non-us",
        action="store_true",
        help="Drop non-U.S. locations from matches.json and exit (no scrape).",
    )
    parser.add_argument(
        "--no-auto-prep",
        action="store_true",
        help="Discover and queue matches without launching Playwright auto-prep.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    if args.prune_non_us:
        kept, dropped = prune_non_us_matches()
        print(f"[aggregator] Pruned matches.json: removed {dropped} non-U.S. roles, {kept} remain.")
        return

    profile = load_profile()
    email = profile.get("email", "")
    if "YOUR_EMAIL" in email or "example.com" in email:
        log.warning("profile.json still has placeholder contact details.")

    fresh = discover(
        profile,
        hours_old=args.hours_old,
        results_wanted=args.results_wanted,
        skip_jobspy=args.skip_jobspy,
        auto_prep=not args.no_auto_prep,
    )
    if not fresh:
        print("[aggregator] No new matching jobs this run. seen_jobs.json / matches.json updated.")
        return

    print(f"[aggregator] {len(fresh)} new matching jobs:\n")
    for job in fresh:
        print(
            f"  {job.match_score:5.1f}  {job.salary_label():<22}  "
            f"{job.source:<12}  {job.company} — {job.title}"
        )
        print(f"         {job.best_url()}")
    if args.no_auto_prep:
        print(
            f"\nQueue saved to {MATCHES_PATH.name}. Fill forms with:  "
            "python bot.py --auto-prep --from-matches"
        )
    else:
        print(
            f"\nQueued {len(fresh)} matching role(s). $150k+ listings rank first. "
            "Greenhouse/Lever/Ashby forms open one at a time; other portals are skipped."
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\n[aggregator] Interrupted.")
