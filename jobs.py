#!/usr/bin/env python3
"""Job search: find openings, score them against Sai's resume, prepare applications.

server.py runs this on a schedule inside the panel process. It never submits an
application itself. It prepares the answers, Sai reviews and approves them in the panel,
and the autofill script in his browser submits the approved ones.

Network access is limited to URLs this module builds itself: the public job board
APIs of Greenhouse, Lever, Ashby, Workday (<tenant>.wdN.myworkdayjobs.com) and
SmartRecruiters, and the local SearXNG, plus IMAP for the agent's own inbox. Model output never
becomes a URL or request data, and no personal data leaves the machine.

Files in JOBS_DIR (default /home/agentd/jobs, which the agent user can't read):
    config.json   roles, companies, filters, schedule (deploy/jobs-config.example.json)
    profile.json  facts for application forms (deploy/jobs-profile.example.json)
    resume.txt    plain-text resume used for scoring and drafts
    jobs.json     everything found so far, written by this module
    mail.json     the agent inbox's address and app password (set from the panel)
    inbox.json    emails read from that inbox
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
SCORE_VERSION = 2         # bump to rescore everything with a new method
SEEN_DAYS = 90            # forget skipped openings after this; they are re-checked if still listed
TOKEN_RE = re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
# Workday boards are "<tenant>.wdN/<site>", as in mtb.wd5.myworkdayjobs.com/MTB
WORKDAY_RE = re.compile(r"^[a-z0-9-]{1,60}\.wd\d{1,3}/[A-Za-z0-9_-]{1,80}$")
WORKDAY_PAGES = 5         # 20 postings a page, per search term
SMARTRECRUITERS_PAGES = 10  # 100 postings a page
REQUEST_PAUSE = 0.2       # seconds between requests to the same board

DEFAULTS = {
    "timezone": "America/New_York",
    "run_at": "01:00",
    "digest_at": "08:00",
    "roles": ["software engineer", "software developer", "full stack", "back end",
              "data engineer", "database administrator", "database engineer", "database reliability", "swe",
              "software development engineer", "sde", "application developer", "applications developer",
              "java developer", "python developer", "programmer analyst"],
    "exclude_titles": ["senior", "sr", "staff", "principal", "lead", "manager", "director",
                       "head", "vp", "architect", "distinguished", "fellow", "intern",
                       "internship", "iii", "iv", "3", "4", "5", "6"],
    "search_roles": ["software engineer new grad", "backend engineer", "full stack engineer",
                     "data engineer", "database administrator"],
    "search": True,
    "companies": {"greenhouse": [], "lever": [], "ashby": [], "workday": [], "smartrecruiters": []},
    # Workday boards hold every job a company has (banks list thousands), so they are
    # searched with these terms instead of read whole.
    "workday_search": ["software engineer", "software developer", "data engineer", "database",
                       "full stack", "backend"],
    "max_years": 4,
    "exclude_no_sponsorship": False,
    "max_scored_per_run": 200,
    "draft_model": "qwen3:8b",  # the 4B invented project details in drafts; the 8B stuck to the resume
    "max_drafts_per_run": 15,
    "queue_size": 50,          # applications in the morning review list
    "good_score": 60,
    "strong_score": 72,
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
# "Buffalo, NY", "Remote (NY)", and Workday's "CT - Hartford" (state first)
STATE_RE = re.compile(r"(?:^|,|-|\(|/)\s*(" + "|".join(US_STATES) + r")\b")
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


# ---------- sources ----------

def get_json(url, body=None):
    """GET, or POST when body is given (Workday's search takes a JSON body)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=40) as resp:
        return json.load(resp)


def valid_token(ats, token):
    return bool((WORKDAY_RE if ats == "workday" else TOKEN_RE).match(token or ""))


def ms_to_iso(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat(timespec="seconds")
    except (TypeError, ValueError, OSError):
        return ""


def fetch_greenhouse(token, _cfg=None, _seen=()):
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


def fetch_lever(token, _cfg=None, _seen=()):
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


def fetch_ashby(token, _cfg=None, _seen=()):
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


def workday_posted(det):
    """Workday gives a start date, or only text like "Posted 3 Days Ago"."""
    if det.get("startDate"):
        return det["startDate"]
    text = (det.get("postedOn") or "").lower()
    days = 0 if "today" in text else 1 if "yesterday" in text else None
    m = re.search(r"(\d+)\+? days", text)
    if m:
        days = int(m.group(1))
    if days is None:
        return ""
    return (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()


def fetch_workday(board, cfg, seen=()):
    """Search a Workday board with the configured terms, then read the details of each
    new posting whose title and location pass. Workday lists hold only the title,
    location and path; the description takes one request per posting."""
    tenant_wd, site = board.split("/", 1)
    tenant, wd = tenant_wd.split(".", 1)
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    found = {}
    for term in cfg["workday_search"]:
        for page in range(WORKDAY_PAGES):
            data = get_json(f"{base}/jobs", {"appliedFacets": {}, "limit": 20,
                                             "offset": page * 20, "searchText": term})
            posts = data.get("jobPostings") or []
            for p in posts:
                if p.get("externalPath"):
                    found.setdefault(p["externalPath"], p)
            time.sleep(REQUEST_PAUSE)
            if len(posts) < 20 or (page + 1) * 20 >= (data.get("total") or 0):
                break
    for path, p in found.items():
        jid = f"workday:{board}:{path}"
        loc = p.get("locationsText", "")
        several = re.fullmatch(r"\d+ Locations", loc)  # the real list is in the details
        if jid in seen or not title_ok(p.get("title", ""), cfg) or not (several or location_ok(loc)):
            continue
        det_all = get_json(base + path)
        det = det_all.get("jobPostingInfo") or {}
        time.sleep(REQUEST_PAUSE)
        locs = [det.get("location", "")] + list(det.get("additionalLocations") or [])
        org = re.sub(r"^\d+\s+", "", (det_all.get("hiringOrganization") or {}).get("name", ""))
        yield {
            "id": jid,
            "company": org or tenant,
            "title": det.get("title") or p.get("title", ""),
            "location": " / ".join(filter(None, locs)) or loc,
            "url": det.get("externalUrl") or f"https://{tenant}.{wd}.myworkdayjobs.com/{site}{path}",
            "posted": workday_posted(det),
            "description": html_to_text(det.get("jobDescription", "")),
        }


def fetch_smartrecruiters(company, cfg, seen=()):
    """List a company's US postings, then read the details of each new one whose
    title passes."""
    base = f"https://api.smartrecruiters.com/v1/companies/{company}/postings"
    for page in range(SMARTRECRUITERS_PAGES):
        data = get_json(f"{base}?limit=100&offset={page * 100}&country=us")
        posts = data.get("content") or []
        for p in posts:
            jid = f"smartrecruiters:{company}:{p.get('id')}"
            where = p.get("location") or {}
            loc = where.get("fullLocation") or ", ".join(filter(None, [where.get("city"), where.get("region")]))
            if where.get("remote") and "remote" not in loc.lower():
                loc = f"{loc} (Remote)".strip()
            if jid in seen or not title_ok(p.get("name", ""), cfg) or not location_ok(loc):
                continue
            det = get_json(f"{base}/{urllib.parse.quote(str(p.get('id')))}")
            time.sleep(REQUEST_PAUSE)
            sections = ((det.get("jobAd") or {}).get("sections") or {})
            text = "\n\n".join(html_to_text((sections.get(k) or {}).get("text", ""))
                               for k in ("jobDescription", "qualifications", "additionalInformation"))
            yield {
                "id": jid,
                "company": (p.get("company") or {}).get("name") or company,
                "title": p.get("name", ""),
                "location": loc,
                "url": det.get("postingUrl") or f"https://jobs.smartrecruiters.com/{company}/{p.get('id')}",
                "posted": p.get("releasedDate", ""),
                "description": text.strip(),
            }
        time.sleep(REQUEST_PAUSE)
        if len(posts) < 100 or (page + 1) * 100 >= (data.get("totalFound") or 0):
            break


FETCHERS = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby,
            "workday": fetch_workday, "smartrecruiters": fetch_smartrecruiters}
SEARCH_HOSTS = {"greenhouse": "job-boards.greenhouse.io", "lever": "jobs.lever.co",
                "ashby": "jobs.ashbyhq.com", "workday": "myworkdayjobs.com",
                "smartrecruiters": "jobs.smartrecruiters.com"}
BOARD_URL_RE = {  # the groups joined with "" (Workday with "." and "/") make the board name
    "greenhouse": re.compile(r"^https://(?:job-boards|boards)\.greenhouse\.io/([A-Za-z0-9_-]+)/jobs/\d+"),
    "lever": re.compile(r"^https://jobs\.lever\.co/([A-Za-z0-9_.-]+)/[0-9a-f-]{36}"),
    "ashby": re.compile(r"^https://jobs\.ashbyhq\.com/([A-Za-z0-9_.-]+)/[0-9a-f-]{36}"),
    "workday": re.compile(r"^https://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)/job/"),
    "smartrecruiters": re.compile(r"^https://jobs\.smartrecruiters\.com/([A-Za-z0-9_-]+)/\d+"),
}


def board_from_match(ats, m):
    if ats == "workday":
        return f"{m.group(1)}.{m.group(2)}/{m.group(3)}"
    # Workday and SmartRecruiters names are case-sensitive; the others are not
    return m.group(1) if ats == "smartrecruiters" else m.group(1).lower()


def discover(cfg):
    """Find more company boards through SearXNG. Only the board name is kept from each
    result, and only when the result URL matches one of the job board hosts."""
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
                board = m and board_from_match(ats, m)
                if board and valid_token(ats, board):
                    found.add((ats, board))
    return found


# ---------- model ----------

# The model only extracts facts from the posting; code compares them with the resume
# and does the arithmetic. Asked for a 0-100 fit score directly, the 4B gave 85 to
# almost every software role (97 of 205 in the first run).
EXTRACT_PROMPT = """You read job postings and extract their requirements.

Reply with JSON:
- "required_skills": the technical skills the posting requires, as a candidate would list them on a resume: languages, frameworks, databases, cloud services, tools, and technical areas such as "distributed systems" or "machine learning". For example: Python, React, PostgreSQL, Kubernetes, distributed systems. At most 10, each 1 to 3 words. Leave out product areas, duties, soft skills, degrees and years of experience.
- "level": the seniority the posting asks for: "new grad", "junior", "mid", "senior", "staff", or "unclear".
- "summary": what the role works on, in under 15 words."""

EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "required_skills": {"type": "array", "items": {"type": "string"}},
        "level": {"type": "string", "enum": ["new grad", "junior", "mid", "senior", "staff", "unclear"]},
        "summary": {"type": "string"},
    },
    "required": ["required_skills", "level", "summary"],
}

