# Autonomous New Grad Job Hunt Engine

Production-style monitor for **high-paying New Grad / Full Stack Software Engineer** roles across **LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Greenhouse, and Lever**.

It scores postings against `profile.json` (Python, JavaScript, TypeScript, React, Next.js, Prisma, SQL, AI/ML, U.S. Provisional Patent), keeps a **$150k+** compensation floor, auto-applies on Greenhouse / Lever / Ashby via Playwright (`AUTO_SUBMIT=1`), syncs approved apps to [your Google Sheet](https://docs.google.com/spreadsheets/d/1TO5rtMymBoo64X2bld2R-HqBGlWim_m7kHNcHkICtxY/edit), and emails a digest at **9:00 PM America/Chicago**.

Use this only for roles you intend to apply to, with your own information, and within each site's terms of use.

## Architecture

| Piece | File | Role |
| --- | --- | --- |
| Candidate profile | `profile.json` | Name, UTD CS (Dec 2026), U.S. citizenship, GitHub, skills, $150k floor |
| Universal scraper | `aggregator.py` | JobSpy + Greenhouse + Lever + Simplify, salary filter, resume score, auto-queue Playwright |
| Form filler | `bot.py` | Playwright fills Greenhouse/Lever/Ashby; `AUTO_SUBMIT=1` clicks Submit |
| Sheets tracker | `tracker_sync.py` | `gspread` append after you confirm a submission |
| Digest | `reporter.py` | Markdown + SMTP, including Sheets sync status |
| Clock | `scheduler.py` | 9:00 PM America/Chicago scrape + email |
| 24/7 engine | `worker.py` | Hourly 8 AM–7 PM CT scrape + nightly schedule + auto-prep launch |
| Dashboard | `app.py` | Streamlit UI: monitor, feed, bulk-review queue, tracker |

Supporting files: `common.py`, `ats_companies.json`.

## Prerequisites

- Python 3.10+ (`python --version`)
- Resume PDF named `Deethyas_Resume.pdf` in this folder
- Gmail App Password (or other SMTP) for the nightly email
- Google Cloud service account JSON for Sheets (steps below)

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-local.txt
python -m playwright install chromium
copy .env.example .env
```

If PowerShell blocks venv activation:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

## Configure

### 1. `profile.json`

Replace placeholders before applying anywhere:

| Field | Notes |
| --- | --- |
| `email` / `phone` | Recruiter-facing contact details |
| `links.linkedin` | Full `https://` URL |
| `links.github` | `https://github.com/Deethya0715` |
| `graduation_date` / `degree` / `school` | December 2026, B.S. CS, UTD |
| `work_authorization` | U.S. citizen, authorized, no sponsorship |
| `resume_path` | `Deethyas_Resume.pdf` |
| `skills` | Used for resume-similarity scoring |
| `preferences.salary_floor` | `150000` |

### 2. Email (`.env`)

```
REPORT_TO=you@example.com
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASSWORD=your-app-password
SMTP_FROM=you@example.com
```

Gmail needs an [App Password](https://myaccount.google.com/apppasswords), not your normal login password.

`RAPIDAPI_KEY` is optional. JobSpy + Greenhouse + Lever still run without it.

### 3. Google Sheets service account

The tracker is wired to:

https://docs.google.com/spreadsheets/d/1TO5rtMymBoo64X2bld2R-HqBGlWim_m7kHNcHkICtxY/edit

1. Open [Google Cloud Console](https://console.cloud.google.com/) and create (or pick) a project.
2. Enable the **Google Sheets API** and the **Google Drive API**.
3. **IAM & Admin → Service Accounts → Create**. Skip optional permissions.
4. Open the service account → **Keys → Add key → JSON**. Save the file in this folder as `credentials.json`.
5. Open `credentials.json` and copy `client_email` (looks like `job-hunt@PROJECT.iam.gserviceaccount.com`).
6. In the spreadsheet, click **Share**, paste that email, grant **Editor**, uncheck “notify people”, Save.
7. Confirm `.env` contains:

```
GOOGLE_SHEETS_ID=1TO5rtMymBoo64X2bld2R-HqBGlWim_m7kHNcHkICtxY
GOOGLE_SERVICE_ACCOUNT_FILE=credentials.json
```

If the tracker is not the first tab, set `GOOGLE_SHEETS_WORKSHEET` to the tab name.

Each approved application appends:

`Company | Role Title | Location | Date Applied | Status (Applied) | Contact Person | Contact Email | Notes | Job Link`

Test access:

```powershell
python tracker_sync.py --test
```

## Run the dashboard (recommended)

```powershell
.\.venv\Scripts\Activate.ps1
streamlit run app.py
```

The UI has four sections:

1. **Live Monitor Status** — toggle the worker (hourly scrapes 8:00 AM–7:00 PM CT, email at 9:00 PM CT). New $150k+ matches are queued for Playwright automatically.
2. **Discovered Jobs Feed** — scored listings, salaries, and links from every board.
3. **Application Queue** — auto-prepped roles with live status. Review the paused Playwright window, click **Submit yourself**, then **Approve selected & sync** (or **Approve all ready for review**) to write Sheets rows. There is no per-job Prep button.
4. **Tracker Sync Status** — rows that landed in Google Sheets, plus failures to retry.

Metrics at the top: jobs found today, applications ready for review, monitor active/stopped.

## CLI workflow

### Scan every board

```powershell
python aggregator.py
```

Sources:

- **JobSpy** — LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs
- **Greenhouse / Lever** public board APIs (`ats_companies.json`)
- **SimplifyJobs** new-grad list
- **JSearch** if `RAPIDAPI_KEY` is set

A posting is kept only if it looks like New Grad SWE or Full Stack, is U.S./US-remote, meets the **$150k+** floor, and beats `preferences.min_match_score`.

Each **new** match is queued and handed to `bot.py --auto-prep` unless you pass `--no-auto-prep` or set `AUTO_PREP=0` in `.env`.

```powershell
python aggregator.py --skip-jobspy
python aggregator.py --no-auto-prep
```

### Fill a form (never submits)

```powershell
python bot.py "https://boards.greenhouse.io/example-company/jobs/1234567"
python bot.py --auto-prep
python bot.py --auto-prep --from-matches
python bot.py --job-id <id>
```

`--auto-prep` drains the automatic queue created by a scrape: it fills fields, uploads the resume, logs **FORM READY FOR REVIEW**, and **pauses before Submit**. After you click Submit yourself, press Enter in that console — or leave the row as prepped and bulk-approve it in the dashboard.

`--from-matches` without `--auto-prep` is the older interactive flow (confirm each listing).

### Hourly worker and 9:00 PM email

```powershell
python worker.py                 # hourly 8 AM–7 PM CT scrapes + 9:00 PM report
python scheduler.py              # 9:00 PM only
python reporter.py               # email/Markdown now
python reporter.py --no-email
```

Markdown copies land in `reports/`.

To survive logoff, create a Windows Task Scheduler task that runs:

`...\automated_job\.venv\Scripts\python.exe worker.py`

or `scheduler.py --once` daily at 9:00 PM.

## Cloud (Render / Railway)

`Procfile`:

```
web: streamlit run app.py --server.port=$PORT --server.address=0.0.0.0 --server.headless=true
worker: python worker.py
```

Set the same env vars as `.env` in the host dashboard, and upload `credentials.json` as a secret file. Playwright form filling is a **local** workflow. Cloud instances have no desktop browser. Keep `AUTO_SUBMIT=1` only on the machine where Chromium can run, or set `AUTO_PREP=0` on cloud so the worker only discovers matches.

## Auto-apply rule

`bot.py` fills Greenhouse, Lever, and Ashby forms from `profile.json` and the resume PDF. With `AUTO_SUBMIT=1` it also clicks Submit. Workday, LinkedIn Easy Apply, TikTok, Amazon, Apple, Google, and other login portals are skipped — those still need you to apply in the browser.

It will not invent essay answers or bypass CAPTCHAs. Set `AUTO_SUBMIT=0` if you want fill-only with a review pause.

## Troubleshooting

- **Resume not found** — put `Deethyas_Resume.pdf` in this folder.
- **Browser does not open** — `python -m playwright install chromium`
- **JobSpy / LinkedIn empty** — boards rate-limit. Retry later or use `--skip-jobspy`.
- **No jobs pass $150k** — unlisted salaries are estimated only for known high-comp employers.
- **Email fails** — App Password, `SMTP_USER`, and a real `REPORT_TO` / `profile.json` email.
- **Sheets sync fails** — share the spreadsheet with the service account `client_email` as Editor; enable Sheets + Drive APIs.
- **Wrong authorization answer** — change it during the review pause before you submit.

## Project layout

```
automated_job/
  app.py                Streamlit dashboard
  worker.py             hourly 8 AM–7 PM CT scrape + 9 PM scheduler
  aggregator.py         multi-board scrape / score / de-dupe / auto-queue Playwright
  bot.py                Playwright filler (AUTO_SUBMIT clicks Submit on Greenhouse/Lever/Ashby)
  tracker_sync.py       Google Sheets append
  reporter.py           Markdown + SMTP digest
  scheduler.py          9:00 PM trigger
  common.py             shared records and paths
  profile.json          candidate + skills + $150k floor
  ats_companies.json    Greenhouse + Lever board slugs
  matches.json          generated apply queue
  seen_jobs.json        generated de-dupe state
  prep_queue.json       generated Playwright auto-prep queue
  tracker_log.json      generated Sheets sync log
  bot.log               generated Playwright prep log
  reports/              generated daily Markdown
  Deethyas_Resume.pdf   your resume (you provide this)
  credentials.json      Google service account (you provide this)
  .env                  secrets (you provide this)
  Procfile              Render/Railway process list
```
