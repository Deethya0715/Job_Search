"""LLM answers for leftover application questions.

Uses OpenAI, Anthropic, or Gemini if a key is present. Never invents
work-authorization, citizenship, or employers that contradict profile.json.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import requests

from common import ANSWERS_PATH, ROOT, env, load_json, load_profile, save_json

AI_CACHE_PATH = ROOT / "ai_answer_cache.json"
COVER_LETTERS_DIR = ROOT / "cover_letters"
_TIMEOUT = 45


def llm_configured() -> bool:
    return bool(env("OPENAI_API_KEY") or env("ANTHROPIC_API_KEY") or env("GEMINI_API_KEY"))


def ai_answers_enabled() -> bool:
    raw = env("AI_ANSWERS", "1").lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return llm_configured()


def ai_cover_letters_enabled() -> bool:
    raw = env("AI_COVER_LETTERS", "1").lower()
    return raw not in {"0", "false", "no", "off"}


def _facts(profile: dict[str, Any], job: Any | None) -> str:
    auth = profile.get("work_authorization") or {}
    ident = profile.get("self_identification") or {}
    prefs = profile.get("preferences") or {}
    links = profile.get("links") or {}
    intern = (load_json(ANSWERS_PATH, {}).get("internship") or {})
    company = getattr(job, "company", None) or "this company"
    title = getattr(job, "title", None) or "this role"
    location = getattr(job, "location", None) or "United States"
    return "\n".join(
        [
            f"Candidate: {profile.get('full_name')}",
            f"Email: {profile.get('email')}",
            f"Phone: {profile.get('phone')}",
            f"Location: {profile.get('location')}",
            f"Age: {profile.get('age')}",
            f"Pronouns: {profile.get('pronouns')}",
            f"Gender: {profile.get('gender')}",
            f"School: {profile.get('school')}",
            f"Degree: {profile.get('degree')}",
            f"Graduation: {profile.get('graduation_date')}",
            f"Currently student: {profile.get('currently_student')}",
            f"Currently employed: {profile.get('currently_employed')}",
            f"Years of experience: {profile.get('years_of_experience')}",
            f"Willing to relocate: {profile.get('willing_to_relocate')}",
            f"Open to remote: {profile.get('open_to_remote')}",
            f"US citizen: {auth.get('us_citizen')}",
            f"Authorized to work in US: {auth.get('authorized_to_work')}",
            f"Requires sponsorship now or in the future: {auth.get('requires_sponsorship')}",
            f"Work auth status: {auth.get('status')}",
            f"Export control: {auth.get('export_control_status')}",
            f"Veteran: {ident.get('veteran')}",
            f"Disability: {ident.get('disability')} ({ident.get('disability_status')})",
            f"Race / ethnicity / hispanic: decline to self-identify",
            f"Languages: {', '.join(profile.get('languages') or [])}",
            f"Skills: {', '.join(profile.get('skills') or [])}",
            f"LinkedIn: {links.get('linkedin')}",
            f"GitHub: {links.get('github')}",
            f"How heard: {profile.get('how_heard')}",
            f"Previously applied: {profile.get('previously_applied')}",
            f"Conflict of interest: {profile.get('conflict_of_interest')}",
            f"Security clearance held: {(profile.get('security_clearance') or {}).get('holds_now')}",
            f"Salary floor: {prefs.get('salary_floor')}",
            f"Preferred salary: {prefs.get('preferred_salary')}",
            f"Internship: {intern.get('title')} at {intern.get('company')} "
            f"({intern.get('location')}, {intern.get('when')})",
            f"Projects to mention: Hope. Cope. Heal. EHR (Nuxt, Prisma); "
            f"SignalSafe (U.S. Provisional Patent 64/132,977); CBRE ServiceNow / GenAI.",
            f"Applying to: {title} at {company} ({location})",
        ]
    )


def _style_examples() -> str:
    bank = load_json(ANSWERS_PATH, {})
    chunks: list[str] = []
    for item in (bank.get("questions") or [])[:10]:
        answer = item.get("answer") or item.get("answer_swe") or ""
        if not answer:
            continue
        qid = item.get("id") or "sample"
        chunks.append(f"- {qid}: {answer[:420]}")
    return "\n".join(chunks)


def _cache() -> dict[str, Any]:
    payload = load_json(AI_CACHE_PATH, {"answers": {}, "cover_letters": {}})
    if not isinstance(payload, dict):
        return {"answers": {}, "cover_letters": {}}
    payload.setdefault("answers", {})
    payload.setdefault("cover_letters", {})
    return payload


def _cache_key(company: str, question: str) -> str:
    blob = f"{(company or '').lower()}::{re.sub(r'\s+', ' ', (question or '').lower())[:240]}"
    return blob


def _parse_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I | re.S).strip()
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", raw, re.S)
        if not match:
            return {}
        try:
            data = json.loads(match.group(0))
            return data if isinstance(data, dict) else {}
        except json.JSONDecodeError:
            return {}


def _openai_complete(system: str, user: str) -> str:
    key = env("OPENAI_API_KEY")
    if not key:
        return ""
    model = env("OPENAI_MODEL", "gpt-4o-mini")
    response = requests.post(
        "https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": 0.35,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    return str(response.json()["choices"][0]["message"]["content"] or "")


def _anthropic_complete(system: str, user: str) -> str:
    key = env("ANTHROPIC_API_KEY")
    if not key:
        return ""
    model = env("ANTHROPIC_MODEL", "claude-sonnet-4-5")
    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 2500,
            "temperature": 0.35,
            "system": system,
            "messages": [{"role": "user", "content": user}],
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    parts = response.json().get("content") or []
    return "".join(str(part.get("text") or "") for part in parts if isinstance(part, dict))


def _gemini_complete(system: str, user: str) -> str:
    key = env("GEMINI_API_KEY")
    if not key:
        return ""
    model = env("GEMINI_MODEL", "gemini-2.0-flash")
    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"{model}:generateContent?key={key}"
    )
    response = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"parts": [{"text": user}]}],
            "generationConfig": {
                "temperature": 0.35,
                "responseMimeType": "application/json",
            },
        },
        timeout=_TIMEOUT,
    )
    response.raise_for_status()
    parts = (((response.json().get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
    return "".join(str(part.get("text") or "") for part in parts)


def _complete(system: str, user: str) -> str:
    errors: list[str] = []
    for name, fn in (
        ("openai", _openai_complete),
        ("anthropic", _anthropic_complete),
        ("gemini", _gemini_complete),
    ):
        try:
            text = fn(system, user)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
            continue
        if text.strip():
            return text
    if errors:
        raise RuntimeError("; ".join(errors[:3]))
    raise RuntimeError("No LLM API key is set.")


def answer_questions(
    questions: list[dict[str, Any]],
    profile: dict[str, Any] | None = None,
    job: Any | None = None,
) -> dict[str, dict[str, str]]:
    """Return {id: {text, choice}} for leftover fields. Empty dict on failure."""
    if not questions or not ai_answers_enabled():
        return {}
    profile = profile or load_profile()
    company = getattr(job, "company", "") or ""
    cache = _cache()
    cached_answers: dict[str, dict[str, str]] = cache.get("answers") or {}
    pending: list[dict[str, Any]] = []
    result: dict[str, dict[str, str]] = {}

    for item in questions:
        fid = str(item.get("id") or "")
        question = str(item.get("question") or "").strip()
        if not fid or not question:
            continue
        hit = cached_answers.get(_cache_key(company, question))
        if isinstance(hit, dict) and (hit.get("text") or hit.get("choice")):
            result[fid] = {
                "text": str(hit.get("text") or ""),
                "choice": str(hit.get("choice") or ""),
            }
            continue
        pending.append(item)

    if not pending:
        return result

    payload = []
    for item in pending:
        payload.append(
            {
                "id": item.get("id"),
                "kind": item.get("kind") or "text",
                "question": item.get("question"),
                "options": (item.get("options") or [])[:24],
                "maxlength": item.get("maxlength") or 0,
            }
        )

    system = (
        "You fill leftover questions on a real job application for Deethya Janjanam. "
        "Write in first person as Deethya. Match her voice from the style examples. "
        "Return JSON only: {\"answers\": [{\"id\": \"...\", \"text\": \"...\", \"choice\": \"...\"}]}. "
        "If options are listed, set choice to the exact option text she should pick. "
        "text is the typed answer (essays, short answers, cover letters). "
        "HARD RULES you must not violate:\n"
        "- U.S. citizen, authorized to work, NEVER needs visa sponsorship now or later.\n"
        "- New-grad / early-career only. Graduation December 2026, UTD B.S. Computer Science.\n"
        "- Only employer to list is CBRE (Software Engineering Intern, Summer 2026). Do not invent jobs.\n"
        "- Disability: she has a disability. Race/ethnicity/Hispanic: decline to self-identify.\n"
        "- Not a veteran. No current U.S. security clearance.\n"
        "- If asked salary, prefer 150000 and never go below 100000.\n"
        "- How heard: Google job search unless options force LinkedIn/Indeed/other.\n"
        "- Cover letters: 3 short paragraphs, no more than 180 words.\n"
        "- Short fields: one sentence or a number. Never leave text empty if kind is text/textarea."
    )
    user = (
        "FACTS\n"
        f"{_facts(profile, job)}\n\n"
        "STYLE EXAMPLES\n"
        f"{_style_examples()}\n\n"
        "QUESTIONS\n"
        f"{json.dumps(payload, ensure_ascii=False)}"
    )

    try:
        parsed = _parse_json_object(_complete(system, user))
    except Exception as exc:
        print(f"[warn] AI answers failed: {exc}")
        return result

    rows = parsed.get("answers")
    if not isinstance(rows, list):
        return result

    for row in rows:
        if not isinstance(row, dict):
            continue
        fid = str(row.get("id") or "")
        if not fid:
            continue
        text = str(row.get("text") or "").strip()
        choice = str(row.get("choice") or "").strip()
        result[fid] = {"text": text, "choice": choice}
        question = next(
            (str(item.get("question") or "") for item in pending if item.get("id") == fid),
            "",
        )
        if question:
            cached_answers[_cache_key(company, question)] = {"text": text, "choice": choice}

    cache["answers"] = cached_answers
    try:
        save_json(AI_CACHE_PATH, cache)
    except OSError:
        pass
    return result


def _cover_letter_cache_key(job: Any | None) -> str:
    company = str(getattr(job, "company", "") or "company").strip().lower()
    title = str(getattr(job, "title", "") or "role").strip().lower()
    return f"{company}::{title}"


def _slug(text: str, limit: int = 40) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", "_", text or "").strip("_")
    return (cleaned or "role")[:limit]


def _to_winansi(text: str) -> str:
    replacements = {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2026": "...",
        "\u00a0": " ",
    }
    for src, dest in replacements.items():
        text = text.replace(src, dest)
    return text.encode("latin-1", "replace").decode("latin-1")


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _wrap_pdf_lines(text: str, width: int = 88) -> list[str]:
    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").split("\n"):
        paragraph = paragraph.strip()
        if not paragraph:
            lines.append("")
            continue
        current = ""
        for word in paragraph.split():
            trial = f"{current} {word}".strip()
            if len(trial) <= width:
                current = trial
            else:
                if current:
                    lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def write_cover_letter_pdf(path: Path, body: str, *, name: str, email: str) -> None:
    dest = Path(path)
    header = f"{name}\n{email}\n"
    lines = _wrap_pdf_lines(f"{header}\n{body.strip()}\n", 88)[:46]
    content_ops = ["BT", "/F1 11 Tf", "15 TL", "54 742 Td"]
    for index, line in enumerate(lines):
        if index:
            content_ops.append("T*")
        content_ops.append(f"({_pdf_escape(_to_winansi(line))}) Tj")
    content_ops.append("ET")
    stream = "\n".join(content_ops).encode("latin-1", "replace")

    def obj(number: int, payload: bytes) -> bytes:
        return f"{number} 0 obj\n".encode("ascii") + payload + b"\nendobj\n"

    objects = [
        obj(1, b"<< /Type /Catalog /Pages 2 0 R >>"),
        obj(2, b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>"),
        obj(
            3,
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        ),
        obj(4, b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"),
        obj(5, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"),
    ]
    header_bytes = b"%PDF-1.4\n"
    offsets = [0]
    cursor = len(header_bytes)
    for block in objects:
        offsets.append(cursor)
        cursor += len(block)
    xref = [b"xref\n0 6\n", b"0000000000 65535 f \n"]
    for offset in offsets[1:]:
        xref.append(f"{offset:010d} 00000 n \n".encode("ascii"))
    trailer = (
        b"trailer\n<< /Size 6 /Root 1 0 R >>\n"
        + f"startxref\n{cursor}\n".encode("ascii")
        + b"%%EOF\n"
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(header_bytes + b"".join(objects) + b"".join(xref) + trailer)


def _fallback_cover_letter(profile: dict[str, Any], job: Any | None) -> str:
    name = str(profile.get("full_name") or "Deethya Janjanam")
    company = str(getattr(job, "company", None) or "your team")
    title = str(getattr(job, "title", None) or "Software Engineer")
    skills = list(getattr(job, "matched_skills", None) or [])[:4]
    skill_bit = ""
    if skills:
        skill_bit = f" The posting lines up with work I already do in {', '.join(skills)}."
    return (
        f"Dear Hiring Team,\n\n"
        f"I am applying to the {title} role at {company}. I am a UT Dallas Computer Science "
        f"new graduate (December 2026) and a U.S. citizen - authorized to work with no "
        f"sponsorship now or later.{skill_bit}\n\n"
        "I ship production software, not just coursework: a Nuxt/Prisma EHR (Hope. Cope. Heal.), "
        "SignalSafe (phone-sensor turn-intent, U.S. Provisional Patent 64/132,977), and "
        "ServiceNow/GenAI platform work as a Software Engineering Intern at CBRE. I want this "
        "role because it is the same loop at a larger scale - Python, TypeScript, and systems "
        "people actually use.\n\n"
        f"I would welcome the chance to contribute as a new-grad software engineer at {company}. "
        "Thank you for your time.\n\n"
        f"Sincerely,\n{name}"
    )


def generate_cover_letter(
    profile: dict[str, Any] | None = None,
    job: Any | None = None,
) -> str:
    """Write a short cover letter tailored to the role. Uses LLM when configured."""
    if not ai_cover_letters_enabled():
        return ""
    profile = profile or load_profile()
    cache = _cache()
    letters: dict[str, str] = cache.get("cover_letters") or {}
    key = _cover_letter_cache_key(job)
    cached = str(letters.get(key) or "").strip()
    if cached:
        return cached

    letter = ""
    if llm_configured():
        company = getattr(job, "company", None) or "this company"
        title = getattr(job, "title", None) or "this role"
        description = str(getattr(job, "description", "") or "")[:1800]
        system = (
            "You write a job cover letter as Deethya Janjanam. "
            'Return JSON only: {"cover_letter": "..."}. '
            "First person, professional, concrete. 3 short paragraphs plus greeting and sign-off. "
            "About 140-180 words. No salary, no visa questions, no generic 'passionate about software' filler. "
            "HARD RULES: U.S. citizen, no sponsorship; UTD B.S. CS, December 2026; "
            "only employer is CBRE (Software Engineering Intern, Summer 2026); "
            "mention at most two of: Hope. Cope. Heal. EHR, SignalSafe patent 64/132,977, CBRE intern work. "
            "Name the company and role. Do not invent employers, awards, or GPA."
        )
        user = (
            "FACTS\n"
            f"{_facts(profile, job)}\n\n"
            "STYLE EXAMPLES\n"
            f"{_style_examples()}\n\n"
            f"ROLE\n{title} at {company}\n\n"
            "JOB DESCRIPTION (excerpt, use only if it helps you be specific)\n"
            f"{description or '(none)'}"
        )
        try:
            parsed = _parse_json_object(_complete(system, user))
            letter = str(parsed.get("cover_letter") or "").strip()
        except Exception as exc:
            print(f"[warn] AI cover letter failed: {exc}")
            letter = ""

    if not letter:
        letter = _fallback_cover_letter(profile, job)

    letters[key] = letter
    cache["cover_letters"] = letters
    try:
        save_json(AI_CACHE_PATH, cache)
    except OSError:
        pass
    return letter


def cover_letter_pdf_path(
    profile: dict[str, Any] | None = None,
    job: Any | None = None,
    body: str | None = None,
) -> str:
    """Generate (or reuse) a one-page PDF and return its absolute path."""
    profile = profile or load_profile()
    letter = (body or generate_cover_letter(profile, job)).strip()
    if not letter:
        return ""
    company = _slug(str(getattr(job, "company", "") or "company"))
    title = _slug(str(getattr(job, "title", "") or "role"))
    dest = COVER_LETTERS_DIR / f"{company}_{title}.pdf"
    write_cover_letter_pdf(
        dest,
        letter,
        name=str(profile.get("full_name") or "Deethya Janjanam"),
        email=str(profile.get("email") or ""),
    )
    return str(dest.resolve())