LEVEL_FIT = {"new grad": 1.0, "junior": 1.0, "mid": 0.8, "unclear": 0.7, "senior": 0.2, "staff": 0.0}
SKILL_ALIASES = [  # applied to both the resume and the skills before comparing
    (r"\bc\+\+", "cpp"), (r"\bc#", "csharp"), (r"\bgcp\b|google cloud platform", "google cloud"),
    (r"\bk8s\b", "kubernetes"), (r"\bgolang\b", "go"), (r"\bpostgres(ql)?\b", "postgresql"),
    (r"\breact\.?js\b", "react"), (r"\bnode\.?js\b|\bnode\b", "nodejs"), (r"\bnext\.?js\b", "nextjs"),
    (r"\bjs\b", "javascript"), (r"\bts\b", "typescript"), (r"\brestful\b", "rest"),
    (r"\bapis\b", "api"), (r"\bspringboot\b", "spring boot"), (r"\bamazon web services\b", "aws"),
    (r"\bci\s*/\s*cd\b", "ci cd"), (r"\bmicro-services\b", "microservices"),
]
SKILL_STOPWORDS = {"and", "or", "of", "the", "with", "in", "a", "an", "for", "to", "experience",
                   "knowledge", "skills", "skill", "development", "programming", "language",
                   "languages", "framework", "frameworks", "tool", "tools", "technologies",
                   "modern", "strong", "proficiency", "familiarity", "using", "based", "etc"}


def skill_text(text):
    """Normalize text for phrase matching: aliases, lowercase words, crude singulars."""
    t = text.lower()
    for pat, rep in SKILL_ALIASES:
        t = re.sub(pat, rep, t)
    words = (w.strip(".") for w in re.findall(r"[a-z0-9][a-z0-9+#.]*", t))
    # the same crude singular form on both sides is enough for matching
    words = [w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w
             for w in words if w and w not in SKILL_STOPWORDS]
    return " ".join(words)


def compare_skills(skills, resume_norm):
    """Split required skills into (has, missing). A skill counts when its words appear
    together in the resume; "C/C++" or "Java or Kotlin" count when either one does."""
    has, missing = [], []
    haystack = f" {resume_norm} "
    for s in skills:
        options = [skill_text(o) for o in re.split(r"/|,|\bor\b", s.replace("C/C++", "C, C++"))]
        options = [o for o in options if o]
        if options:
            (has if any(f" {o} " in haystack for o in options) else missing).append(s.strip())
    return has, missing


def fit_score(has, missing, level, years, no_sponsorship):
    total = len(has) + len(missing)
    skill = len(has) / total if total else 0.5
    yrs = 1.0 if years is None or years <= 2 else {3: 0.85, 4: 0.7}.get(years, 0.5)
    score = 100 * (0.65 * skill + 0.25 * LEVEL_FIT.get(level, 0.7) + 0.10 * yrs)
    return max(0, round(score) - (10 if no_sponsorship else 0))

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


