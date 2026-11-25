"""
Local AI job search agent.

1. Collects remote postings nationwide plus any-arrangement postings near home
   (JobSpy, Greenhouse, Lever, USAJobs, Adzuna)
2. De-duplicates them in a local SQLite database
3. Scores each new posting against your profile with Apple's on-device model (Apple Intelligence)
4. Writes an Excel tracker; your Status/Notes edits are preserved between runs

Usage:
    ./run.sh                 # full run
    ./run.sh --skip-collect  # only score anything unscored + rewrite spreadsheet
    ./run.sh --limit 20      # score at most 20 postings this run (good for a first test)
    ./run.sh --rescore       # re-score everything (e.g. after editing your profile)
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import html
import json
import logging
import re
import sqlite3
import sys
import time
from pathlib import Path

import requests
import yaml

BASE = Path(__file__).resolve().parent
log = logging.getLogger("jobagent")

LOCAL_SHEET = "Buffalo-Niagara"
STATUSES = ["New", "Interested", "Applied", "Interviewing", "Offer", "Rejected", "Not interested"]


# --------------------------------------------------------------------------- setup

def setup_logging() -> None:
    (BASE / "logs").mkdir(exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(BASE / "logs" / "jobagent.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.setLevel(logging.INFO)
    log.addHandler(fh)
    log.addHandler(sh)


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


# --------------------------------------------------------------------------- helpers

def clean_text(raw) -> str:
    """Strip HTML and collapse whitespace."""
    if raw is None:
        return ""
    s = str(raw)
    if s.lower() == "nan":
        return ""
    s = html.unescape(s)
    s = re.sub(r"<(br|/p|/li|/div|/h\d)[^>]*>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", " ", s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()


def job_id(title: str, company: str, url: str) -> str:
    """Same title + company from different sources counts as one posting."""
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    c = re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip()
    key = f"{t}|{c}" if t and c else (url or t)
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def make_job(source, title, company, location, url, date_posted, description, salary_raw="") -> dict:
    title, company = clean_text(title), clean_text(company)
    return {
        "id": job_id(title, company, url),
        "source": source,
        "title": title,
        "company": company,
        "location": clean_text(location),
        "url": str(url or ""),
        "date_posted": str(date_posted or "")[:10] if str(date_posted or "").lower() != "nan" else "",
        "description": clean_text(description),
        "salary_raw": salary_raw or "",
    }


def fmt_salary(lo, hi, interval="") -> str:
    def ok(x):
        try:
            return x is not None and float(x) == float(x) and float(x) > 0
        except (TypeError, ValueError):
            return False
    parts = [f"${float(x):,.0f}" for x in (lo, hi) if ok(x)]
    if not parts:
        return ""
    s = " - ".join(parts)
    interval = str(interval or "")
    if interval and interval.lower() != "nan":
        s += f" / {interval}"
    return s


# --------------------------------------------------------------------------- storage

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    source TEXT, title TEXT, company TEXT, location TEXT, url TEXT,
    date_posted TEXT, description TEXT, salary_raw TEXT, first_seen TEXT,
    scored INTEGER DEFAULT 0,
    fit_score INTEGER, category TEXT, remote TEXT, salary TEXT, seniority TEXT,
    requires_bar TEXT, key_requirements TEXT, why TEXT, red_flags TEXT,
    status TEXT DEFAULT 'New', notes TEXT DEFAULT '',
    local TEXT
)
"""


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute(SCHEMA)
    cols = {r[1] for r in con.execute("PRAGMA table_info(jobs)")}
    if "local" not in cols:                       # upgrade a database from the first version
        con.execute("ALTER TABLE jobs ADD COLUMN local TEXT")
        con.commit()
    return con


