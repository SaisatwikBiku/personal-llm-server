#!/usr/bin/env python3
"""Job search: find openings, score them against Sai's resume, prepare applications.

server.py runs this on a schedule inside the panel process. It never submits an
application. It prepares the answers and Sai applies through the posting link.

Network access is limited to URLs this module builds itself: the public job board
APIs of Greenhouse, Lever and Ashby, and the local SearXNG. Model output never
becomes a URL or request data, and no personal data leaves the machine.

Files in JOBS_DIR (default /home/agentd/jobs, which the agent user can't read):
    config.json   roles, companies, filters, schedule (deploy/jobs-config.example.json)
    profile.json  facts for application forms (deploy/jobs-profile.example.json)
    resume.txt    plain-text resume used for scoring and drafts
    jobs.json     everything found so far, written by this module
"""
import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import ollama

import agent as core

JOBS_DIR = Path(os.environ.get("AGENT_JOBS_DIR", "/home/agentd/jobs"))
DB_FILE = JOBS_DIR / "jobs.json"
USER_AGENT = "Mozilla/5.0 (local-agent job search)"
MAX_DESC_CHARS = 6000     # stored per job, for the panel
SCORE_DESC_CHARS = 2500   # posting text sent to the model when scoring
DRAFT_DESC_CHARS = 1500   # posting text sent to the model when drafting
MAX_DISCOVERED = 60       # companies found through search, most recent kept
SEEN_DAYS = 90            # forget skipped openings after this; they are re-checked if still listed
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")

DEFAULTS = {
    "timezone": "America/New_York",
    "run_at": "01:00",
    "digest_at": "08:00",
    "roles": ["software engineer", "software developer", "full stack", "back end",
              "data engineer", "database administrator", "database engineer", "database reliability", "swe"],
    "exclude_titles": ["senior", "sr", "staff", "principal", "lead", "manager", "director",
                       "head", "vp", "architect", "distinguished", "fellow", "intern",
                       "internship", "iii", "iv"],
    "search_roles": ["software engineer new grad", "backend engineer", "full stack engineer",
                     "data engineer", "database administrator"],
    "search": True,
    "companies": {"greenhouse": [], "lever": [], "ashby": []},
    "max_years": 4,
    "exclude_no_sponsorship": False,
    "max_scored_per_run": 200,
    "draft_model": "qwen3:8b",  # the 4B invented project details in drafts; the 8B stuck to the resume
    "max_drafts_per_run": 15,
    "good_score": 60,
    "strong_score": 75,
    "skills": [],
}

db_lock = threading.Lock()
run_lock = threading.Lock()
progress = {"running": False, "step": "", "done": 0, "total": 0}


# ---------- files ----------