def score_job(job, resume_norm, is_busy):
    """Return the fields to store for one posting: extracted facts and the computed score."""
    user = (f"Title: {job['title']}\n\nPosting:\n{job['description'][:SCORE_DESC_CHARS]}")
    resp = model_call([{"role": "system", "content": EXTRACT_PROMPT}, {"role": "user", "content": user}],
                      is_busy, format=EXTRACT_SCHEMA, options={"temperature": 0.1, "num_predict": 200})
    try:
        out = json.loads(resp.message.content or "")
        skills = [str(s) for s in out["required_skills"]][:10]
        level = out["level"] if out["level"] in LEVEL_FIT else "unclear"
        summary = str(out.get("summary", ""))
    except (ValueError, KeyError, TypeError):
        return {"score": None, "summary": "The model's reply could not be read.",
                "score_version": SCORE_VERSION}
    has, missing = compare_skills(skills, resume_norm)
    return {"score": fit_score(has, missing, level, job.get("years"), job.get("no_sponsorship")),
            "has_skills": has, "missing_skills": missing, "level": level, "summary": summary,
            "score_version": SCORE_VERSION}


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

YES_NO = ["Yes", "No"]
DECLINE = "Decline to self-identify"
# The application profile, as the panel's Profile form shows it. Built from the
# questions on the forms of the jobs found so far. (key, label, choices or None, help)
PROFILE_FORM = [
    ("Name", [
        ("first_name", "Legal first name", None, ""),
        ("last_name", "Legal last name", None, ""),
        ("full_name", "Full legal name", None, "As on your ID. Also names the resume file."),
        ("preferred_name", "Preferred first name", None, "Leave empty to use your first name."),
        ("pronouns", "Pronouns", ["He/him", "She/her", "They/them", DECLINE], ""),
    ]),
    ("Contact", [
        ("email", "Email", None, "The address employers write to."),
        ("phone", "Phone", None, "With country code, like +1 518 555 0100."),
        ("address", "Street address", None, "For forms that ask where you'll work from."),
        ("city", "City", None, ""),
        ("state", "State", None, "Two letters, like NY."),
        ("zip", "ZIP code", None, ""),
        ("location", "City, state", None, "Like Albany, NY. Picks the place in location boxes."),
        ("country", "Country you live in", None, ""),
        ("us_resident", "Live in the US?", YES_NO, ""),
    ]),
    ("Links", [
        ("linkedin", "LinkedIn URL", None, ""),
        ("github", "GitHub URL", None, ""),
        ("website", "Portfolio or website", None, ""),
        ("twitter", "X / Twitter", None, "Optional."),
    ]),
    ("Work authorization", [
        ("work_authorized", "Authorized to work in the US now?", YES_NO, "On OPT this is Yes."),
        ("needs_sponsorship", "Need sponsorship now or in the future?", YES_NO,
         "On OPT this is usually Yes: an H-1B later counts as future sponsorship."),
        ("authorized_without_sponsorship", "Authorized to work without the company's sponsorship?", YES_NO,
         "Asked as one question by some forms. On OPT most people answer No, since it runs out."),
        ("visa_status", "Current status", None, "Like F-1 OPT, or F-1 STEM OPT until 2029-05."),
        ("sponsorship_type", "Sponsorship you'll need", None, "Like H-1B. Used when a form asks what kind."),
        ("citizenship", "Country of citizenship", None, ""),
        ("us_person", "U.S. person for export control?", YES_NO,
         "Citizens, green card holders, refugees and asylees are. F-1 and OPT are not."),
        ("sanctioned_country", "Citizen or resident of Cuba, Iran, North Korea, Syria or Crimea?", YES_NO, ""),
        ("over_18", "At least 18 years old?", YES_NO, ""),
        ("clearance", "Security clearance", None, "Like None."),
    ]),
    ("Experience", [
        ("current_company", "Current or most recent employer", None, ""),
        ("current_title", "Current or most recent job title", None, ""),
        ("years_experience", "Years of full-time software experience", None, "Not counting internships. A number."),
        ("strongest_language", "Strongest programming language", None, ""),
        ("restrictive_agreement", "Bound by a non-compete or similar agreement?", YES_NO, ""),
        ("contact_employer", "May they contact your current employer?", YES_NO, ""),
        ("past_employers", "Companies you've worked for, including internships and contracts", None,
         "Comma separated, or None. \"Have you worked at X before?\" is answered Yes only for these."),
        ("past_interviews", "Companies you've interviewed or applied at before", None,
         "Comma separated, or None. Answers \"Have you interviewed with us before?\"."),
        ("gov_employee", "Ever worked for a government, military or state-owned employer?", YES_NO, ""),
        ("gov_official", "Government official now or in the last five years?", YES_NO, ""),
        ("gov_relative", "Close relative of a government official?", YES_NO, ""),
    ]),
    ("Education, most recent degree", [
        ("school", "School", None, "Full name, as school lists spell it."),
        ("degree", "Degree", ["Bachelor's Degree", "Master's Degree", "Doctorate"], ""),
        ("discipline", "Major", None, "Like Computer Science."),
        ("graduation", "Graduation date", None, "Like May 2025."),
        ("gpa", "GPA", None, "Leave empty if you'd rather not say."),
        ("education", "One line summary", None, "Like M.S. Computer Science, University at Albany, 2025."),
    ]),
    ("Education, bachelor's degree", [
        ("undergrad_school", "School", None, ""),
        ("undergrad_discipline", "Major", None, ""),
        ("undergrad_graduation", "Graduation date", None, ""),
        ("undergrad_gpa", "GPA", None, ""),
        ("gre_score", "GRE score", None, "Optional, like 320 (Q 168, V 152)."),
    ]),
    ("Job preferences", [
        ("relocate", "Willing to relocate?", YES_NO, ""),
        ("in_office", "Willing to work on-site or hybrid?", YES_NO, "Answers \"can you work from our office 3 days a week?\"."),
        ("work_setup", "Remote, hybrid or on-site preference", None, "Like Open to remote, hybrid or on-site."),
        ("start_date", "Earliest start date", None, "Like Immediately, or 2 weeks after an offer."),
        ("salary", "Salary expectation", None, "Like $95,000 to $120,000, or Open to discussion."),
        ("deadlines", "Offer deadlines or timeline", None, "Like None."),
        ("accommodations", "Interview accommodations", None, "Like None."),
        ("heard_about", "How you heard about jobs", None, "LinkedIn is one of the choices on most forms."),
        ("marketing_opt_in", "Opt in to recruiting newsletters and text messages?", YES_NO, ""),
    ]),
    ("Voluntary self-identification", [
        ("gender", "Gender", ["Male", "Female", "Non-binary", DECLINE], "Voluntary. Never affects screening by law."),
        ("race", "Race", ["Asian", "Black or African American", "White", "Hispanic or Latino",
                          "Two or More Races", "Native Hawaiian or Other Pacific Islander",
                          "American Indian or Alaska Native", DECLINE], ""),
        ("hispanic", "Hispanic or Latino?", ["Yes", "No", DECLINE], ""),
        ("veteran", "Protected veteran?", ["I am not a protected veteran", "I identify as one or more of the classifications of a protected veteran", DECLINE], ""),
        ("disability", "Disability", ["No, I do not have a disability", "Yes, I have a disability", DECLINE], ""),
    ]),
]
PROFILE_KEYS = {key for _, fields in PROFILE_FORM for key, *_ in fields}
PROFILE_LABELS = {key: label for _, fields in PROFILE_FORM for key, label, *_ in fields}
PROFILE_MAX_CHARS = 2000   # per field
MAX_SAVED_ANSWERS = 200