def insert_jobs(con: sqlite3.Connection, jobs: list[dict]) -> int:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    new = 0
    for j in jobs:
        cur = con.execute(
            """INSERT OR IGNORE INTO jobs
               (id, source, title, company, location, url, date_posted, description, salary_raw, first_seen)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (j["id"], j["source"], j["title"], j["company"], j["location"], j["url"],
             j["date_posted"], j["description"], j["salary_raw"], now),
        )
        new += cur.rowcount
    con.commit()
    return new


# --------------------------------------------------------------------------- collectors

def title_excluded(title: str, excludes: list[str]) -> bool:
    t = (title or "").lower()
    return any(x.lower() in t for x in excludes)


def collect_jobspy(cfg: dict, local: bool = False) -> list[dict]:
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.error("python-jobspy is not installed. Run ./setup.sh again.")
        return []

    out = []
    terms = cfg.get("search_terms", [])
    for i, term in enumerate(terms):
        try:
            where = cfg.get("location", "United States")
            log.info(f"JobSpy{' (local)' if local else ''}: '{term}' near {where}")
            kwargs = dict(
                site_name=cfg.get("sites", ["indeed", "linkedin"]),
                search_term=term,
                location=where,
                results_wanted=cfg.get("results_per_search", 20),
                hours_old=cfg.get("hours_old", 72),
                country_indeed="USA",
                linkedin_fetch_description=True,
                verbose=0,
            )
            if local:
                kwargs["distance"] = cfg.get("distance_miles", 35)
            else:
                kwargs["is_remote"] = True
            df = scrape_jobs(**kwargs)
            for _, r in df.iterrows():
                out.append(make_job(
                    source=f"{r.get('site', 'jobspy')}",
                    title=r.get("title"),
                    company=r.get("company"),
                    location=r.get("location"),
                    url=r.get("job_url"),
                    date_posted=r.get("date_posted"),
                    description=r.get("description"),
                    salary_raw=fmt_salary(r.get("min_amount"), r.get("max_amount"), r.get("interval")),
                ))
            log.info(f"  -> {len(df)} results")
        except Exception as e:
            log.warning(f"  JobSpy search failed for '{term}': {e}")
        if i < len(terms) - 1:
            time.sleep(cfg.get("delay_seconds", 6))
    return out


def _title_wanted(title: str, keywords: list[str]) -> bool:
    t = (title or "").lower()
    return any(k.lower() in t for k in keywords)


def collect_greenhouse(companies: list[str], keywords: list[str]) -> list[dict]:
    out = []
    for slug in companies:
        try:
            r = requests.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs",
                             params={"content": "true"}, timeout=30)
            r.raise_for_status()
            jobs = r.json().get("jobs", [])
            kept = 0
            for j in jobs:
                if not _title_wanted(j.get("title"), keywords):
                    continue
                kept += 1
                out.append(make_job("greenhouse", j.get("title"), slug,
                                    (j.get("location") or {}).get("name"), j.get("absolute_url"),
                                    j.get("updated_at"), j.get("content")))
            log.info(f"Greenhouse {slug}: {kept}/{len(jobs)} kept")
        except Exception as e:
            log.warning(f"Greenhouse {slug} failed: {e}")
    return out


def collect_lever(companies: list[str], keywords: list[str]) -> list[dict]:
    out = []
    for slug in companies:
        try:
            r = requests.get(f"https://api.lever.co/v0/postings/{slug}", params={"mode": "json"}, timeout=30)
            r.raise_for_status()
            jobs = r.json()
            kept = 0
            for j in jobs:
                if not _title_wanted(j.get("text"), keywords):
                    continue
                kept += 1
                created = j.get("createdAt")
                date = dt.datetime.fromtimestamp(created / 1000).date() if created else ""
                loc = (j.get("categories") or {}).get("location", "")
                if j.get("workplaceType"):
                    loc = f"{loc} ({j['workplaceType']})"
                out.append(make_job("lever", j.get("text"), slug, loc, j.get("hostedUrl"),
                                    date, j.get("descriptionPlain") or j.get("description")))
            log.info(f"Lever {slug}: {kept}/{len(jobs)} kept")
        except Exception as e:
            log.warning(f"Lever {slug} failed: {e}")
    return out


def collect_usajobs(cfg: dict) -> list[dict]:
    if not cfg.get("api_key") or not cfg.get("email"):
        log.warning("USAJobs enabled but api_key/email missing in config.yaml; skipping.")
        return []
    headers = {"Host": "data.usajobs.gov", "User-Agent": cfg["email"], "Authorization-Key": cfg["api_key"]}
    out = []
    for kw in cfg.get("keywords", []):
        try:
            params = {"Keyword": kw, "ResultsPerPage": 50, "DatePosted": 7}
            if cfg.get("remote_only", True):
                params["RemoteIndicator"] = "True"
            r = requests.get("https://data.usajobs.gov/api/search", headers=headers, params=params, timeout=30)
            r.raise_for_status()
            items = r.json().get("SearchResult", {}).get("SearchResultItems", [])
            for it in items:
                d = it.get("MatchedObjectDescriptor", {})
                pay = (d.get("PositionRemuneration") or [{}])[0]
                details = (d.get("UserArea") or {}).get("Details", {})
                desc = "\n".join(filter(None, [details.get("JobSummary"), d.get("QualificationSummary")]))
                out.append(make_job("usajobs", d.get("PositionTitle"), d.get("OrganizationName"),
                                    d.get("PositionLocationDisplay"), d.get("PositionURI"),
                                    d.get("PublicationStartDate"), desc,
                                    fmt_salary(pay.get("MinimumRange"), pay.get("MaximumRange"),
                                               pay.get("Description", ""))))
            log.info(f"USAJobs '{kw}': {len(items)} results")
        except Exception as e:
            log.warning(f"USAJobs '{kw}' failed: {e}")
    return out


def collect_adzuna(cfg: dict) -> list[dict]:
    if not cfg.get("app_id") or not cfg.get("app_key"):
        log.warning("Adzuna enabled but app_id/app_key missing in config.yaml; skipping.")
        return []
    out = []
    for kw in cfg.get("keywords", []):
        try:
            r = requests.get("https://api.adzuna.com/v1/api/jobs/us/search/1", params={
                "app_id": cfg["app_id"], "app_key": cfg["app_key"], "what": kw,
                "results_per_page": 50, "max_days_old": cfg.get("max_days_old", 3),
                "content-type": "application/json"}, timeout=30)
            r.raise_for_status()
            results = r.json().get("results", [])
            for j in results:
                out.append(make_job("adzuna", j.get("title"), (j.get("company") or {}).get("display_name"),
                                    (j.get("location") or {}).get("display_name"), j.get("redirect_url"),
                                    j.get("created"), j.get("description"),
                                    fmt_salary(j.get("salary_min"), j.get("salary_max"))))
            log.info(f"Adzuna '{kw}': {len(results)} results")
        except Exception as e:
            log.warning(f"Adzuna '{kw}' failed: {e}")
    return out


def collect_all(cfg: dict) -> list[dict]:
    src = cfg.get("sources", {})
    kw = src.get("company_boards_title_keywords", [])
    jobs: list[dict] = []
    if src.get("jobspy", {}).get("enabled"):
        jobs += collect_jobspy(src["jobspy"])
    if src.get("jobspy_local", {}).get("enabled"):
        jobs += collect_jobspy(src["jobspy_local"], local=True)
    if src.get("greenhouse", {}).get("enabled"):
        jobs += collect_greenhouse(src["greenhouse"].get("companies", []), kw)
    if src.get("lever", {}).get("enabled"):
        jobs += collect_lever(src["lever"].get("companies", []), kw)
    if src.get("usajobs", {}).get("enabled"):
        jobs += collect_usajobs(src["usajobs"])
    if src.get("adzuna", {}).get("enabled"):
        jobs += collect_adzuna(src["adzuna"])

    excludes = cfg.get("exclude_title_keywords", [])
    before = len(jobs)
    jobs = [j for j in jobs if j["title"] and not title_excluded(j["title"], excludes)]
    log.info(f"Collected {before} postings, {len(jobs)} after title pre-filter")
    return jobs


# --------------------------------------------------------------------------- LLM scoring

SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "integer", "minimum": 1, "maximum": 10},
        "category": {"type": "string", "enum": ["Patent / IP", "Consulting", "Analyst", "R&D / Technical", "Other"]},
        "remote": {"type": "string", "enum": ["Fully remote", "Hybrid", "Onsite", "Unclear"]},
        "in_local_area": {"type": "string", "enum": ["Yes", "No", "Unclear"]},
        "salary": {"type": "string"},
        "seniority": {"type": "string"},
        "requires_bar_admission": {"type": "string", "enum": ["Yes", "No", "Not mentioned"]},
        "key_requirements": {"type": "string"},
        "why": {"type": "string"},
        "red_flags": {"type": "string"},
    },
    "required": ["fit_score", "category", "remote", "in_local_area", "salary", "seniority",
                 "requires_bar_admission", "key_requirements", "why", "red_flags"],
}

SYSTEM_PROMPT = """You are a careful career advisor screening job postings for one specific candidate.

CANDIDATE PROFILE:
{profile}

Read the job posting and return JSON only.

Scoring rubric for fit_score (1-10):
- 9-10: Role is remote, entry level, and requires a background in chemical engineering or engineering in general. Candidate meets the core requirements.
- 7-8: Role is remote or hybrid, entry level, and in an adjacent technical domain.
- 5-6: Role is remote or hybrid with plausible but meaningful gaps (different science domain, mostly business role).
- 3-4: Weak fit (requires a JD or bar admission as an attorney, unrelated field, or very senior).
- 1-2: Not relevant.
Location rules (from the profile): fully remote roles anywhere are fine. Roles in the candidate's
local area are fine as remote or hybrid. If a role is Onsite or Hybrid
and NOT in the local area, fit_score must be 3 or lower.
If it requires a JD / attorney bar admission, fit_score must be 4 or lower.
Never invent facts that are not in the posting.

Field guidance:
- in_local_area: "Yes" if the job's work location is in the candidate's local area named in the
  profile, "No" if it is clearly elsewhere (or remote with no local office), else "Unclear".
- salary: as stated in the posting, else "Not listed".
- seniority: e.g. "Entry", "Mid", "Senior", "Director", with years of experience if stated.
- requires_bar_admission: "Yes" only if an attorney/state bar admission or JD is REQUIRED.
  (USPTO patent bar registration is not attorney bar admission; mention it in key_requirements.)
- key_requirements: the 3-5 most important requirements, semicolon separated.
- why: one or two sentences on why it does or doesn't fit THIS candidate.
- red_flags: anything concerning (vague commission-only pay, staffing spam, location restrictions), or "None"."""


def _enum(field: str) -> list[str]:
    return SCORE_SCHEMA["properties"][field]["enum"]


def _build_generable(fm):
    """The same fields as SCORE_SCHEMA, in the form Apple's SDK uses for structured output."""
    @fm.generable("Fit assessment of one job posting for the candidate")
    class JobFit:
        fit_score: int = fm.guide("Overall fit, 1 to 10, per the rubric", range=(1, 10))
        category: str = fm.guide("Role family", anyOf=_enum("category"))
        remote: str = fm.guide("Work arrangement", anyOf=_enum("remote"))
        in_local_area: str = fm.guide("Is the work location in the candidate's local area?",
                                      anyOf=_enum("in_local_area"))
        salary: str = fm.guide("Pay as stated in the posting, else Not listed")
        seniority: str = fm.guide("Entry, Mid, Senior or Director, with years of experience if stated")
        requires_bar_admission: str = fm.guide("Is an attorney bar admission or JD required?",
                                               anyOf=_enum("requires_bar_admission"))
        key_requirements: str = fm.guide("The 3-5 most important requirements, semicolon separated")
        why: str = fm.guide("One or two sentences on why it does or doesn't fit this candidate")
        red_flags: str = fm.guide("Anything concerning, or None")
    return JobFit


class AppleModel:
    """Apple's on-device Foundation Model (Apple Intelligence), via Apple's Python SDK."""

    name = "Apple on-device model"

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.fm = None

    def ok(self) -> bool:
        if sys.platform != "darwin":
            log.error("Apple's on-device model only runs on macOS.")
            return False
        try:
            import apple_fm_sdk as fm
        except ImportError:
            log.error("apple-fm-sdk is not installed. Run ./setup.sh again.")
            return False
        try:
            ok, why = fm.SystemLanguageModel().is_available()
        except Exception as e:
            ok, why = False, e
        if not ok:
            log.error(f"Apple Intelligence model not available ({why}). Turn on Apple Intelligence in "
                      f"System Settings and wait for its download to finish.")
            return False
        self.fm = fm
        self.JobFit = _build_generable(fm)
        return True

    async def score_job(self, job: sqlite3.Row) -> dict | None:
        fm = self.fm
        # Apple's model has a ~4,000-token window per request (prompt + posting + answer).
        limit = self.cfg.get("max_description_chars", 3500)
        temp = (self.cfg.get("apple") or {}).get("temperature", 0.1)
        for attempt in (1, 2, 3):
            desc = (job["description"] or "")[:limit]
            posting = (f"TITLE: {job['title']}\nCOMPANY: {job['company']}\nLOCATION: {job['location']}\n"
                       f"SALARY (from listing): {job['salary_raw'] or 'n/a'}\nSOURCE: {job['source']}\n\n"
                       f"DESCRIPTION:\n{desc or '(no description available - judge from the title only and say so)'}")
            session = fm.LanguageModelSession(instructions=SYSTEM_PROMPT.format(profile=self.cfg["profile"]))
            try:
                r = await session.respond(posting, generating=self.JobFit,
                                          options=fm.GenerationOptions(temperature=temp))
                data = {k: getattr(r, k) for k in SCORE_SCHEMA["required"]}
                data["fit_score"] = max(1, min(10, int(data.get("fit_score") or 1)))
                return data
            except fm.ExceededContextWindowSizeError:
                limit //= 2
                log.info(f"  posting too long for the model; retrying with {limit} characters")
            except (fm.GuardrailViolationError, fm.RefusalError) as e:
                log.warning(f"  model declined to rate '{job['title']}' ({type(e).__name__}); "
                            f"scored 5 so you can read it yourself")
                return {"fit_score": 5, "category": "Other", "remote": "Unclear", "in_local_area": "Unclear",
                        "salary": job["salary_raw"] or "Not listed", "seniority": "",
                        "requires_bar_admission": "Not mentioned", "key_requirements": "",
                        "why": "Apple's model declined to rate this posting; please read it yourself.",
                        "red_flags": "Not rated by the model"}
            except Exception as e:
                log.warning(f"  scoring attempt {attempt} failed for '{job['title']}': {e}")
        return None


def is_local(cfg: dict, location: str, llm_answer: str) -> str:
    loc = (location or "").lower()
    if any(k.lower() in loc for k in cfg.get("local_area_keywords", [])):
        return "Yes"
    return "Yes" if llm_answer == "Yes" else "No"


def score_pending(con: sqlite3.Connection, cfg: dict, model: AppleModel, limit: int | None) -> int:
    rows = con.execute("SELECT * FROM jobs WHERE scored = 0 ORDER BY first_seen DESC").fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        log.info("Nothing new to score.")
        return 0
    log.info(f"Scoring {len(rows)} postings with {model.name}...")
    return asyncio.run(_score_rows(con, cfg, model, rows))


async def _score_rows(con, cfg, model, rows) -> int:
    done = 0
    t0 = time.time()
    for i, job in enumerate(rows, 1):
        res = await model.score_job(job)
        if res is None:
            continue
        con.execute(
            """UPDATE jobs SET scored=1, fit_score=?, category=?, remote=?, local=?, salary=?,
               seniority=?, requires_bar=?, key_requirements=?, why=?, red_flags=? WHERE id=?""",
            (res["fit_score"], res["category"], res["remote"],
             is_local(cfg, job["location"], res.get("in_local_area", "No")),
             res["salary"], res["seniority"],
             res["requires_bar_admission"], res["key_requirements"], res["why"], res["red_flags"], job["id"]),
        )
        con.commit()
        done += 1
        log.info(f"  [{i}/{len(rows)}] {res['fit_score']:>2}/10  {job['title'][:60]} @ {job['company'][:30]}")
    log.info(f"Scored {done} in {time.time() - t0:.0f}s")
    return done


# --------------------------------------------------------------------------- spreadsheet

COLUMNS = [  # (header, db field or special, width)
    ("Fit", "fit_score", 6),
    ("Title", "title", 38),
    ("Company", "company", 24),
    ("Category", "category", 14),
    ("Remote", "remote", 12),
    ("Local", "local", 7),
    ("Salary", "salary", 18),
    ("Seniority", "seniority", 16),
    ("JD/Bar req.", "requires_bar", 11),
    ("Why it fits", "why", 60),
    ("Red flags", "red_flags", 30),
    ("Key requirements", "key_requirements", 50),
    ("Status", "status", 14),
    ("Notes", "notes", 30),
    ("Link", "url", 10),
    ("Location", "location", 20),
    ("Source", "source", 10),
    ("Posted", "date_posted", 11),
    ("First seen", "first_seen", 16),
    ("ID", "id", 18),
]


def sync_edits_from_sheet(con: sqlite3.Connection, xlsx: Path) -> None:
    """Pull the Status/Notes you typed in Excel back into the database before rewriting."""
    if not xlsx.exists():
        return
    from openpyxl import load_workbook
    try:
        wb = load_workbook(xlsx, read_only=True)
    except Exception as e:
        log.warning(f"Couldn't read existing spreadsheet to keep your edits ({e}).")
        return
    # Compare against what was written last time, so an edit on one sheet isn't
    # undone by the unchanged copy of the same row on the other sheet.
    saved = {r["id"]: (r["status"] or "New", r["notes"] or "")
             for r in con.execute("SELECT id, status, notes FROM jobs")}
    edits: dict[str, dict] = {}
    for name in ("All Jobs", "Top Matches", LOCAL_SHEET):
        if name not in wb.sheetnames:
            continue
        rows = wb[name].iter_rows(values_only=True)
        header = next(rows, None)
        if not header:
            continue
        idx = {h: i for i, h in enumerate(header)}
        if not {"ID", "Status", "Notes"} <= idx.keys():
            continue
        for row in rows:
            jid = row[idx["ID"]]
            if not jid or jid not in saved:
                continue
            status = str(row[idx["Status"]] or "New")
            notes = str(row[idx["Notes"]] or "")
            old_status, old_notes = saved[jid]
            if status != old_status:
                edits.setdefault(jid, {})["status"] = status
            if notes != old_notes:
                edits.setdefault(jid, {})["notes"] = notes
    wb.close()
    for jid, e in edits.items():
        status = e.get("status", saved[jid][0])
        notes = e.get("notes", saved[jid][1])
        con.execute("UPDATE jobs SET status=?, notes=? WHERE id=?", (status, notes, jid))
    con.commit()
    if edits:
        log.info(f"Kept Status/Notes edits on {len(edits)} postings from the spreadsheet")


def write_spreadsheet(con: sqlite3.Connection, cfg: dict, xlsx: Path, run_started: str) -> Path:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
    from openpyxl.worksheet.datavalidation import DataValidation

    rows = con.execute(
        "SELECT * FROM jobs WHERE scored=1 ORDER BY fit_score DESC, first_seen DESC").fetchall()
    min_top = cfg.get("min_score_for_top", 7)
    closed = ("Not interested", "Rejected")

    def location_ok(r) -> bool:
        return r["local"] == "Yes" or r["remote"] not in ("Onsite", "Hybrid")

    top = [r for r in rows if r["fit_score"] >= min_top and location_ok(r) and r["status"] not in closed]
    local_rows = [r for r in rows if r["local"] == "Yes"]

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="1F4E78")
    green = PatternFill("solid", fgColor="C6EFCE")
    yellow = PatternFill("solid", fgColor="FFEB9C")
    new_fill = PatternFill("solid", fgColor="DDEBF7")
    link_font = Font(color="0563C1", underline="single")
    wrap = Alignment(wrap_text=True, vertical="top")
    top_align = Alignment(vertical="top")

    wb = Workbook()
    wb.remove(wb.active)

    def build(sheet_name: str, data: list[sqlite3.Row]):
        ws = wb.create_sheet(sheet_name)
        for c, (h, _, w) in enumerate(COLUMNS, 1):
            cell = ws.cell(row=1, column=c, value=h)
            cell.font, cell.fill = header_font, header_fill
            ws.column_dimensions[get_column_letter(c)].width = w
        for r_i, job in enumerate(data, 2):
            is_new = (job["first_seen"] or "") >= run_started
            for c, (h, field, _) in enumerate(COLUMNS, 1):
                val = job[field]
                cell = ws.cell(row=r_i, column=c)
                if field == "url":
                    cell.value = "Open" if val else ""
                    if val:
                        cell.hyperlink = val
                        cell.font = link_font
                else:
                    cell.value = val
                cell.alignment = wrap if field in ("why", "key_requirements", "red_flags", "title") else top_align
                if is_new and field == "title":
                    cell.fill = new_fill
                    cell.font = Font(bold=True)
            score_cell = ws.cell(row=r_i, column=1)
            score_cell.font = Font(bold=True)
            if job["fit_score"] >= 8:
                score_cell.fill = green
            elif job["fit_score"] >= 6:
                score_cell.fill = yellow
        last = max(len(data) + 1, 2)
        ws.freeze_panes = "C2"
        ws.auto_filter.ref = f"A1:{get_column_letter(len(COLUMNS))}{last}"
        status_col = get_column_letter([h for h, _, _ in COLUMNS].index("Status") + 1)
        dv = DataValidation(type="list", formula1='"' + ",".join(STATUSES) + '"', allow_blank=True)
        ws.add_data_validation(dv)
        dv.add(f"{status_col}2:{status_col}{max(last, 500)}")
        ws.column_dimensions[get_column_letter(len(COLUMNS))].hidden = True   # ID column

    build("Top Matches", top)
    build(LOCAL_SHEET, local_rows)
    build("All Jobs", rows)

    info = wb.create_sheet("About")
    info["A1"], info["A1"].font = "Job Agent tracker", Font(bold=True, size=14)
    lines = [
        f"Last updated: {dt.datetime.now():%Y-%m-%d %H:%M}",
        "Model: Apple on-device model (Apple Intelligence)",
        f"Top Matches = fit score >= {min_top}; fully remote anywhere, or any arrangement in "
        f"Buffalo-Niagara; not marked Rejected/Not interested.",
        f"{LOCAL_SHEET} = every Buffalo-Niagara posting (remote, hybrid or onsite), best first.",
        "Highlighted titles = new since the last run.",
        "Edit the Status and Notes columns freely; they are kept on the next run.",
        "Close this file before the agent runs, or it will save a dated copy instead.",
    ]
    for i, line in enumerate(lines, 3):
        info.cell(row=i, column=1, value=line)
    info.column_dimensions["A"].width = 90

    try:
        if (xlsx.parent / f"~${xlsx.name}").exists():   # Excel for Mac has it open
            raise PermissionError
        wb.save(xlsx)
        return xlsx
    except PermissionError:
        alt = xlsx.with_name(f"{xlsx.stem}_{dt.datetime.now():%Y%m%d_%H%M}{xlsx.suffix}")
        wb.save(alt)
        log.warning(f"{xlsx.name} is open in Excel; saved to {alt.name} instead. "
                    f"Status edits made in the copy won't carry over; make them in {xlsx.name}.")
        return alt