def load_json(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.chmod(0o600)
    os.replace(tmp, path)


def configured():
    return (JOBS_DIR / "config.json").exists() and (JOBS_DIR / "resume.txt").exists()


def load_config():
    return {**DEFAULTS, **load_json(JOBS_DIR / "config.json", {})}


def load_db():
    db = load_json(DB_FILE, {})
    db.setdefault("jobs", {})
    db.setdefault("seen", {})
    db.setdefault("meta", {})
    db["meta"].setdefault("discovered", {})
    return db


def update_db(fn):
    """Load, change and save jobs.json under the lock; return fn's result."""
    with db_lock:
        db = load_db()
        result = fn(db)
        save_json(DB_FILE, db)
        return result


def today(cfg):
    return datetime.now(ZoneInfo(cfg["timezone"]))


def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------- text helpers ----------

def html_to_text(raw):
    text = html.unescape(raw or "")  # Greenhouse double-escapes its HTML
    text = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>|</(p|li|div|h\d)>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def norm_title(title):
    t = title.lower().replace("fullstack", "full stack").replace("backend", "back end")
    t = re.sub(r"(?<![c+])\+", " ", t)  # "Staff+" is staff; "C++" stays
    return " " + re.sub(r"[^a-z0-9+#]+", " ", t) + " "


def title_ok(title, cfg):
    t = norm_title(title)
    roles = [norm_title(r) for r in cfg["roles"]]
    if not any(r in t for r in roles):
        return False
    return not any(norm_title(x) in t for x in cfg["exclude_titles"])


US_STATES = ("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE "
             "NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC").split()
US_RE = re.compile(
    r"united states|\busa\b|\bu\.s\.|\bus\b|\bnyc\b|new york|san francisco|bay area|seattle|"
    r"boston|austin|chicago|los angeles|denver|atlanta|miami|dallas|houston|phoenix|"
    r"san diego|san jose|palo alto|mountain view|menlo park|sunnyvale|redmond|bellevue|"
    r"washington,? d\.?c|philadelphia|pittsburgh|portland|salt lake|raleigh|minneapolis|"
    r"detroit|albany|brooklyn|foster city|san mateo|redwood city|"
    r"alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|florida|"
    r"georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|maine|maryland|"
    r"massachusetts|michigan|minnesota|mississippi|missouri|montana|nebraska|nevada|"
    r"new hampshire|new jersey|new mexico|north carolina|north dakota|ohio|oklahoma|oregon|"
    r"pennsylvania|rhode island|south carolina|south dakota|tennessee|texas|utah|vermont|"
    r"virginia|wisconsin|wyoming", re.I)
STATE_RE = re.compile(r"(?:,|-|\()\s*(" + "|".join(US_STATES) + r")\b")
NON_US_RE = re.compile(
    r"canada|toronto|vancouver|montreal|united kingdom|\buk\b|london|england|ireland|dublin|"
    r"germany|berlin|munich|france|paris|spain|madrid|barcelona|netherlands|amsterdam|poland|"
    r"warsaw|india|bangalore|bengaluru|hyderabad|pune|singapore|japan|tokyo|korea|seoul|"
    r"australia|sydney|melbourne|mexico|brazil|argentina|israel|tel aviv|switzerland|zurich|"
    r"sweden|stockholm|denmark|copenhagen|norway|oslo|finland|helsinki|portugal|lisbon|italy|"
    r"milan|belgium|brussels|austria|vienna|czech|prague|hungary|budapest|romania|bucharest|"
    r"bulgaria|sofia|serbia|belgrade|greece|athens|turkey|istanbul|ukraine|kyiv|russia|\bcis\b|"
    r"estonia|tallinn|lithuania|latvia|armenia|yerevan|egypt|cairo|nigeria|lagos|kenya|nairobi|"
    r"south africa|cape town|\buae\b|dubai|abu dhabi|saudi|riyadh|qatar|pakistan|karachi|"
    r"lahore|bangladesh|dhaka|sri lanka|philippines|manila|vietnam|indonesia|jakarta|malaysia|"
    r"kuala lumpur|thailand|bangkok|china|beijing|shanghai|shenzhen|hong kong|taiwan|taipei|"
    r"new zealand|auckland|chile|santiago|colombia|bogota|medellin|peru|lima|costa rica|"
    r"uruguay|montevideo|emea|apac|latam|europe|asia", re.I)


def location_ok(loc):
    loc = loc or ""
    if US_RE.search(loc):
        return True
    if NON_US_RE.search(loc):
        return False  # checked before state codes: "Hyderabad, IN" is India, not Indiana
    if STATE_RE.search(loc):
        return True
    return not loc.strip() or "remote" in loc.lower()


BLOCK_RE = re.compile(
    r"security clearance|secret clearance|ts/sci|top secret|active clearance|"
    r"clearance is required|able to obtain (a|an) [\w ]{0,20}clearance|"
    r"u\.?s\.? citizen(ship)? (is )?required|must be (a )?u\.?s\.? citizens?|"
    r"u\.?s\.? citizens only|\bitar\b|u\.?s\.? persons?\b[^.]{0,60}export", re.I)
NO_SPONSOR_RE = re.compile(
    r"(unable|not able) to (provide |offer )?(visa )?sponsor|"
    r"(will|can|do|does) ?not (provide |offer )?(visa |immigration )?sponsor|"
    r"cannot (provide |offer )?(visa )?sponsor|no (visa |immigration )?sponsorship|"
    r"sponsorship (is )?not (available|offered|provided)|"
    r"without (the need for |requiring )?(current or future |now or in the future |any )?"
    r"(visa |employer |immigration )?sponsorship|not eligible for (visa )?sponsorship", re.I)
SPONSOR_RE = re.compile(
    r"sponsorship (is )?available|we (can |will |do )?sponsor|offers? (visa )?sponsorship", re.I)
YEARS_RE = re.compile(
    r"(\d{1,2})\s*\+?\s*(?:(?:-|to|–)\s*\d{1,2}\s*)?\+?\s*years?(?:'s)?\s+"
    r"(?:of\s+)?(?:[\w/+-]+\s+){0,4}?experience", re.I)


def years_required(text):
    """Smallest 'N+ years ... experience' in the posting, or None."""
    found = [int(n) for n in YEARS_RE.findall(text) if 0 < int(n) <= 20]
    return min(found) if found else None


def skills_in(text, skills):
    out = []
    for s in skills:
        if re.search(r"(?<![\w.+#])" + re.escape(s) + r"(?![\w+#])", text, re.I):
            out.append(s)
    return out


# ---------- sources ----------

def get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


def ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def fetch_greenhouse(token):
    data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true")
    for j in data.get("jobs", []):
        yield {
            "id": f"greenhouse:{token}:{j['id']}",
            "company": j.get("company_name") or token,
            "title": j.get("title", ""),
            "location": (j.get("location") or {}).get("name", ""),
            "url": j.get("absolute_url", ""),
            "posted": j.get("first_published") or j.get("updated_at") or "",
            "description": html_to_text(j.get("content", "")),
        }


def fetch_lever(token):
    for j in get_json(f"https://api.lever.co/v0/postings/{token}?mode=json"):
        cats = j.get("categories") or {}
        lists = "\n".join(f"{x.get('text', '')}\n{html_to_text(x.get('content', ''))}"
                          for x in j.get("lists") or [])
        loc = cats.get("location", "") or ", ".join(cats.get("allLocations") or [])
        if j.get("workplaceType") == "remote" and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        yield {
            "id": f"lever:{token}:{j['id']}",
            "company": token,
            "title": j.get("text", ""),
            "location": loc,
            "url": j.get("hostedUrl", ""),
            "posted": ms_to_iso(j.get("createdAt")),
            "description": "\n\n".join(filter(None, [j.get("descriptionPlain", ""), lists,
                                                     j.get("additionalPlain", "")])).strip(),
        }


def fetch_ashby(token):
    data = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
        loc = " / ".join(filter(None, locs))
        if j.get("isRemote") and "remote" not in loc.lower():
            loc = f"{loc} (Remote)".strip()
        yield {
            "id": f"ashby:{token}:{j.get('id', j.get('jobUrl', ''))}",
            "company": token,
            "title": j.get("title", ""),
            "location": loc,
            "url": j.get("jobUrl", ""),
            "posted": j.get("publishedAt", ""),
            "description": j.get("descriptionPlain") or html_to_text(j.get("descriptionHtml", "")),
        }


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}
SEARCH_HOSTS = {"greenhouse": "job-boards.greenhouse.io", "lever": "jobs.lever.co",
                "ashby": "jobs.ashbyhq.com"}