FACTS = [  # (label pattern, profile key); first match wins, so specific patterns go first
    (r"preferred (first |full )?name|name you.d prefer|prefer(red)? us to use", "preferred_name"),
    (r"first name", "first_name"),
    (r"last name|surname|family name", "last_name"),
    (r"^full name|^name$|legal name", "full_name"),
    (r"e-?mail", "email"),
    (r"phone|mobile", "phone"),
    (r"linkedin", "linkedin"),
    (r"github", "github"),
    (r"twitter|^x$", "twitter"),
    (r"website|portfolio|personal (site|url)", "website"),
    (r"pronoun", "pronouns"),
    (r"without (company |employer )?sponsor", "authorized_without_sponsorship"),
    (r"(what|which|type of) (sponsorship|support)|sponsorship would you require|list the type", "sponsorship_type"),
    (r"sponsor|sponorship|immigration (support|case)|\bvisa\b(?!.{0,15}mastercard)(?! card)", "needs_sponsorship"),
    (r"authori[sz]ed to work|legally (authorized|eligible|able|work authorized) to work|work authori[sz]ation|eligible to work", "work_authorized"),
    (r"u\.?s\.? person|export control", "us_person"),
    (r"cuba|iran|north korea|syria|crimea", "sanctioned_country"),
    (r"citizenship", "citizenship"),
    (r"18 years", "over_18"),
    (r"clearance", "clearance"),
    (r"government official", "gov_relative_or_official"),
    (r"government|military|state.owned", "gov_employee"),
    (r"non.?compete|non.?solicit|post.employment restriction|agreements? with (a |your )?(current|former)|bound by any agreement", "restrictive_agreement"),
    (r"contact your (current )?employer", "contact_employer"),
    (r"(current|previous|recent|last).{0,20}(employer|company)|where have you (most recently )?worked", "current_company"),
    (r"(current|previous|recent).{0,20}(job )?title", "current_title"),
    (r"how many years", "years_experience"),
    (r"strongest (coding|programming) language", "strongest_language"),
    (r"city and state", "location"),
    (r"work remotely|remote location", "work_setup"),
    (r"in.?office|on.?site|hybrid|days (a|per) week|commut|office location|in.?person|work from (the|our) office", "in_office"),
    (r"(located|based|live|reside) in (the )?(us|u\.s\.?|united states)\b", "us_resident"),
    (r"relocat", "relocate"),
    (r"address", "address"),
    (r"zip|postal", "zip"),
    (r"u\.?s\.? state|what state|state/region|state or province", "state"),
    (r"country", "country"),
    (r"where are you (currently )?(located|based)|current (location|city)|^location|city", "location"),
    (r"hear about|how did you (find|learn)|learned about", "heard_about"),
    # "Start date year" belongs to an education entry, not to when Sai can start
    (r"start date(?! (year|month))|earliest.*start|when (can|could) you start|available to start|start (a new role|full.time|working)|notice period", "start_date"),
    (r"salary|compensation|pay expectation", "salary"),
    (r"deadline|timeline consideration", "deadlines"),
    (r"(describe|need|any).{0,40}accommodation|adjustments we can make", "accommodations"),
    (r"whatsapp|text messages|stay up to date|receive alerts|newsletter|opt.in", "marketing_opt_in"),
    (r"undergrad.{0,15}gpa|gpa.{0,5}undergrad", "undergrad_gpa"),
    (r"gpa(?!.{0,5}doctora)", "gpa"),
    (r"\bgre\b", "gre_score"),
    (r"what (school|university|college)", "school"),
    (r"graduat", "graduation"),
    (r"^degree|degree (type|level)|highest degree", "degree"),
    (r"discipline|major|field of study", "discipline"),
    (r"school|university|college", "school"),
    (r"education", "education"),
]
FALLBACK = {"preferred_name": "first_name"}  # used when the first key is empty
EEO_KEYS = [(r"gender|sex\b", "gender"), (r"hispanic|latino", "hispanic"), (r"race|ethnic", "race"),
            (r"veteran", "veteran"), (r"disabilit", "disability")]
EEO_RE = re.compile(r"gender|race|ethnic|hispanic|latino|veteran|disabilit|sexual orientation|transgender", re.I)
LEGAL_RE = re.compile(r"agree|acknowledg|consent|arbitrat|attest|\bi (hereby )?certify|privacy|policy|"
                      r"terms (and|&) conditions|signature|redact|add another|review the linked|"
                      r"confirm that|read the|understand that", re.I)
PAST_RE = re.compile(r"(previously|before|ever|past).{0,40}(work|interview|appl|employ|consult|engaged)|"
                     r"(employ|work|engaged).{0,80}(in the past|before\b|previously)|current or former .{0,40}employee", re.I)
EXPERIENCE_RE = re.compile(r"(do you (have|possess)|have you).{0,40}\b(\d+|one|two|three|four|five)\+? (\) )?years?\b", re.I)
FOLLOWUP_RE = re.compile(r"^(\[optional[^\]]*\] )?(if (you|yes|so|other|\"|'|“|applicable)|please (specify|explain|provide additional))", re.I)
OPEN_RE = re.compile(r"^(why|what|how|tell|describe|share|explain|briefly|please (describe|share|tell|explain))|\?\s*$", re.I)


LINK_KEYS = {"linkedin", "github", "twitter", "website"}


def fits_options(value, options):
    """Whether a profile value picks one of a question's choices, matched like the
    autofill script does: same words, or one starting with the other."""
    v = norm_label(value)
    return any(o == v or o.startswith(v + " ") or v.startswith(o + " ")
               for o in map(norm_label, options) if o)


def in_list(company, text):
    names = [norm_label(n) for n in str(text or "").split(",")]
    c = norm_label(company)
    return bool(c) and any(n and (n == c or n in c.split() or c in n.split()) for n in names)


def saved_answer(label, profile):
    """Sai's own answer to this question, written in the Profile form."""
    key = norm_label(label)
    for item in profile.get("answers") or []:
        q = norm_label(item.get("q", ""))
        if q and (q == key or (len(q) > 25 and (q in key or key in q))):
            return str(item.get("a", "")).strip()
    return ""