# --------------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description="Local AI job search agent")
    ap.add_argument("--config", default=str(BASE / "config.yaml"))
    ap.add_argument("--skip-collect", action="store_true", help="don't fetch new postings")
    ap.add_argument("--limit", type=int, default=None, help="max postings to score this run")
    ap.add_argument("--rescore", action="store_true", help="re-score every posting")
    args = ap.parse_args()

    setup_logging()
    cfg = load_config(Path(args.config))
    run_started = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    db_path = BASE / cfg.get("database", "jobs.db")
    xlsx = BASE / cfg.get("spreadsheet", "job_matches.xlsx")

    con = open_db(db_path)
    sync_edits_from_sheet(con, xlsx)

    if args.rescore:
        con.execute("UPDATE jobs SET scored=0")
        con.commit()

    if not args.skip_collect:
        jobs = collect_all(cfg)
        new = insert_jobs(con, jobs)
        log.info(f"{new} new postings added to the database")

    model = AppleModel(cfg)
    if model.ok():
        score_pending(con, cfg, model, args.limit)
    else:
        log.error("Skipping scoring; unscored postings will be scored on the next run.")

    out = write_spreadsheet(con, cfg, xlsx, run_started)
    n_top = sum(1 for _ in con.execute(
        "SELECT 1 FROM jobs WHERE scored=1 AND fit_score>=? "
        "AND (local='Yes' OR remote NOT IN ('Onsite','Hybrid')) "
        "AND status NOT IN ('Not interested','Rejected')",
        (cfg.get("min_score_for_top", 7),)))
    n_local = con.execute("SELECT COUNT(*) FROM jobs WHERE scored=1 AND local='Yes'").fetchone()[0]
    log.info(f"Done. Spreadsheet: {out}  ({n_top} top matches, {n_local} Buffalo-Niagara postings)")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