BOARD_URL_RE = {
    "greenhouse": re.compile(r"^https://(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/\d+"),
    "lever": re.compile(r"^https://jobs\.lever\.co/([A-Za-z0-9_.-]+)/[0-9a-f-]{36}"),
    "ashby": re.compile(r"^https://jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)/[0-9a-f-]{36}"),
}


def discover(cfg):
    """Find more company boards through SearXNG. Only the board name is kept from each
    result, and only when the result URL matches one of the three job board hosts."""
    found = set()
    for role in cfg["search_roles"]:
        for ats, host in SEARCH_HOSTS.items():
            q = urllib.parse.urlencode({"q": f"site:{host} {role}", "format": "json"})
            try:
                data = get_json(f"{core.SEARXNG_URL}/search?{q}")
            except Exception:
                continue
            for r in data.get("results", []):
                m = BOARD_URL_RE[ats].match(r.get("url", ""))
                if m and TOKEN_RE.match(m.group(1)):
                    found.add((ats, m.group(1).lower()))
    return found


# ---------- model ----------

SCORE_PROMPT = """You compare job postings with a candidate's resume and rate the fit.

Resume:
{resume}

Rate the fit from 0 to 100:
- 80 to 100: most required skills are in the resume and the level matches (new grad to mid level).
- 60 to 79: a reasonable fit with some gaps.
- 40 to 59: several required skills are missing.
- 0 to 39: a different field, or clearly too senior.
Use only facts in the resume. Before naming a gap, check that the skill is really missing from the resume.
Reply with JSON: {{"score": number, "fit": "under 20 words on the strongest match", "gaps": "under 20 words on the biggest missing requirement"}}."""

SCORE_SCHEMA = {
    "type": "object",
    "properties": {"score": {"type": "integer"}, "fit": {"type": "string"},
                   "gaps": {"type": "string"}},
    "required": ["score", "fit", "gaps"],
}

