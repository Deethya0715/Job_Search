"""Shared paths, profile loading, job records, and dashboard state helpers."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

try:
    from dotenv import load_dotenv
except ImportError:  # Streamlit Cloud if python-dotenv did not install
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

ROOT = Path(__file__).resolve().parent
PROFILE_PATH = ROOT / "profile.json"
SEEN_JOBS_PATH = ROOT / "seen_jobs.json"
MATCHES_PATH = ROOT / "matches.json"
ATS_COMPANIES_PATH = ROOT / "ats_companies.json"
ANSWERS_PATH = ROOT / "answers.json"
REPORTS_DIR = ROOT / "reports"
ENV_PATH = ROOT / ".env"
WORKER_STATUS_PATH = ROOT / "worker_status.json"
WORKER_LOG_PATH = ROOT / "worker.log"
TRACKER_LOG_PATH = ROOT / "tracker_log.json"
BOT_LOCK_PATH = ROOT / ".bot_running"
BOT_LOG_PATH = ROOT / "bot.log"
PREP_QUEUE_PATH = ROOT / "prep_queue.json"
REVIEW_SIGNAL_PATH = ROOT / ".bot_review.json"
STOP_FLAG_PATH = ROOT / ".worker_stop"

load_dotenv(ENV_PATH)

DEFAULT_SALARY_FLOOR = 150_000
DEFAULT_MIN_MATCH_SCORE = 40.0
try:
    LOCAL_TZ = ZoneInfo("America/Chicago")
except Exception:
    LOCAL_TZ = timezone.utc
GOOGLE_SHEETS_ID_DEFAULT = "1TO5rtMymBoo64X2bld2R-HqBGlWim_m7kHNcHkICtxY"

# fill_status values used across aggregator, bot, tracker, and the dashboard.
STATUS_PENDING = "pending"  # discovered, waiting for automatic Playwright prep
STATUS_PREPPING = "prepping"  # Playwright is filling the form now
STATUS_REVIEWING = "reviewing"  # browser open; human is editing / submitting
STATUS_PREPPED = "prepped"  # form filled, waiting for human Submit / dashboard approval
STATUS_APPLIED = "applied"  # human confirmed Submit
STATUS_SYNCED = "synced"  # row landed in Google Sheets
STATUS_SKIPPED = "skipped"
STATUS_FAILED = "failed"

STATUS_LABELS = {
    STATUS_PENDING: "Queued — waiting for Playwright",
    STATUS_PREPPING: "Prepping now",
    STATUS_REVIEWING: "Open in browser — edit then Submit",
    STATUS_PREPPED: "Ready for review",
    STATUS_APPLIED: "Submitted — syncing",
    STATUS_SYNCED: "Synced to Sheets",
    STATUS_SKIPPED: "Skipped",
    STATUS_FAILED: "Prep failed",
}

SHEET_HEADERS = [
    "Company",
    "Role Title",
    "Location",
    "Date Applied",
    "Status",
    "Contact Person",
    "Contact Email",
    "Notes",
    "Job Link",
]


@dataclass
class JobPosting:
    id: str
    title: str
    company: str
    location: str
    source: str
    url: str
    apply_url: str = ""
    description: str = ""
    salary_min: int | None = None
    salary_max: int | None = None
    salary_estimated: bool = False
    match_score: float = 0.0
    date_posted: str = ""
    first_seen: str = ""
    matched_skills: list[str] = field(default_factory=list)
    fill_status: str = STATUS_PENDING
    notes: str = ""
    contact_person: str = ""
    contact_email: str = ""
    applied_at: str = ""
    prepped_at: str = ""
    synced_at: str = ""
    sheet_error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> JobPosting:
        data: dict[str, Any] = {}
        for key in cls.__dataclass_fields__:
            if key in raw and raw[key] is not None:
                data[key] = raw[key]
        if "apply_url" not in data:
            data["apply_url"] = raw.get("url") or ""
        data.setdefault("matched_skills", [])
        return cls(**data)

    def salary_label(self) -> str:
        if self.salary_min and self.salary_max:
            label = f"${self.salary_min:,.0f}-${self.salary_max:,.0f}"
        elif self.salary_min:
            label = f"${self.salary_min:,.0f}+"
        elif self.salary_max:
            label = f"up to ${self.salary_max:,.0f}"
        else:
            return "unlisted"
        if self.salary_estimated:
            label += " (est.)"
        return label

    def best_url(self) -> str:
        return self.apply_url or self.url

    def sheet_row(self, date_applied: str | None = None) -> list[str]:
        """Column order expected by the Google Sheets tracker."""
        return [
            self.company,
            self.title,
            self.location or "",
            date_applied or local_today(),
            "Applied",
            self.contact_person or "",
            self.contact_email or "",
            self.notes or "",
            self.best_url(),
        ]


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def local_now() -> datetime:
    return datetime.now(LOCAL_TZ)


def local_today() -> str:
    return local_now().date().isoformat()


def load_profile(path: Path = PROFILE_PATH) -> dict[str, Any]:
    if not path.exists():
        sys.exit(f"[error] Missing profile file: {path}")

    with path.open(encoding="utf-8") as handle:
        profile = json.load(handle)

    resume = Path(profile["resume_path"])
    if not resume.is_absolute():
        resume = (ROOT / resume).resolve()
    profile["_resume_abs"] = str(resume)
    profile["_resume_exists"] = resume.is_file()

    prefs = profile.setdefault("preferences", {})
    prefs.setdefault("salary_floor", DEFAULT_SALARY_FLOOR)
    prefs.setdefault("min_match_score", DEFAULT_MIN_MATCH_SCORE)
    prefs.setdefault("country", "USA")
    prefs.setdefault("search_terms", ["Software Engineer New Grad", "Full Stack Engineer"])
    prefs.setdefault(
        "exclude_title_keywords",
        ["senior", "staff", "principal", "director", "manager", "intern", "internship"],
    )
    return profile


def resume_path_or_exit(profile: dict[str, Any]) -> str:
    if not profile.get("_resume_exists"):
        sys.exit(
            f"[error] Resume not found at {profile.get('_resume_abs')}\n"
            "Place Deethyas_Resume.pdf in this folder or update resume_path in profile.json."
        )
    return profile["_resume_abs"]


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError:
        return default


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    tmp.replace(path)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in {
            "utm_source",
            "utm_medium",
            "utm_campaign",
            "utm_term",
            "utm_content",
            "gh_src",
        }
    ]
    cleaned = parsed._replace(query=urlencode(query), fragment="")
    return urlunparse(cleaned).rstrip("/")


def make_job_id(source: str, external_id: str, url: str, company: str, title: str) -> str:
    basis = "|".join(
        [
            source.lower().strip(),
            (external_id or "").strip(),
            normalize_url(url),
            re.sub(r"\W+", "", company.lower()),
            re.sub(r"\W+", "", title.lower()),
        ]
    )
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:20]


def tokenize(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9+#/.]+", (text or "").lower()) if len(token) > 1}


def env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()


def google_sheets_id() -> str:
    return env("GOOGLE_SHEETS_ID", GOOGLE_SHEETS_ID_DEFAULT)


def service_account_path() -> Path:
    configured = env("GOOGLE_SERVICE_ACCOUNT_FILE", "credentials.json")
    path = Path(configured)
    if not path.is_absolute():
        path = ROOT / path
    return path


def load_matches() -> dict[str, Any]:
    payload = load_json(MATCHES_PATH, {"jobs": [], "generated_at": "", "new_count": 0})
    payload.setdefault("jobs", [])
    return payload


def load_match_jobs() -> list[JobPosting]:
    return [JobPosting.from_dict(raw) for raw in load_matches().get("jobs") or []]


def load_seen_jobs() -> list[JobPosting]:
    seen = load_json(SEEN_JOBS_PATH, {"jobs": {}})
    records = seen.get("jobs") or {}
    if not isinstance(records, dict):
        return []
    return [JobPosting.from_dict(raw) for raw in records.values()]


def upsert_match(job: JobPosting) -> None:
    payload = load_matches()
    jobs = payload.setdefault("jobs", [])
    replaced = False
    for index, raw in enumerate(jobs):
        if raw.get("id") == job.id:
            jobs[index] = job.to_dict()
            replaced = True
            break
    if not replaced:
        jobs.insert(0, job.to_dict())
    payload["queue_count"] = len(jobs)
    payload["updated_at"] = utc_now()
    save_json(MATCHES_PATH, payload)

    seen = load_json(SEEN_JOBS_PATH, {"jobs": {}})
    records = seen.setdefault("jobs", {})
    if isinstance(records, dict) and job.id in records:
        records[job.id].update(job.to_dict())
        seen["updated_at"] = utc_now()
        save_json(SEEN_JOBS_PATH, seen)


def update_match_status(job_id: str, **fields: Any) -> JobPosting | None:
    """Patch a queued job (and its seen_jobs twin) and return the updated record."""
    payload = load_matches()
    updated: JobPosting | None = None
    for raw in payload.get("jobs") or []:
        if raw.get("id") != job_id:
            continue
        raw.update(fields)
        updated = JobPosting.from_dict(raw)
        break
    if updated is not None:
        payload["updated_at"] = utc_now()
        save_json(MATCHES_PATH, payload)

    seen = load_json(SEEN_JOBS_PATH, {"jobs": {}})
    records = seen.setdefault("jobs", {})
    if isinstance(records, dict) and job_id in records:
        records[job_id].update(fields)
        seen["updated_at"] = utc_now()
        save_json(SEEN_JOBS_PATH, seen)
        if updated is None:
            updated = JobPosting.from_dict(records[job_id])
    return updated


def find_match(job_id: str) -> JobPosting | None:
    for job in load_match_jobs():
        if job.id == job_id:
            return job
    seen = load_json(SEEN_JOBS_PATH, {"jobs": {}})
    records = seen.get("jobs") or {}
    raw = records.get(job_id) if isinstance(records, dict) else None
    return JobPosting.from_dict(raw) if raw else None


def jobs_found_on(day: str | None = None) -> list[JobPosting]:
    day = day or local_today()
    found: list[JobPosting] = []
    for job in load_seen_jobs():
        stamp = (job.first_seen or "")[:10]
        if not stamp:
            continue
        try:
            utc_dt = datetime.fromisoformat(job.first_seen.replace("Z", "+00:00"))
            stamp = utc_dt.astimezone(LOCAL_TZ).date().isoformat()
        except ValueError:
            stamp = (job.first_seen or "")[:10]
        if stamp == day:
            found.append(job)
    return found


def count_by_status(*statuses: str) -> int:
    wanted = set(statuses)
    return sum(1 for job in load_match_jobs() if job.fill_status in wanted)


def default_worker_status() -> dict[str, Any]:
    return {
        "state": "stopped",
        "pid": None,
        "started_at": "",
        "last_heartbeat": "",
        "last_scrape_at": "",
        "last_scrape_new": 0,
        "last_scrape_error": "",
        "last_report_at": "",
        "next_report_at": "21:00 America/Chicago",
        "next_scrape_at": "",
        "jobs_today": 0,
        "queue_size": 0,
        "prepped": 0,
        "synced": 0,
        "message": "Worker is idle.",
    }


def load_worker_status() -> dict[str, Any]:
    status = default_worker_status()
    status.update(load_json(WORKER_STATUS_PATH, {}))
    return status


def save_worker_status(**fields: Any) -> dict[str, Any]:
    status = load_worker_status()
    status.update(fields)
    status["last_heartbeat"] = utc_now()
    status["jobs_today"] = len(jobs_found_on())
    status["queue_size"] = count_by_status(
        STATUS_PENDING, STATUS_PREPPING, STATUS_REVIEWING, STATUS_PREPPED
    )
    status["prepped"] = count_by_status(STATUS_PREPPED)
    status["synced"] = count_by_status(STATUS_SYNCED)
    save_json(WORKER_STATUS_PATH, status)
    return status


def status_label(fill_status: str) -> str:
    return STATUS_LABELS.get(fill_status, fill_status or "unknown")


def auto_prep_enabled() -> bool:
    raw = env("AUTO_PREP", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def auto_submit_enabled() -> bool:
    raw = env("AUTO_SUBMIT", "0").lower()
    return raw in {"1", "true", "yes", "on"}


def playwright_headless() -> bool:
    raw = env("PLAYWRIGHT_HEADLESS", "0").lower()
    return raw in {"1", "true", "yes", "on"}


def auto_prep_limit() -> int:
    raw = env("AUTO_PREP_LIMIT", "0")
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


AUTO_APPLY_HOST_MARKERS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
)


def is_automatable_apply_url(url: str) -> bool:
    """True when Playwright can fill (and optionally submit) without a login wall."""
    if not url:
        return False
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    query = parsed.query.lower()
    if "gh_jid=" in query:
        return True
    return any(marker in host for marker in AUTO_APPLY_HOST_MARKERS)


def append_log_file(path: Path, message: str) -> None:
    try:
        with path.open("a", encoding="utf-8") as handle:
            stamp = local_now().strftime("%H:%M:%S")
            lines = message.splitlines() or [""]
            for line in lines:
                handle.write(f"{stamp} {line}\n")
    except OSError:
        pass


def bot_notify(message: str) -> None:
    """Print to the bot terminal and append bot.log so the dashboard can tail it."""
    print(message)
    append_log_file(BOT_LOG_PATH, message)


def _read_bot_lock() -> dict[str, Any]:
    if not BOT_LOCK_PATH.exists():
        return {}
    try:
        raw = json.loads(BOT_LOCK_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return {"pid": None, "legacy": True}
    return raw if isinstance(raw, dict) else {"pid": None, "legacy": True}


def write_bot_lock(job_id: str | None = None, pid: int | None = None) -> None:
    payload = _read_bot_lock()
    payload.update(
        {
            "pid": pid if pid is not None else os.getpid(),
            "started_at": payload.get("started_at") or utc_now(),
            "updated_at": utc_now(),
        }
    )
    if job_id is not None:
        payload["job_id"] = job_id
    else:
        payload.setdefault("job_id", "")
    save_json(BOT_LOCK_PATH, payload)


def clear_bot_lock() -> None:
    try:
        BOT_LOCK_PATH.unlink(missing_ok=True)
    except TypeError:
        if BOT_LOCK_PATH.exists():
            BOT_LOCK_PATH.unlink()
    except OSError:
        pass


def bot_is_running() -> bool:
    payload = _read_bot_lock()
    if not payload:
        return False
    pid = payload.get("pid")
    if pid_is_alive(pid):
        return True
    clear_bot_lock()
    return False


def bot_lock_info() -> dict[str, Any]:
    return _read_bot_lock() if bot_is_running() else {}


def load_prep_queue_ids() -> list[str]:
    payload = load_json(PREP_QUEUE_PATH, {"job_ids": []})
    ids = payload.get("job_ids") or []
    if not isinstance(ids, list):
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    for job_id in ids:
        if not job_id or job_id in seen:
            continue
        seen.add(str(job_id))
        ordered.append(str(job_id))
    return ordered


def enqueue_for_prep(job_ids: list[str]) -> list[str]:
    """Append job ids to the Playwright auto-prep queue (de-duplicated, stable order)."""
    queued = load_prep_queue_ids()
    seen = set(queued)
    for job_id in job_ids:
        if not job_id or job_id in seen:
            continue
        job = find_match(job_id)
        if job is not None and job.fill_status in {
            STATUS_REVIEWING,
            STATUS_PREPPED,
            STATUS_APPLIED,
            STATUS_SYNCED,
            STATUS_SKIPPED,
            "filled",
        }:
            continue
        queued.append(job_id)
        seen.add(job_id)
    save_json(
        PREP_QUEUE_PATH,
        {"job_ids": queued, "updated_at": utc_now(), "count": len(queued)},
    )
    return queued


def take_prep_batch() -> list[str]:
    """Atomically drain the auto-prep queue and return those job ids."""
    queued = load_prep_queue_ids()
    save_json(PREP_QUEUE_PATH, {"job_ids": [], "updated_at": utc_now(), "count": 0})
    return queued


def take_prep_one() -> str | None:
    """Pop the next queued job id so only one application is in flight."""
    queued = load_prep_queue_ids()
    if not queued:
        return None
    job_id = queued[0]
    remaining = queued[1:]
    save_json(
        PREP_QUEUE_PATH,
        {"job_ids": remaining, "updated_at": utc_now(), "count": len(remaining)},
    )
    return job_id


def load_review_signal() -> dict[str, Any]:
    payload = load_json(REVIEW_SIGNAL_PATH, {})
    return payload if isinstance(payload, dict) else {}


def write_review_signal(job_id: str, state: str, **fields: Any) -> dict[str, Any]:
    payload = load_review_signal()
    payload.update(
        {
            "job_id": job_id,
            "state": state,
            "updated_at": utc_now(),
        }
    )
    payload.update(fields)
    save_json(REVIEW_SIGNAL_PATH, payload)
    return payload


def set_review_action(action: str) -> dict[str, Any]:
    """Dashboard / human signal: submitted | skip | next."""
    payload = load_review_signal()
    payload["action"] = str(action or "").strip().lower()
    payload["updated_at"] = utc_now()
    save_json(REVIEW_SIGNAL_PATH, payload)
    return payload


def clear_review_signal() -> None:
    try:
        REVIEW_SIGNAL_PATH.unlink(missing_ok=True)
    except TypeError:
        if REVIEW_SIGNAL_PATH.exists():
            REVIEW_SIGNAL_PATH.unlink()
    except OSError:
        pass


def reset_orphaned_prepping() -> int:
    """If Playwright is not running, roll crashed in-flight rows back."""
    if bot_is_running():
        return 0
    reset = 0
    for job in load_match_jobs():
        if job.fill_status == STATUS_PREPPING:
            update_match_status(job.id, fill_status=STATUS_PENDING)
            reset += 1
        elif job.fill_status == STATUS_REVIEWING:
            update_match_status(job.id, fill_status=STATUS_PREPPED)
            reset += 1
    return reset


def skip_non_automatable_jobs(job_ids: list[str] | None = None) -> int:
    """Mark login-walled / custom-portal listings as skipped so they never block auto-apply."""
    wanted = {str(job_id) for job_id in (job_ids or []) if job_id} or None
    skipped = 0
    for job in load_match_jobs():
        if wanted is not None and job.id not in wanted:
            continue
        if job.fill_status not in {STATUS_PENDING, STATUS_PREPPING, STATUS_FAILED}:
            continue
        if is_automatable_apply_url(job.best_url()):
            continue
        update_match_status(
            job.id,
            fill_status=STATUS_SKIPPED,
            notes=(
                job.notes
                or "Skipped auto-apply: not a Greenhouse/Lever/Ashby form "
                "(company portal or login required)."
            ),
        )
        skipped += 1
    return skipped


def pending_auto_apply_ids() -> list[str]:
    return [
        job.id
        for job in load_match_jobs()
        if job.fill_status == STATUS_PENDING
        and is_automatable_apply_url(job.best_url())
    ]


def spawn_auto_prep_bot() -> str:
    """Start bot.py --auto-prep. Chromium stays visible; output is always logged."""
    reset_orphaned_prepping()
    command = [sys.executable, "-u", str(ROOT / "bot.py"), "--auto-prep"]
    limit = auto_prep_limit()
    if limit > 0:
        command.extend(["--limit", str(limit)])

    log_handle = BOT_LOG_PATH.open("a", encoding="utf-8")
    kwargs: dict[str, Any] = {
        "cwd": str(ROOT),
        "stdout": log_handle,
        "stderr": log_handle,
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        kwargs["close_fds"] = False
    else:
        kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **kwargs)
    write_bot_lock(pid=process.pid)
    queued = load_prep_queue_ids()
    action = "auto-apply" if auto_submit_enabled() else "auto-prep"
    message = (
        f"Started Playwright {action} (pid {process.pid}) for {len(queued)} $150k+ role(s). "
        + (
            "Forms are filled and submitted on Greenhouse/Lever/Ashby."
            if auto_submit_enabled()
            else "One form stays open until you edit and Submit it; the next role waits."
        )
    )
    append_log_file(BOT_LOG_PATH, message)
    append_log_file(WORKER_LOG_PATH, message)
    return message


def request_auto_prep(job_ids: list[str] | None = None) -> str:
    """
    Queue automatable matches for Playwright and launch the filler if it is idle.

    Login-walled boards are marked skipped. Leftover pending Greenhouse/Lever/Ashby
    rows are included so a scrape that never launched the bot still gets applied.
    """
    if not auto_prep_enabled():
        return ""

    reset_orphaned_prepping()
    extra = [str(job_id) for job_id in (job_ids or []) if job_id]
    skip_non_automatable_jobs()
    ids: list[str] = []
    seen: set[str] = set()
    for job_id in extra + pending_auto_apply_ids():
        if not job_id or job_id in seen:
            continue
        seen.add(job_id)
        ids.append(job_id)

    queued = enqueue_for_prep(ids) if ids else load_prep_queue_ids()
    if ids and queued:
        preview = []
        for job_id in ids[:8]:
            job = find_match(job_id)
            if job:
                preview.append(f"{job.company} — {job.title}")
        extra_label = f" including {', '.join(preview)}" if preview else ""
        append_log_file(
            BOT_LOG_PATH,
            f"Queued {len(ids)} Greenhouse/Lever/Ashby role(s) for automatic Playwright "
            f"{'apply' if auto_submit_enabled() else 'prep'}{extra_label}.",
        )

    queued = load_prep_queue_ids()
    if not queued:
        return ""

    if bot_is_running():
        lock = _read_bot_lock()
        current = lock.get("job_id") or "the current listing"
        message = (
            f"{len(queued)} role(s) queued for Playwright. A window is already open "
            f"({current}); new forms will start after that listing finishes."
        )
        append_log_file(BOT_LOG_PATH, message)
        append_log_file(WORKER_LOG_PATH, message)
        return message

    return spawn_auto_prep_bot()


def load_tracker_log() -> dict[str, Any]:
    payload = load_json(TRACKER_LOG_PATH, {"rows": [], "updated_at": ""})
    payload.setdefault("rows", [])
    return payload


def append_tracker_log(row: dict[str, Any]) -> None:
    payload = load_tracker_log()
    payload["rows"] = [row] + [
        existing for existing in payload["rows"] if existing.get("job_id") != row.get("job_id")
    ]
    payload["updated_at"] = utc_now()
    save_json(TRACKER_LOG_PATH, payload)


def pid_is_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            process = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if process:
                kernel32.CloseHandle(process)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
