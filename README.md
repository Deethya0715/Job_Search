# Autonomous New Grad Job Hunt Engine

Production-style monitor for **high-paying New Grad / Full Stack Software Engineer** roles across **LinkedIn, Indeed, Glassdoor, ZipRecruiter, Google Jobs, Greenhouse, and Lever**.

It scores postings against `profile.json` (Python, JavaScript, TypeScript, React, Next.js, Prisma, SQL, AI/ML, U.S. Provisional Patent), keeps a **$150k+** compensation floor, fills applications in a visible Playwright window, **never clicks Submit**, syncs approved apps to [your Google Sheet](https://docs.google.com/spreadsheets/d/1TO5rtMymBoo64X2bld2R-HqBGlWim_m7kHNcHkICtxY/edit), and emails a digest at **9:00 PM America/Chicago**.

Use this only for roles you intend to apply to, with your own information, and within each site's terms of use.

## Architecture

| Piece | File | Role |
| --- | --- | --- |
| Candidate profile | `profile.json` | Name, UTD CS (Dec 2026), U.S. citizenship, GitHub, skills, $150k floor |
| Universal scraper | `aggregator.py` | JobSpy + Greenhouse + Lever + Simplify, salary filter, resume score, `seen_jobs.json` |
| Form filler | `bot.py` | Playwright `headless=False`, fills fields, uploads resume, **stops before Submit** |
| Sheets tracker | `tracker_sync.py` | `gspread` append after you confirm a submission |
| Digest | `reporter.py` | Markdown + SMTP, including Sheets sync status |
| Clock | `scheduler.py` | 9:00 PM America/Chicago scrape + email |
| 24/7 engine | `worker.py` | Background scrape loop + nightly schedule |
| Dashboard | `app.py` | Streamlit UI: monitor, feed, queue, tracker |

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
python -m pip install -r requirements.txt
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

1. **Live Monitor Status** — toggle the 24/7 worker (scrape every 45 minutes, email at 9:00 PM CT).
2. **Discovered Jobs Feed** — scored listings, salaries, and links from every board.
3. **Application Queue** — **Prep with Playwright** fills the form and pauses. After you click Submit yourself, **I submitted — sync tracker** writes the Sheets row.
4. **Tracker Sync Status** — rows that landed in Google Sheets, plus failures to retry.

Metrics at the top: jobs found today, applications prepped, monitor active/stopped.

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

```powershell
python aggregator.py --skip-jobspy
```

### Fill a form (never submits)

```powershell
python bot.py "https://boards.greenhouse.io/example-company/jobs/1234567"
python bot.py --from-matches
python bot.py --job-id <id>
```

After fields are filled you will see `REVIEW REQUIRED`. Review the page, click **Submit yourself**, press Enter, then answer **y** to sync Google Sheets.

### 24/7 worker and 9:00 PM email

```powershell
python worker.py                 # scrape loop + 9:00 PM report
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

Set the same env vars as `.env` in the host dashboard, and upload `credentials.json` as a secret file. Playwright form filling (`headless=False`) is a **local** workflow — cloud instances have no visible browser for you to review before Submit.

## Safety rule

`bot.py` **fills fields only**. It does not click Submit, Apply, or Send Application. Cookie banners and resume Attach buttons are the only clicks it makes.

It will not invent essay answers, bypass CAPTCHAs or logins, or apply while you are away. LinkedIn Easy Apply and some Workday portals still need a manual login during the pause.

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
  worker.py             24/7 scrape + 9 PM scheduler
  aggregator.py         multi-board scrape / score / de-dupe
  bot.py                Playwright filler (no submit)
  tracker_sync.py       Google Sheets append
  reporter.py           Markdown + SMTP digest
  scheduler.py          9:00 PM trigger
  common.py             shared records and paths
  profile.json          candidate + skills + $150k floor
  ats_companies.json    Greenhouse + Lever board slugs
  matches.json          generated apply queue
  seen_jobs.json        generated de-dupe state
  tracker_log.json      generated Sheets sync log
  reports/              generated daily Markdown
  Deethyas_Resume.pdf   your resume (you provide this)
  credentials.json      Google service account (you provide this)
  .env                  secrets (you provide this)
  Procfile              Render/Railway process list
```