DRAFT_PROMPT = """You write answers to job application questions for the candidate whose resume is below.

Resume:
{resume}

Rules:
- Write in the first person as the candidate, 60 to 110 words, plain and specific.
- Every claim about the candidate must be stated in the resume. Describe a project only with what its resume bullets say.
- If the posting asks for something the resume doesn't show, don't claim it. Say the candidate is interested in learning it, or leave it out.
- Name one or two resume projects or skills that match the posting, and say what the candidate did in them.
- No greeting, no sign-off, no headings, no dashes. Reply with the answer text only."""


def model_call(messages, is_busy, model=None, **kw):
    model = model or core.MODEL
    if model.startswith("qwen3:"):
        kw["think"] = False  # the qwen3 tags think by default (see CLAUDE.md)
    while is_busy():  # let panel tasks have the model first
        time.sleep(5)
    return ollama.chat(model=model, messages=messages, keep_alive=-1, **kw)


def restore_default_model():
    """Load the panel's model again after drafting, so the next task doesn't wait for it."""
    try:
        ollama.chat(model=core.MODEL, messages=[{"role": "user", "content": "ok"}],
                    keep_alive=-1, options={"num_predict": 1})
    except Exception:
        pass


def score_job(job, resume, is_busy):
    system = SCORE_PROMPT.format(resume=resume)
    user = (f"Company: {job['company']}\nTitle: {job['title']}\nLocation: {job['location']}\n\n"
            f"Posting:\n{job['description'][:SCORE_DESC_CHARS]}")
    resp = model_call([{"role": "system", "content": system}, {"role": "user", "content": user}],
                      is_busy, format=SCORE_SCHEMA, options={"temperature": 0.2, "num_predict": 150})
    try:
        out = json.loads(resp.message.content or "")
        return max(0, min(100, int(out["score"]))), str(out.get("fit", "")), str(out.get("gaps", ""))
    except (ValueError, KeyError, TypeError):
        return None, "", "The model's reply could not be read."


def draft_answer(job, question, resume, is_busy, model):
    user = (f"Company: {job['company']}\nRole: {job['title']}\n\n"
            f"Posting:\n{job['description'][:DRAFT_DESC_CHARS]}\n\nQuestion: {question}")
    resp = model_call([{"role": "system", "content": DRAFT_PROMPT.format(resume=resume)},
                       {"role": "user", "content": user}],
                      is_busy, model=model, options={"temperature": 0.3, "num_predict": 320})
    text = re.sub(r"\s*—\s*", ", ", (resp.message.content or "").strip())
    if getattr(resp, "done_reason", "") == "length" and ". " in text:
        text = text[:text.rindex(". ") + 1]  # cut off: end at the last full sentence
    return text


# ---------- application answers ----------

FACTS = [  # (label pattern, profile key); first match wins
    (r"preferred (first )?name", "preferred_name"),
    (r"first name", "first_name"),
    (r"last name|surname|family name", "last_name"),
    (r"^full name|^name$|legal name", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"website|portfolio|personal (site|url)", "website"),
    (r"pronoun", "pronouns"),
    (r"authori[sz]ed to work|legally (authorized|eligible|able) to work|work authori[sz]ation|eligible to work", "work_authorized"),
    (r"sponsor", "needs_sponsorship"),
    (r"relocat", "relocate"),
    (r"in.?office|on.?site|hybrid|days (a|per) week|commut", "in_office"),
    (r"where are you (currently )?(located|based)|current (location|city)|^location|city", "location"),
    (r"hear about|how did you (find|learn)|referr", "heard_about"),
    (r"start date|earliest.*start|when (can|could) you start|available to start|notice period", "start_date"),
    (r"salary|compensation|pay expectation", "salary"),
    (r"graduat", "graduation"),
    (r"degree|education|university|school|college", "education"),
]
FALLBACK = {"preferred_name": "first_name"}  # used when the first key is empty
EEO_RE = re.compile(r"gender|race|ethnic|hispanic|latino|veteran|disabilit|sexual orientation|transgender", re.I)
LEGAL_RE = re.compile(r"agree|acknowledg|consent|arbitrat|attest|\bi (hereby )?certify|privacy|policy|"
                      r"terms (and|&) conditions|signature|"
                      r"confirm that|read the|understand that", re.I)