def answer_for(label, fields, profile, company=""):
    """Classify one form question and fill what can be filled from profile.json."""
    ftypes = {f.get("type", "") for f in fields}
    options = [v.get("label", "") for f in fields for v in f.get("values") or []]
    mine = saved_answer(label, profile)
    if mine and "input_file" not in ftypes:
        return {"kind": "fact", "a": mine, "options": options}
    if "input_file" in ftypes or re.search(r"resume|\bcv\b|cover letter", label, re.I):
        if re.search(r"cover letter", label, re.I):
            return {"kind": "draft"}
        return {"kind": "file", "a": "Attach your resume PDF."}
    if EEO_RE.search(label):
        for pat, key in EEO_KEYS:
            if re.search(pat, label, re.I) and profile.get(key):
                if options and not fits_options(profile[key], options):
                    break
                return {"kind": "fact", "a": str(profile[key]), "options": options}  # Sai chose to share it
        return {"kind": "eeo", "a": "Voluntary. Your choice."}
    if LEGAL_RE.search(label):
        return {"kind": "legal", "a": "Read this and answer it yourself."}
    if FOLLOWUP_RE.search(label):  # "If you answered Yes, ..." depends on another answer
        return {"kind": "you", "a": "Answer this yourself if it applies.", "options": options}
    about_them = company and re.search(re.escape(company.split()[0]) + r"|\b(us|our company)\b", label, re.I)
    if PAST_RE.search(label) and about_them:
        key = "past_interviews" if re.search(r"interview|appl", label, re.I) else "past_employers"
        if str(profile.get(key, "")).strip():  # "None" counts as filled in
            return {"kind": "fact", "a": "Yes" if in_list(company, profile[key]) else "No", "options": options}
        return {"kind": "you", "field": key,
                "a": "Fill in the company lists in the Profile tab, or answer this yourself."}
    if PAST_RE.search(label) and not re.search(r"government|military|state.owned", label, re.I):
        return {"kind": "you", "a": "Answer this yourself."}
    if EXPERIENCE_RE.search(label):  # "Do you possess 2 years of experience in X?" is about skills
        return {"kind": "you", "a": "Answer this yourself.", "options": options}
    for pat, key in FACTS:
        if re.search(pat, label, re.I):
            if key in LINK_KEYS and len(label) > 60:
                continue  # "Share a project ... (personal, portfolio or ...)" is a question, not a link
            if key == "gov_relative_or_official":
                key = "gov_relative" if re.search(r"relative", label, re.I) else "gov_official"
            value = str(profile.get(key) or profile.get(FALLBACK.get(key, ""), "")).strip()
            if not value:
                return {"kind": "you", "field": key,
                        "a": f"Fill in \"{PROFILE_LABELS.get(key, key)}\" in the Profile tab, or answer this yourself."}
            if options and not fits_options(value, options):
                return {"kind": "you", "a": f"Your answer ({value}) isn't one of the choices.", "options": options}
            return {"kind": "fact", "a": value, "options": options}
    if OPEN_RE.search(label) and ("textarea" in ftypes or not options):
        return {"kind": "draft"}
    return {"kind": "you", "a": "Answer this yourself.", "options": options}


def standard_questions(profile):
    labels = ["First name", "Last name", "Email", "Phone", "LinkedIn", "GitHub", "Website",
              "Current location", "Are you authorized to work in the US?",
              "Will you require visa sponsorship?", "Resume"]
    return [{"label": l, "required": True, "fields": []} for l in labels]


ASHBY_FORM_QUERY = """query ApiJobPosting($organizationHostedJobsPageName: String!, $jobPostingId: String!) {
 jobPosting(organizationHostedJobsPageName: $organizationHostedJobsPageName, jobPostingId: $jobPostingId) {
  applicationForm { sections { fieldEntries { ... on FormFieldEntry { isRequired field } } } } } }"""
ASHBY_TYPES = {"File": "input_file", "LongText": "textarea"}


def ashby_questions(token, native):
    """The questions on an Ashby application form, shaped like Greenhouse's."""
    data = get_json("https://jobs.ashbyhq.com/api/non-user-graphql?op=ApiJobPosting",
                    {"operationName": "ApiJobPosting", "query": ASHBY_FORM_QUERY,
                     "variables": {"organizationHostedJobsPageName": urllib.parse.unquote(token),
                                   "jobPostingId": native}})
    posting = (data.get("data") or {}).get("jobPosting") or {}
    out = []
    for section in (posting.get("applicationForm") or {}).get("sections") or []:
        for entry in section.get("fieldEntries") or []:
            f = entry.get("field") or {}
            values = [{"label": v.get("label", "")} for v in f.get("selectableValues") or []]
            if f.get("type") == "Boolean":
                values = [{"label": "Yes"}, {"label": "No"}]
            out.append({"label": f.get("title") or "", "required": bool(entry.get("isRequired")),
                        "fields": [{"type": ASHBY_TYPES.get(f.get("type"), "input_text"), "values": values}]})
    return out


def prepare(job, profile, resume, is_busy, model, max_drafts=3):
    """Build the list of answers for one job. Greenhouse and Ashby publish the form's
    questions; for the others, use the usual fields plus one open answer."""
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
    if ats == "ashby":
        try:
            questions = ashby_questions(token, native)
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
        a = answer_for(label, q.get("fields") or [], profile, job["company"])
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

    boards = {(ats, t if ats in ("workday", "smartrecruiters") else t.lower())
              for ats, ts in cfg["companies"].items() if ats in FETCHERS
              for t in ts if valid_token(ats, t)}
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
            postings = list(FETCHERS[ats](urllib.parse.quote(token), cfg, seen))
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
        return [j for j in db["jobs"].values()
                if j.get("score_version") != SCORE_VERSION and j["status"] == "new"]
    todo = update_db(backlog)
    for job in candidates:
        desc = job["description"]
        job.update(description=desc[:MAX_DESC_CHARS], found=stamp(), status="new", score=None,
                   answers=None, note="",
                   no_sponsorship=bool(NO_SPONSOR_RE.search(desc)),
                   sponsors=bool(SPONSOR_RE.search(desc)) and not NO_SPONSOR_RE.search(desc),
                   years=years_required(desc))
        if job["url"].startswith("https://"):
            todo.append(job)
    todo.sort(key=lambda j: j.get("posted") or "", reverse=True)
    stats["backlog"] = max(0, len(todo) - cfg["max_scored_per_run"])
    todo = todo[:cfg["max_scored_per_run"]]

    def store(db):
        for j in todo:
            db["jobs"].setdefault(j["id"], j)
    update_db(store)

    # 2. Score each candidate. The system prompt is the same for every posting, so
    #    Ollama's prefix cache covers it.
    resume_norm = skill_text(resume)
    for i, job in enumerate(todo, 1):
        set_progress(f"scoring {job['company']}: {job['title']}", i, len(todo))
        result = score_job(job, resume_norm, is_busy)
        stats["scored"] += 1

        def save_score(db, jid=job["id"], r=result):
            if jid in db["jobs"]:
                db["jobs"][jid].update(r, scored=stamp())
        update_db(save_score)

    # 3. Prepare answers for the best new matches.
    with db_lock:
        db = load_db()
        # boards the autofill can submit on come first, so the review list fills up
        strong = sorted((j for j in db["jobs"].values()
                         if j["status"] == "new" and j.get("answers") is None
                         and (j.get("score") or 0) >= cfg["good_score"]),
                        key=lambda j: (not auto_apply_ok(j), -j["score"]))[:cfg["max_drafts_per_run"]]
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
    waiting = len(review_queue(db, cfg))
    strong = sum(1 for j in fresh if j["score"] >= cfg["strong_score"])
    if fresh:
        def line(j):
            n = len(j.get("has_skills") or []) + len(j.get("missing_skills") or [])
            skills = f", {len(j['has_skills'])}/{n} skills" if n else ""
            return f"{j['company']}: {j['title']} ({j['score']}{skills})"
        top = "; ".join(line(j) for j in fresh[:5])
        body = f"{len(fresh)} new matches, {strong} strong. {top}"
    else:
        body = f"No new matches. Scored {last.get('scored', 0)} openings from {last.get('boards', 0)} companies."
    if waiting:
        body = f"{waiting} applications ready for your review. " + body
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