PAST_RE = re.compile(r"(previously|before|ever|past).{0,30}(work|interview|appl|employ)", re.I)
OPEN_RE = re.compile(r"^(why|what|how|tell|describe|share|explain|briefly|please (describe|share|tell|explain))|\?\s*$", re.I)


def answer_for(label, fields, profile):
    """Classify one form question and fill what can be filled from profile.json."""
    ftypes = {f.get("type", "") for f in fields}
    options = [v.get("label", "") for f in fields for v in f.get("values") or []]
    if "input_file" in ftypes or re.search(r"resume|\bcv\b|cover letter", label, re.I):
        if re.search(r"cover letter", label, re.I):
            return {"kind": "draft"}
        return {"kind": "file", "a": "Attach your resume PDF."}
    if EEO_RE.search(label):
        return {"kind": "eeo", "a": "Voluntary. Your choice."}
    if LEGAL_RE.search(label):
        return {"kind": "legal", "a": "Read this and answer it yourself."}
    if PAST_RE.search(label):
        return {"kind": "you", "a": "Answer this yourself."}
    for pat, key in FACTS:
        if re.search(pat, label, re.I):
            value = str(profile.get(key) or profile.get(FALLBACK.get(key, ""), "")).strip()
            if not value:
                return {"kind": "you", "a": f"Add \"{key}\" to profile.json, or answer this yourself."}
            return {"kind": "fact", "a": value, "options": options}
    if OPEN_RE.search(label) and ("textarea" in ftypes or not options):
        return {"kind": "draft"}
    return {"kind": "you", "a": "Answer this yourself.", "options": options}


def standard_questions(profile):
    labels = ["First name", "Last name", "Email", "Phone", "LinkedIn", "GitHub", "Website",
              "Current location", "Are you authorized to work in the US?",
              "Will you require visa sponsorship?", "Resume"]
    return [{"label": l, "required": True, "fields": []} for l in labels]


def prepare(job, profile, resume, is_busy, model, max_drafts=3):
    """Build the list of answers for one job. Greenhouse publishes the form's questions;
    for Lever and Ashby, use the usual fields plus one open answer."""
    ats, token, native = job["id"].split(":", 2)
    questions, note = [], ""
    if ats == "greenhouse":
        try:
            data = get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{native}?questions=true")
            questions = data.get("questions") or []
            questions += data.get("location_questions") or []
            if data.get("compliance"):
                note = "The form also has voluntary demographic questions."
        except Exception as e:
            note = f"Could not load the form's questions ({e})."
    if not questions:
        questions = standard_questions(profile) + [
            {"label": f"Why are you interested in this role at {job['company']}?",
             "required": False, "fields": [{"type": "textarea"}]}]
        note = note or "This board doesn't publish its form questions. Check the form for extra ones."
    answers, drafted = [], 0
    for q in questions:
        label = html_to_text(q.get("label", "")).strip()
        if re.fullmatch(r"(?i)latitude|longitude", label):
            continue  # hidden fields the form fills from the location box
        a = answer_for(label, q.get("fields") or [], profile)
        if a["kind"] == "draft":
            if drafted < max_drafts:
                a["a"] = draft_answer(job, label, resume, is_busy, model)
                drafted += 1
            else:
                a = {"kind": "you", "a": "Answer this yourself."}
        answers.append({"q": label, "required": bool(q.get("required")), **a})
    return answers, note


# ---------- run ----------

def set_progress(step, done=None, total=None):
    progress["step"] = step
    if done is not None:
        progress["done"] = done
    if total is not None:
        progress["total"] = total


def run(is_busy=lambda: False):
    """One full pass: fetch, filter, score, prepare. Returns a stats dict."""
    if not run_lock.acquire(blocking=False):
        return {"error": "A job search is already running."}
    progress.update(running=True, step="starting", done=0, total=0)
    try:
        return _run(is_busy)
    finally:
        progress.update(running=False, step="")
        run_lock.release()


def _run(is_busy):
    cfg = load_config()
    resume = (JOBS_DIR / "resume.txt").read_text().strip()
    profile = load_json(JOBS_DIR / "profile.json", {})
    stats = {"started": stamp(), "boards": 0, "board_errors": [], "fetched": 0, "new": 0,
             "filtered": {}, "scored": 0, "drafted": 0, "backlog": 0}
    update_db(lambda db: db["meta"].update(last_run_date=today(cfg).date().isoformat()))

    boards = {(ats, t.lower()) for ats, ts in cfg["companies"].items() if ats in FETCHERS
              for t in ts if TOKEN_RE.match(t)}
    if cfg["search"]:
        set_progress("searching for more companies")
        found = discover(cfg)

        def remember(db):
            disc = db["meta"]["discovered"]
            for ats, t in found:
                disc[f"{ats}:{t}"] = stamp()
            for key in sorted(disc, key=disc.get)[:-MAX_DISCOVERED]:
                del disc[key]
            return [tuple(k.split(":", 1)) for k in disc]
        boards |= set(update_db(remember))

    # 1. Fetch every board and keep openings not seen before that pass the filters.
    with db_lock:
        db = load_db()
        seen = set(db["seen"]) | set(db["jobs"])
    candidates, fresh_ids = [], set()
    for i, (ats, token) in enumerate(sorted(boards), 1):
        set_progress(f"reading {ats}/{token}", i, len(boards))
        try:
            postings = list(FETCHERS[ats](urllib.parse.quote(token)))
        except Exception as e:
            stats["board_errors"].append(f"{ats}/{token}: {e}"[:120])
            continue
        stats["boards"] += 1
        stats["fetched"] += len(postings)
        for job in postings:
            if job["id"] in seen or job["id"] in fresh_ids:
                continue
            fresh_ids.add(job["id"])
            reason = filter_reason(job, cfg)
            if reason:
                stats["filtered"][reason] = stats["filtered"].get(reason, 0) + 1
                continue
            candidates.append(job)
    stats["new"] = len(fresh_ids)
    stats["matched"] = len(candidates)

    def mark_seen(db):
        day = today(cfg).date().isoformat()
        cand = {c["id"] for c in candidates}
        for jid in fresh_ids - cand:
            db["seen"][jid] = day
        cutoff = (today(cfg) - timedelta(days=SEEN_DAYS)).date().isoformat()
        db["seen"] = {k: v for k, v in db["seen"].items() if v >= cutoff}
    update_db(mark_seen)

    # Openings stored but left unscored by an earlier run (a restart mid-run) are scored too.
    def backlog(db):
        return [j for j in db["jobs"].values() if j.get("score") is None and j["status"] == "new"]
    todo = update_db(backlog)
    for job in candidates:
        desc = job["description"]
        job.update(description=desc[:MAX_DESC_CHARS], found=stamp(), status="new", score=None,
                   fit="", gaps="", answers=None, note="",
                   no_sponsorship=bool(NO_SPONSOR_RE.search(desc)),
                   sponsors=bool(SPONSOR_RE.search(desc)) and not NO_SPONSOR_RE.search(desc),
                   years=years_required(desc), skills=skills_in(desc, cfg["skills"]))
        if job["url"].startswith("https://"):
            todo.append(job)
    todo.sort(key=lambda j: j.get("posted") or "", reverse=True)
    stats["backlog"] = max(0, len(todo) - cfg["max_scored_per_run"])
    todo = todo[:cfg["max_scored_per_run"]]

    def store(db):
        for j in todo:
            db["jobs"].setdefault(j["id"], j)
    update_db(store)

    # 2. Score each candidate. The resume sits in the system prompt so Ollama's
    #    prefix cache covers it after the first call.
    for i, job in enumerate(todo, 1):
        set_progress(f"scoring {job['company']}: {job['title']}", i, len(todo))
        score, fit, gaps = score_job(job, resume, is_busy)
        stats["scored"] += 1

        def save_score(db, jid=job["id"], s=score, f=fit, g=gaps):
            if jid in db["jobs"]:
                db["jobs"][jid].update(score=s, fit=f, gaps=g, scored=stamp())
        update_db(save_score)

    # 3. Prepare answers for the best new matches.
    with db_lock:
        db = load_db()
        strong = sorted((j for j in db["jobs"].values()
                         if j["status"] == "new" and j.get("answers") is None
                         and (j.get("score") or 0) >= cfg["strong_score"]),
                        key=lambda j: -j["score"])[:cfg["max_drafts_per_run"]]
    for i, job in enumerate(strong, 1):
        set_progress(f"preparing {job['company']}: {job['title']}", i, len(strong))
        answers, note = prepare(job, profile, resume, is_busy, cfg["draft_model"])
        stats["drafted"] += 1

        def save_answers(db, jid=job["id"], a=answers, n=note):
            if jid in db["jobs"]:
                db["jobs"][jid].update(answers=a, note=n)
        update_db(save_answers)
    if strong and cfg["draft_model"] != core.MODEL:
        restore_default_model()

    stats["finished"] = stamp()
    update_db(lambda db: db["meta"].update(last_run=stats))
    return stats