SUMMARY_KEYS = ("id", "company", "title", "location", "url", "posted", "status", "score", "summary",
                "level", "has_skills", "missing_skills", "no_sponsorship", "sponsors", "years", "found")


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
        "unscored": sum(1 for j in db["jobs"].values()
                        if j.get("score_version") != SCORE_VERSION and j["status"] == "new"),
        "jobs": jobs,
        "review": [j["id"] for j in review_queue(db, cfg)],
        "approved": sum(1 for j in db["jobs"].values() if j["status"] == "approved"),
    }


def refresh_answers(job, profile):
    """Stored answers with every non-draft one worked out again from the current profile,
    so changes in the Profile tab show up in jobs prepared before them."""
    out = []
    for a in job.get("answers") or []:
        if a["kind"] == "draft":  # a draft for a question the profile now answers gives way
            fresh = answer_for(a["q"], [{"type": "textarea"}], profile, job["company"])
            if fresh["kind"] == "fact":
                a = {"q": a["q"], "required": a.get("required", False), **fresh}
        elif a["kind"] != "draft":
            fields = [{"type": "input_file" if a["kind"] == "file" else "",
                       "values": [{"label": o} for o in a.get("options") or []]}]
            a = {"q": a["q"], "required": a.get("required", False),
                 **answer_for(a["q"], fields, profile, job["company"])}
        out.append(a)
    return out


def detail(job_id):
    with db_lock:
        job = load_db()["jobs"].get(job_id)
    if not job:
        return None
    if job.get("answers") is not None:
        job["answers"] = refresh_answers(job, load_json(JOBS_DIR / "profile.json", {}))
    return {**job, "apply_url": apply_url(job), "emails": job_emails(job_id),
            "auto_apply": auto_apply_ok(job)}


def profile_form():
    """The Profile tab: the form, Sai's answers so far, and the questions from prepared
    jobs that still need him, most common first."""
    profile = load_json(JOBS_DIR / "profile.json", {})
    with db_lock:
        jobs = [j for j in load_db()["jobs"].values() if j.get("answers")]
    open_q = {}
    for job in jobs:
        for a in refresh_answers(job, profile):
            if a["kind"] != "you" or a.get("field") or FOLLOWUP_RE.search(a["q"]):
                continue
            item = open_q.setdefault(norm_label(a["q"]), {"q": a["q"], "jobs": 0, "companies": set(),
                                                          "options": a.get("options") or []})
            item["jobs"] += 1
            item["companies"].add(job["company"])
    unanswered = sorted(open_q.values(), key=lambda x: (-x["jobs"], x["q"]))[:80]
    for item in unanswered:
        item["companies"] = sorted(item["companies"])[:5]
    return {"form": [{"section": name, "fields": [{"key": k, "label": l, "choices": c, "help": h}
                                                   for k, l, c, h in fields]}
                     for name, fields in PROFILE_FORM],
            "profile": {k: v for k, v in profile.items() if k in PROFILE_KEYS},
            "answers": profile.get("answers") or [],
            "unanswered": unanswered, "prepared_jobs": len(jobs)}


def save_profile(values, answers):
    """Save the Profile tab. Keys outside the form are kept as they were."""
    profile = load_json(JOBS_DIR / "profile.json", {})
    for key, value in values.items():
        if key in PROFILE_KEYS:
            profile[key] = str(value)[:PROFILE_MAX_CHARS].strip()
    kept = []
    for item in answers[:MAX_SAVED_ANSWERS]:
        q, a = str(item.get("q", ""))[:500].strip(), str(item.get("a", ""))[:PROFILE_MAX_CHARS].strip()
        if q and a:
            kept.append({"q": q, "a": a})
    profile["answers"] = kept
    save_json(JOBS_DIR / "profile.json", profile)


def apply_url(job):
    """The page with the application form, on the job board's own site where the
    autofill script runs. Many Greenhouse postings link to the company's careers page,
    and job-boards.greenhouse.io/<board>/jobs/<id> redirects there too, but the
    embeddable form opens as a page of its own."""
    ats, board, native = job["id"].split(":", 2)
    url = job.get("url", "")
    if ats == "greenhouse":
        q = urllib.parse.urlencode({"for": urllib.parse.unquote(board), "token": native})
        return f"https://job-boards.greenhouse.io/embed/job_app?{q}"
    if ats == "lever" and not url.endswith("/apply"):
        return url.rstrip("/") + "/apply"
    if ats == "ashby" and not url.endswith("/application"):
        return url.rstrip("/") + "/application"
    return url


# ---------- autofill (the userscript in Sai's browser calls these through server.py) ----------

FORM_URL_RE = [
    ("greenhouse", re.compile(r"^https://(?:job-boards|boards)\.greenhouse\.io/(?:embed/job_app\?.*|([A-Za-z0-9_-]+)/jobs/(\d+))")),
    ("lever", re.compile(r"^https://jobs\.lever\.co/([A-Za-z0-9_.-]+)/([0-9a-f-]{36})")),
    ("ashby", re.compile(r"^https://jobs\.ashbyhq\.com/([A-Za-z0-9_.%-]+)/([0-9a-f-]{36})")),
]


def job_id_for_url(url):
    for ats, pat in FORM_URL_RE:
        m = pat.match(url or "")
        if not m:
            continue
        if ats == "greenhouse" and not m.group(1):  # embedded form: ?for=<board>&token=<job id>
            q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            board, native = (q.get("for") or [""])[0], (q.get("token") or [""])[0]
        else:
            board, native = m.group(1), m.group(2)
        if board and native:
            return f"{ats}:{urllib.parse.quote(board.lower())}:{native}"
    return None


def norm_label(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def fill(page_url, fields):
    """Answers for the fields of an application form open in Sai's browser. Uses the
    answers prepared for that job when it's in jobs.json, and profile.json otherwise.
    Returns a value only for facts, drafts and the resume; everything else is Sai's."""
    profile = load_json(JOBS_DIR / "profile.json", {})
    job = detail(job_id_for_url(page_url) or "")
    approved = bool(job) and job["status"] == "approved"
    agreed = {norm_label(q) for q in (job or {}).get("consents") or []} if approved else set()
    overrides = {norm_label(q): a for q, a in ((job or {}).get("overrides") or {}).items()}
    stored = {norm_label(a["q"]): a for a in (job or {}).get("answers") or []}
    spare_drafts = [a for a in stored.values() if a["kind"] == "draft" and a.get("a")]
    out = []
    for i, f in enumerate(fields):
        label = str(f.get("label", ""))[:300]
        ftype = str(f.get("type", ""))
        options = [str(o)[:200] for o in (f.get("options") or [])][:100]
        a = stored.get(norm_label(label))
        if overrides.get(norm_label(label)):  # Sai's answer from the review
            a = {"kind": "fact", "a": overrides[norm_label(label)]}
        elif not (a and a.get("a") and a["kind"] in ("fact", "draft")):
            a = answer_for(label, [{"type": "input_file" if ftype == "file" else ftype,
                                    "values": [{"label": o} for o in options]}], profile,
                           (job or {}).get("company", ""))
            if a["kind"] == "draft":  # reuse a prepared draft only for a question like it
                fits = spare_drafts and re.search(r"why|interest|cover letter|motivat", label, re.I)
                a = spare_drafts.pop(0) if fits else {"kind": "you"}
        if ftype == "checkbox" and a["kind"] not in ("legal", "fact"):
            a = {"kind": "you"}
        if a["kind"] == "legal" and norm_label(label) in agreed:  # Sai ticked it when approving
            if ftype in ("text", "textarea") and re.search(r"signature|full name|type your name", label, re.I):
                a = {"kind": "fact", "a": str(profile.get("full_name") or "")}
            else:
                a = {"kind": "consent", "a": "agree"}
        value = a.get("a") if a["kind"] in ("fact", "draft", "consent") else None
        out.append({"i": i, "kind": a["kind"], "value": value})
    name = str(profile.get("full_name") or "Resume").replace(" ", "_")
    return {"job": job and {"id": job["id"], "company": job["company"], "title": job["title"]},
            "approved": approved, "answers": out, "resume_name": f"{name}_Resume.pdf" if name != "Resume" else "Resume.pdf"}


# ---------- review and apply ----------
# Each morning Sai reviews up to queue_size prepared applications. Approving one saves
# his edits and his agreement to its consents; the autofill script then submits it in
# his browser, where any CAPTCHA stays his to solve.

AUTO_ATS = ("greenhouse", "lever", "ashby")  # forms the autofill script can fill and submit


def auto_apply_ok(job):
    return job["id"].split(":", 1)[0] in AUTO_ATS


def needs_answer(a):
    """A required question the profile, the drafts and the consents don't cover."""
    return (a.get("required") and a["kind"] == "you" and not FOLLOWUP_RE.search(a["q"]))


def review_queue(db, cfg):
    jobs = [j for j in db["jobs"].values()
            if j["status"] == "new" and j.get("answers") is not None and auto_apply_ok(j)
            and (j.get("score") or 0) >= cfg["good_score"]]
    jobs.sort(key=lambda j: -(j.get("score") or 0))
    return jobs[:cfg["queue_size"]]


def approve(job_id, answers, agreed):
    """Approve one application with Sai's edited answers and the consents he ticked.
    Returns the required questions still without an answer (a required consent he
    didn't tick counts); nothing is saved while any are left."""
    job = detail(job_id)
    if not job or not auto_apply_ok(job) or job.get("answers") is None:
        raise ValueError("This job can't be approved for automatic applying.")
    overrides = {}
    for item in answers[:200]:
        q, a = str(item.get("q", ""))[:500].strip(), str(item.get("a", ""))[:PROFILE_MAX_CHARS].strip()
        if q and a:
            overrides[q] = a
    agreed = {str(q) for q in agreed}
    consents = [a["q"] for a in job["answers"] if a["kind"] == "legal" and a["q"] in agreed]
    missing = [a["q"] for a in job["answers"]
               if (needs_answer(a) and not overrides.get(a["q"]))
               or (a["kind"] == "legal" and a.get("required") and a["q"] not in agreed)]
    if missing:
        return missing

    def change(db):
        j = db["jobs"].get(job_id)
        if j:
            j.update(status="approved", status_at=stamp(), approved_at=stamp(),
                     overrides=overrides, consents=consents)
            j.pop("deferred_at", None)
    update_db(change)
    return []


def next_approved():
    """The approved application to submit next; ones Sai put off go last."""
    with db_lock:
        jobs = [j for j in load_db()["jobs"].values() if j["status"] == "approved"]
    jobs.sort(key=lambda j: (j.get("deferred_at") or "", j.get("approved_at") or ""))
    if not jobs:
        return {"job": None, "left": 0}
    j = jobs[0]
    return {"job": {"id": j["id"], "company": j["company"], "title": j["title"], "apply_url": apply_url(j)},
            "left": len(jobs)}


def defer(job_id):
    return update_db(lambda db: bool(db["jobs"].get(job_id)) and
                     (db["jobs"][job_id].update(deferred_at=stamp()) or True))


def resume_pdf():
    path = JOBS_DIR / "resume.pdf"
    return path.read_bytes() if path.exists() else None


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


# ---------- inbox ----------
# The agent's own email address, which Sai uses on applications. It is read over IMAP,
# read-only (messages stay unread for Sai), to follow up on applications. Email text is
# untrusted: it's never given to a model, links in it are never opened, and the panel
# shows it as plain text.

MAIL_FILE = JOBS_DIR / "mail.json"    # {"address", "password"}; the password never leaves this file
INBOX_FILE = JOBS_DIR / "inbox.json"  # {"uidvalidity", "last_uid", "checked", "error", "messages"}
IMAP_HOSTS = {  # the server comes from the address, never from input
    "gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com",
    "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com",
    "fastmail.com": "imap.fastmail.com", "yahoo.com": "imap.mail.yahoo.com",
    "ymail.com": "imap.mail.yahoo.com", "rocketmail.com": "imap.mail.yahoo.com",
}
ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@([A-Za-z0-9.-]{1,100})$")
INBOX_KEEP = 300          # messages kept for the panel, newest first
INBOX_FIRST_DAYS = 30     # how far back the first check reads
INBOX_BATCH = 60          # messages read per check at most
INBOX_MAX_BYTES = 2_000_000  # bigger messages (attachments) are read by their headers only
SNIPPET_CHARS = 1500
inbox_lock = threading.Lock()

VERIFY_RE = re.compile(r"verif|confirm your (email|account)|one.time (pass)?code|security code|"
                       r"sign.?in code|activate your account|passcode|access code", re.I)
REJECT_RE = re.compile(r"unfortunately|not (to )?(be )?mov(e|ing) forward|decided (not )?to (pursue|proceed|move forward) with other|"
                       r"other candidates|no longer (being )?considered|position has been filled|not been selected|"
                       r"will not be proceeding|regret to inform", re.I)
INTERVIEW_RE = re.compile(r"would like to (invite|schedule|speak|chat|set up)|invite you to|"
                          r"schedule (a|an|your) (call|chat|time|phone|video|conversation)|your availability|"
                          r"recruiter (call|screen)|phone screen|coding (challenge|assessment|exercise)|online assessment|"
                          r"hackerrank|codesignal|codility|take.home", re.I)
CONFIRM_RE = re.compile(r"thank(s| you) for (applying|your application|submitting|your interest)|"
                        r"application (has been |was )?(received|submitted)|we('ve| have) received your application|"
                        r"successfully (applied|submitted)", re.I)
CODE_RE = re.compile(r"(?:code|passcode|pin)\D{0,40}?\b(\d{4,8})\b", re.I)
STATUS_AFTER = {"confirmation": "applied", "interview": "interview", "rejection": "rejected"}


def mail_config():
    return load_json(MAIL_FILE, None)


def imap_host(address):
    m = ADDRESS_RE.match(address or "")
    return m and IMAP_HOSTS.get(m.group(1).lower())


def imap_login(address, password):
    import imaplib, ssl
    host = imap_host(address)
    if not host:
        raise ValueError("Use a Gmail, iCloud, Fastmail or Yahoo address.")
    conn = imaplib.IMAP4_SSL(host, 993, ssl_context=ssl.create_default_context(), timeout=30)
    try:
        conn.login(address, password)
    except imaplib.IMAP4.error:
        conn.logout()
        raise ValueError("The mail server refused the login. Use an app password, not the account password.")
    return conn


def set_mail(address, password):
    """Check the login, then save it. The profile's email becomes this address."""
    address, password = address.strip(), password.replace(" ", "")
    imap_login(address, password).logout()
    save_json(MAIL_FILE, {"address": address, "password": password})
    profile = load_json(JOBS_DIR / "profile.json", {})
    profile["email"] = address
    save_json(JOBS_DIR / "profile.json", profile)
    with inbox_lock:
        save_json(INBOX_FILE, {"messages": []})


def remove_mail():
    for path in (MAIL_FILE, INBOX_FILE):
        path.unlink(missing_ok=True)


def message_text(msg):
    part = msg.get_body(preferencelist=("plain", "html"))
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # unknown charset
        text = part.get_payload(decode=True).decode("utf-8", "replace")
    return html_to_text(text) if part.get_content_type() == "text/html" else text.strip()


def classify_email(subject, text):
    both = subject + "\n" + text[:4000]
    # "2-Step Verification turned on" is a notice; a verification email asks for something
    if VERIFY_RE.search(subject) and (CODE_RE.search(both) or re.search(r"verify|confirm|activate|code", subject, re.I)):
        return "verification"
    if REJECT_RE.search(both):
        return "rejection"
    if INTERVIEW_RE.search(both):
        return "interview"
    if CONFIRM_RE.search(both):
        return "confirmation"
    if VERIFY_RE.search(both) and CODE_RE.search(both):
        return "verification"
    return "other"


def match_job(jobs, sender, subject, text):
    """The job an email is about: its company named by the sender or the subject (or,
    for longer names, the start of the body), then the most title words in common."""
    strong = " " + norm_label(sender + " " + subject) + " "
    body = " " + norm_label(text[:1500]) + " "
    words = set(norm_label(subject + " " + text[:1500]).split())
    best, best_score = None, 0
    for job in jobs:
        company = norm_label(job["company"].split(" - ")[0])
        if not company:
            continue
        named = f" {company} " in strong or f" {company.replace(' ', '')} " in strong
        if not named and not (len(company) >= 6 and f" {company} " in body):
            continue
        title = set(norm_title(job["title"]).split()) - {"and", "of", "the", "i", "ii"}
        score = 1 + len(title & words) / max(len(title), 1) + (0.5 if job["status"] == "applied" else 0)
        if score > best_score:
            best, best_score = job, score
    return best


def check_inbox(notify=lambda *a, **k: None):
    """Read new mail, match it to jobs, move their status along and push what needs Sai."""
    import email, email.policy, email.utils
    cfg = mail_config()
    if not cfg:
        return
    with inbox_lock:
        state = load_json(INBOX_FILE, {})
        state.setdefault("messages", [])
        try:
            conn = imap_login(cfg["address"], cfg["password"])
        except Exception as e:
            state.update(error=str(e), checked=stamp())
            save_json(INBOX_FILE, state)
            raise
        try:
            conn.select("INBOX", readonly=True)
            validity = (conn.response("UIDVALIDITY")[1] or [b""])[0]
            validity = validity.decode() if isinstance(validity, bytes) else str(validity)
            if validity != state.get("uidvalidity"):
                state.update(uidvalidity=validity, last_uid=0)
            last = state.get("last_uid") or 0
            if last:
                _, data = conn.uid("search", None, f"UID {last + 1}:*")
            else:
                since = (datetime.now(timezone.utc) - timedelta(days=INBOX_FIRST_DAYS)).strftime("%d-%b-%Y")
                _, data = conn.uid("search", None, f"SINCE {since}")
            uids = sorted(int(u) for u in (data[0] or b"").split() if int(u) > last)[-INBOX_BATCH:]
            fresh = []
            for uid in uids:
                _, meta = conn.uid("fetch", str(uid), "(RFC822.SIZE)")
                size = re.search(rb"RFC822\.SIZE (\d+)", meta[0] or b"")
                part = "BODY.PEEK[]" if size and int(size.group(1)) <= INBOX_MAX_BYTES else "BODY.PEEK[HEADER]"
                _, got = conn.uid("fetch", str(uid), f"({part})")
                raw = next((x[1] for x in got if isinstance(x, tuple)), b"")
                msg = email.message_from_bytes(raw, policy=email.policy.default)
                name, addr = email.utils.parseaddr(str(msg.get("From", "")))
                subject = str(msg.get("Subject", ""))[:300]
                text = message_text(msg) if part == "BODY.PEEK[]" else ""
                try:
                    when = email.utils.parsedate_to_datetime(str(msg.get("Date"))).astimezone(timezone.utc).isoformat(timespec="seconds")
                except Exception:
                    when = stamp()
                kind = classify_email(subject, text)
                code = CODE_RE.search(subject + "\n" + text[:3000]) if kind == "verification" else None
                fresh.append({"uid": uid, "from": (name or addr)[:120], "from_addr": addr[:200],
                              "subject": subject, "date": when, "kind": kind,
                              "code": code.group(1) if code else "", "snippet": text[:SNIPPET_CHARS]})
                state["last_uid"] = uid
        finally:
            try:
                conn.logout()
            except Exception:
                pass
        if fresh:
            def apply(db):
                jobs = list(db["jobs"].values())
                for m in fresh:
                    job = match_job(jobs, m["from"] + " " + m["from_addr"].split("@")[-1], m["subject"], m["snippet"])
                    if not job:
                        continue
                    m.update(job_id=job["id"], company=job["company"], title=job["title"])
                    new = STATUS_AFTER.get(m["kind"])
                    # a confirmation only moves a job forward from new; the others always apply
                    if new and not (new == "applied" and job["status"] != "new"):
                        job.update(status=new, status_at=stamp())
            update_db(apply)
        state["messages"] = (list(reversed(fresh)) + state["messages"])[:INBOX_KEEP]
        state.update(error="", checked=stamp())
        save_json(INBOX_FILE, state)
    for m in fresh:
        who = m.get("company") or m["from"]
        if m["kind"] == "interview":
            notify("Interview request", f"{who}: {m['subject']}", tag="inbox")
        elif m["kind"] == "verification":
            notify("Verification email", f"{who}: " + (f"code {m['code']}" if m["code"] else m["subject"]), tag="inbox")
    return len(fresh)


def inbox_summary():
    cfg = mail_config()
    state = load_json(INBOX_FILE, {}) if cfg else {}
    return {"configured": bool(cfg), "address": cfg["address"] if cfg else "",
            "checked": state.get("checked"), "error": state.get("error", ""),
            "messages": state.get("messages", [])}


def job_emails(job_id):
    return [m for m in load_json(INBOX_FILE, {}).get("messages", []) if m.get("job_id") == job_id]


if __name__ == "__main__":  # manual run: python jobs.py
    print(json.dumps(run(), indent=1))