def filter_reason(job, cfg):
    if not job["title"] or not job["url"]:
        return "incomplete"
    if not title_ok(job["title"], cfg):
        return "title"
    if not location_ok(job["location"]):
        return "location"
    desc = job["description"]
    if BLOCK_RE.search(desc):
        return "citizenship or clearance"
    if cfg["exclude_no_sponsorship"] and NO_SPONSOR_RE.search(desc):
        return "no sponsorship"
    years = years_required(desc)
    if years is not None and years > cfg["max_years"]:
        return "experience"
    return ""


# ---------- digest and schedule ----------

def digest(notify):
    cfg = load_config()
    with db_lock:
        db = load_db()
        since = db["meta"].get("last_digest_at", "")
        fresh = [j for j in db["jobs"].values()
                 if j["status"] == "new" and (j.get("scored") or "") > since
                 and (j.get("score") or 0) >= cfg["good_score"]]
        last = db["meta"].get("last_run") or {}
    fresh.sort(key=lambda j: -j["score"])
    strong = sum(1 for j in fresh if j["score"] >= cfg["strong_score"])
    if fresh:
        top = "; ".join(f"{j['company']}: {j['title']} ({j['score']})" for j in fresh[:3])
        body = f"{len(fresh)} new matches, {strong} strong. {top}"
    else:
        body = f"No new matches. Scored {last.get('scored', 0)} openings from {last.get('boards', 0)} companies."
    notify("Jobs", body, tag="jobs")
    update_db(lambda db: db["meta"].update(last_digest_at=stamp(),
                                           last_digest_date=today(cfg).date().isoformat()))


def tick(notify, is_busy):
    """Called every minute by server.py: run the search and send the digest when due."""
    if not configured():
        return
    cfg = load_config()
    now = today(cfg)
    day, hm = now.date().isoformat(), now.strftime("%H:%M")
    with db_lock:
        meta = load_db()["meta"]
    if hm >= cfg["run_at"] and meta.get("last_run_date") != day:
        run(is_busy)
        with db_lock:
            meta = load_db()["meta"]
    if hm >= cfg["digest_at"] and meta.get("last_digest_date") != day and meta.get("last_run"):
        digest(notify)


# ---------- panel helpers ----------

SUMMARY_KEYS = ("id", "company", "title", "location", "url", "posted", "status", "score", "fit",
                "gaps", "no_sponsorship", "sponsors", "years", "skills", "found")


def summary():
    cfg = load_config()
    with db_lock:
        db = load_db()
    jobs = [{k: j.get(k) for k in SUMMARY_KEYS} | {"prepared": j.get("answers") is not None}
            for j in db["jobs"].values() if j.get("score") is not None]
    jobs.sort(key=lambda j: (-(j["score"] or 0), j["found"] or ""))
    meta = db["meta"]
    return {
        "configured": configured(),
        "progress": dict(progress),
        "last_run": meta.get("last_run"),
        "last_digest_at": meta.get("last_digest_at"),
        "discovered": len(meta.get("discovered", {})),
        "good": cfg["good_score"], "strong": cfg["strong_score"],
        "unscored": sum(1 for j in db["jobs"].values() if j.get("score") is None),
        "jobs": jobs,
    }


def detail(job_id):
    with db_lock:
        return load_db()["jobs"].get(job_id)


def set_status(job_id, status):
    def change(db):
        if job_id not in db["jobs"]:
            return False
        db["jobs"][job_id]["status"] = status
        db["jobs"][job_id]["status_at"] = stamp()
        return True
    return update_db(change)


def prepare_now(job_id, is_busy):
    """Prepare answers for one job on request (for matches below the strong score)."""
    job = detail(job_id)
    if not job:
        return False
    cfg = load_config()
    resume = (JOBS_DIR / "resume.txt").read_text().strip()
    answers, note = prepare(job, load_json(JOBS_DIR / "profile.json", {}), resume, is_busy,
                            cfg["draft_model"])
    if cfg["draft_model"] != core.MODEL:
        restore_default_model()
    return update_db(lambda db: db["jobs"][job_id].update(answers=answers, note=note) or True)


if __name__ == "__main__":  # manual run: python jobs.py
    print(json.dumps(run(), indent=1))
