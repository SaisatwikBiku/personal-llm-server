#!/usr/bin/env python3
"""Phase 3: web control panel for the local agent.

Reuses the prompt, model call and tools from agent.py (same folder).
Runs from /opt/agent as the agentd user (see deploy/agent-web.service); tools run
as the agent user through toolrunner.py when AGENT_USE_TOOLRUNNER=1.
"""
import asyncio
import base64
import io
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse
from pydantic import BaseModel
from pywebpush import WebPushException, webpush

import ollama

import agent as core
import jobs

ALLOWED_LOGIN = os.environ.get("AGENT_ALLOWED_LOGIN", "").strip()
MAX_EVENTS = 300

# Web Push (notifications to the home-screen app)
VAPID_FILE = Path(os.environ.get("AGENT_VAPID_KEY", "/home/agent/vapid_private.pem"))
SUBS_FILE = Path(os.environ.get("AGENT_PUSH_SUBS", "/home/agent/push_subscriptions.json"))
PUSH_SUB = os.environ.get("AGENT_PUSH_SUB", "mailto:agent@example.com")
push_lock = threading.Lock()

# Phase 5: run tools as the separate 'agent' user through sudo (see toolrunner.py)
USE_TOOLRUNNER = os.environ.get("AGENT_USE_TOOLRUNNER") == "1"
TOOLRUNNER = ["sudo", "-n", "-u", "agent", "-H",
              "/opt/agent/venv/bin/python", "/opt/agent/toolrunner.py"]

# Chat mode and memory (plain conversation, no tools; see "chat and memory" below)
CHAT_FILE = Path(os.environ.get("AGENT_CHATS", "/home/agentd/chats.json"))
MEMORY_FILE = Path(os.environ.get("AGENT_MEMORY", "/home/agentd/memory.md"))
CHAT_MODELS = {"better": os.environ.get("AGENT_CHAT_MODEL", "qwen3:8b"), "faster": core.MODEL}
CHAT_HISTORY_CHARS = 12_000  # about 3,000 tokens: the 8B rereads 17 tokens/s when its cache is lost
CHAT_MAX_CHATS = 50
MEMORY_MAX_CHARS = 2_000
FILE_MAX_BYTES = 5_000_000      # per attached file
FILE_MAX_CHARS = 1_000_000      # text kept from a file (tasks save all of it)
CHAT_ATTACH_CHARS = 16_000      # file text put into one chat message, about 4,000 tokens
chat_lock = threading.Lock()
chat_active = threading.Semaphore(1)  # one reply at a time; the CPU can't do two

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
lock = threading.Lock()
state = {"status": "idle", "task": None, "events": [], "pending": None, "seq": 0}
decision = {"id": None, "approve": False, "reason": ""}
decision_ready = threading.Event()
stop_requested = threading.Event()

try:
    core.WORKSPACE.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


class Stopped(Exception):
    pass


# ---------- push notifications ----------

def ensure_vapid_key():
    """Create the server's push signing key on first run; return the public key for browsers."""
    if VAPID_FILE.exists():
        key = serialization.load_pem_private_key(VAPID_FILE.read_bytes(), password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        VAPID_FILE.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        VAPID_FILE.chmod(0o600)
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


try:
    VAPID_PUBLIC = ensure_vapid_key()
    VAPID_ERROR = ""
except OSError as e:
    VAPID_PUBLIC = ""
    VAPID_ERROR = f"Push notifications are off: can't read or create {VAPID_FILE} ({e.strerror})."


def load_subs():
    try:
        return json.loads(SUBS_FILE.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        print(f"Can't read {SUBS_FILE}: {e}", flush=True)
        return []


def save_subs(subs):
    SUBS_FILE.write_text(json.dumps(subs))
    SUBS_FILE.chmod(0o600)


def _send_push(title, body, tag):
    payload = json.dumps({"title": title, "body": body[:180], "tag": tag})
    with push_lock:
        subs = load_subs()
        keep = []
        for sub in subs:
            try:
                webpush(subscription_info=sub, data=payload,
                        vapid_private_key=str(VAPID_FILE),
                        vapid_claims={"sub": PUSH_SUB}, ttl=3600)
                keep.append(sub)
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status not in (404, 410):  # 404/410 = subscription gone, drop it
                    keep.append(sub)
            except Exception:
                keep.append(sub)
        if keep != subs:
            save_subs(keep)


def notify(title, body, tag="agent"):
    """Send a push to every subscribed device without blocking the agent loop."""
    if VAPID_PUBLIC:
        threading.Thread(target=_send_push, args=(title, body, tag), daemon=True).start()


def now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def add_event(kind, **data):
    with lock:
        state["seq"] += 1
        state["events"].append({"n": state["seq"], "kind": kind, "time": now(), **data})
        del state["events"][:-MAX_EVENTS]


def startup_problems():
    """Files the panel needs but can't use. These used to fail silently."""
    problems = [VAPID_ERROR] if VAPID_ERROR else []
    for path in (core.LOG_FILE, SUBS_FILE, CHAT_FILE, MEMORY_FILE):
        target = path if path.exists() else path.parent
        if not os.access(target, os.W_OK):
            problems.append(f"Can't write {path}. Check that it belongs to the user running the panel.")
    return problems


for problem in startup_problems():  # shown in the journal and at the top of the panel log
    print(problem, flush=True)
    add_event("error", text=problem)


def set_status(status, pending=None):
    with lock:
        state["status"] = status
        state["pending"] = pending
        state["seq"] += 1


def run_tool(tool, arg, content):
    if not USE_TOOLRUNNER:
        return core.TOOLS[tool](arg, content)
    try:
        r = subprocess.run(TOOLRUNNER + [tool],
                           input=json.dumps({"arg": arg, "content": content}),
                           capture_output=True, text=True, timeout=core.CMD_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        return "Error: tool timed out."
    if r.returncode != 0 and not r.stdout.strip():
        return f"Error running tool: {r.stderr.strip()[:300]}"
    return r.stdout


def wait_for_decision(action):
    action_id = uuid.uuid4().hex[:8]
    decision_ready.clear()
    set_status("waiting", {
        "id": action_id,
        "tool": action.get("tool"),
        "arg": action.get("arg", ""),
        "content": action.get("content", ""),
    })
    notify("Approval needed", f"{action.get('tool')}: {action.get('arg', '')}", tag="approval")
    while not decision_ready.wait(1):
        if stop_requested.is_set():
            raise Stopped
    with lock:
        approve, reason = decision["approve"], decision["reason"]
    set_status("running")
    return approve, reason


def run_task(task):
    system = {"role": "system", "content": core.SYSTEM_PROMPT + memory_block()}
    task_msg = {"role": "user", "content": f"Task: {task}"}
    history = []
    core.log({"event": "task", "task": task, "via": "web"})
    add_event("task", text=task)
    try:
        for step in range(1, core.MAX_STEPS + 1):
            if stop_requested.is_set():
                raise Stopped
            set_status("thinking")
            action, raw, stats = core.next_action([system, task_msg] + history[-core.KEEP_RECENT:])
            history.append({"role": "assistant", "content": raw})
            tool = action.get("tool")
            add_event("thought", step=step, text=action.get("thought", ""), stats=stats)

            if tool == "finish":
                add_event("answer", text=action.get("arg", ""))
                core.log({"event": "finish", "answer": action.get("arg", ""), "via": "web"})
                notify("Task done", action.get("arg", ""), tag="done")
                return

            if tool not in core.TOOLS:
                approved, result = False, f"Unknown tool '{tool}'."
                add_event("error", text=result)
            else:
                auto = tool in core.AUTO_APPROVE
                add_event("action", tool=tool, arg=action.get("arg", ""),
                          content=action.get("content", ""), auto=auto)
                approved, reason = (True, "") if auto else wait_for_decision(action)
                if approved:
                    set_status("running")
                    result = run_tool(tool, action.get("arg", ""), action.get("content", ""))
                    add_event("result", text=result)
                else:
                    result = f"{core.OWNER} rejected this action." + (f" Reason: {reason}" if reason else "")
                    add_event("rejected", text=reason)

            core.log({"event": "action", "tool": tool, "arg": action.get("arg", ""),
                      "approved": approved, "result": result[:500], "via": "web"})
            history.append({"role": "user", "content": f"Result of {tool}:\n{result}"})

        add_event("error", text=f"Stopped after {core.MAX_STEPS} steps without finishing.")
        core.log({"event": "step_limit", "via": "web"})
        notify("Task stopped", f"Hit the {core.MAX_STEPS}-step limit: {task}", tag="done")
    except Stopped:
        add_event("error", text="Task stopped.")
        core.log({"event": "stopped", "via": "web"})
    except Exception as e:  # keep the server alive whatever the agent does
        add_event("error", text=f"Agent error: {e}")
        notify("Agent error", str(e), tag="done")
    finally:
        with lock:
            state["task"] = None
        set_status("idle")


# ---------- chat and memory ----------
#
# Chat is plain conversation with no tools and no network access, so it needs no
# approvals. Memory is a short list of facts about Sai, one per line, added to the
# start of every chat and task. It's kept short because every character is read
# by the model on each uncached request.

CHAT_PROMPT = """You are a helpful assistant for Sai, running privately on his own computer.
- Answer clearly and directly. Lead with the answer, then the details that matter.
- Use short paragraphs. Use a list only when the content is a real list or steps.
- If you are not sure of a fact, say so. Never invent sources, numbers or quotes.
- You have no internet access and no tools here, and your knowledge stops at your training date. For anything current, say you can't check it.
- For code, give complete, working snippets.""".replace("Sai", core.OWNER)

def extract_text(name, data):
    """Text from an attached file: PDF (text layer only), Word .docx, or anything that
    decodes as text. Raises ValueError with a message for Sai when it can't."""
    if data[:5] == b"%PDF-":
        from pypdf import PdfReader
        try:
            pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        except Exception as e:
            raise ValueError(f"Couldn't read this PDF ({e}).")
        text = "\n\n".join(p.strip() for p in pages if p.strip())
        if not text:
            raise ValueError("This PDF has no text layer; it may be a scan. Images aren't supported.")
        return text
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError):
            raise ValueError("Only Word .docx files are supported from zip-based formats.")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"<[^>]+>", "", xml)).strip()
    if b"\x00" in data[:8192]:
        raise ValueError("This looks like a binary file (image, audio, archive...). Only text, PDF and .docx work.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def safe_filename(name):
    base = os.path.basename(name.replace("\\", "/")) or "file"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    return base[:80]


def chat_attachment_text(files):
    """The files as a block ahead of Sai's message, cut to CHAT_ATTACH_CHARS in total."""
    blocks, left = [], CHAT_ATTACH_CHARS
    for f in files:
        text = f.text
        cut = ""
        if len(text) > left:
            cut = f"\n[cut: the first {left:,} of {len(text):,} characters]"
            text = text[:left]
        blocks.append(f"Attached file: {f.name}\n<<<\n{text}{cut}\n>>>")
        left -= len(text)
        if left <= 0:
            break
    return "\n\n".join(blocks)


REMEMBER_RE = re.compile(r"^\s*remember(?:\s+that)?\s*[:,]?\s+(.+)$", re.I | re.S)


def load_memory():
    try:
        return MEMORY_FILE.read_text().strip()
    except OSError:
        return ""


def save_memory(text):
    MEMORY_FILE.write_text(text.strip() + "\n" if text.strip() else "")
    MEMORY_FILE.chmod(0o600)


def memory_block():
    mem = load_memory()
    return f"\n\nWhat you know about {core.OWNER} (from memory; use it when relevant):\n{mem}" if mem else ""


def load_chats():
    try:
        return json.loads(CHAT_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_chats(chats):
    keep = sorted(chats, key=lambda k: chats[k]["updated"], reverse=True)[:CHAT_MAX_CHATS]
    tmp = CHAT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: chats[k] for k in keep}))
    tmp.chmod(0o600)
    os.replace(tmp, CHAT_FILE)


def chat_messages(chat):
    """System prompt, memory and date first (stable, so Ollama's cache covers them),
    then as much recent conversation as fits the budget, oldest dropped first."""
    today = datetime.now().strftime("%A, %B %d, %Y")
    system = CHAT_PROMPT + memory_block() + f"\n\nToday is {today}."
    kept, used = [], 0
    for m in reversed(chat["messages"]):
        used += len(m["content"])
        if kept and used > CHAT_HISTORY_CHARS:
            break
        kept.append({"role": m["role"], "content": m["content"]})
    return [{"role": "system", "content": system}] + kept[::-1]


def write_reply(chat_id, model, out, stop):
    """Run in a thread: stream the model's reply into the queue `out`, stop early when
    `stop` is set, and save the reply (or the part written before a stop or error)."""
    with chat_lock:
        chat = load_chats()[chat_id]
    messages = chat_messages(chat)
    kw = {"think": False} if model.startswith("qwen3:") else {}  # see CLAUDE.md on qwen3 tags
    parts, note = [], ""
    with chat_active:
        stream = None
        try:
            stream = ollama.chat(model=model, messages=messages, stream=True, keep_alive=-1,
                                 options={"temperature": 0.6, "num_predict": 1024}, **kw)
            for part in stream:
                if stop.is_set():
                    note = " (stopped)"
                    break
                text = part.message.content or ""
                if text:
                    parts.append(text)
                    out.put(text)
        except Exception as e:
            note = f"\n\n(error: {e})"
            out.put(note)
        finally:
            if stream is not None and hasattr(stream, "close"):
                stream.close()  # closes the connection, which makes Ollama stop generating
            with chat_lock:
                chats = load_chats()
                if chat_id in chats:
                    chats[chat_id]["messages"].append({"role": "assistant", "content": "".join(parts) + note,
                                                       "model": model, "time": now()})
                    chats[chat_id]["updated"] = time.time()
                    save_chats(chats)
            out.put(None)


async def stream_reply(chat_id, model, request):
    """Pass the reply to the browser as it's written. The model runs in a thread; this
    side checks for a closed connection (Stop, or the page closed) and tells the
    thread to stop, so Ollama doesn't keep writing to nobody."""
    out, stop = queue.Queue(), threading.Event()
    threading.Thread(target=write_reply, args=(chat_id, model, out, stop), daemon=True).start()
    try:
        while True:
            try:
                item = out.get_nowait()
            except queue.Empty:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.1)
                continue
            if item is None:
                return
            yield item
    finally:
        stop.set()


# ---------- job search ----------

def panel_busy():
    """A task or a chat reply is using the model; the job search waits for both."""
    if state["status"] != "idle":
        return True
    if chat_active.acquire(blocking=False):
        chat_active.release()
        return False
    return True


def jobs_loop():
    """Run the nightly job search and the morning digest when they're due (see jobs.tick)."""
    last_error = ""
    while True:
        try:
            jobs.tick(notify, panel_busy)
            last_error = ""
        except Exception as e:  # keep the loop alive; report each new error once
            if str(e) != last_error:
                last_error = str(e)
                print(f"job search error: {e}", flush=True)
                notify("Job search error", str(e), tag="jobs")
        time.sleep(60)


threading.Thread(target=jobs_loop, daemon=True).start()

INBOX_EVERY = 300  # seconds between mail checks


def inbox_loop():
    """Check the agent's inbox (see jobs.check_inbox). Separate from jobs_loop, which is
    busy for hours during the nightly run."""
    last_error = ""
    while True:
        try:
            jobs.check_inbox(notify)
            last_error = ""
        except Exception as e:
            if str(e) != last_error:
                last_error = str(e)
                print(f"inbox error: {e}", flush=True)
                notify("Inbox error", str(e), tag="inbox")
        time.sleep(INBOX_EVERY)


threading.Thread(target=inbox_loop, daemon=True).start()


# ---------- HTTP ----------

def check_user(request: Request):
    if ALLOWED_LOGIN and request.headers.get("Tailscale-User-Login", "") != ALLOWED_LOGIN:
        raise HTTPException(403, "Not allowed")


class FileIn(BaseModel):
    name: str
    text: str


class TaskIn(BaseModel):
    task: str
    files: list[FileIn] = []


class DecisionIn(BaseModel):
    id: str
    approve: bool
    reason: str = ""


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    check_user(request)
    return PAGE


@app.get("/api/state")
def get_state(request: Request, since: int = 0):
    check_user(request)
    with lock:
        return {
            "seq": state["seq"],
            "status": state["status"],
            "task": state["task"],
            "pending": state["pending"],
            "events": [e for e in state["events"] if e["n"] > since],
        }


@app.post("/api/task")
def start_task(body: TaskIn, request: Request):
    """Start a task. Attached files are saved to uploads/ in the agent's workspace (as
    the agent user, through the tool runner) and listed in the task text, so the model
    can read them with its tools."""
    check_user(request)
    task = body.task.strip()
    if not task and not body.files:
        raise HTTPException(400, "Empty task")
    if any(len(f.text) > FILE_MAX_CHARS for f in body.files):
        raise HTTPException(400, f"A file is over {FILE_MAX_CHARS:,} characters")
    with lock:
        if state["status"] != "idle":
            raise HTTPException(409, "A task is already running")
        state["task"] = task or "(files)"
        state["status"] = "thinking"
    saved = []
    for f in body.files:
        path = f"uploads/{safe_filename(f.name)}"
        result = run_tool("write_file", path, f.text)
        if not result.startswith("Wrote"):
            set_status("idle")
            with lock:
                state["task"] = None
            raise HTTPException(500, f"Couldn't save {f.name}: {result[:200]}")
        saved.append(f"{path} ({len(f.text):,} characters)")
    if saved:
        task = (task or "Look at the attached files.") + \
            f"\n\nFiles {core.OWNER} attached, saved in the workspace: " + ", ".join(saved)
        with lock:
            state["task"] = task
    stop_requested.clear()
    threading.Thread(target=run_task, args=(task,), daemon=True).start()
    return {"ok": True}


@app.post("/api/decision")
def decide(body: DecisionIn, request: Request):
    check_user(request)
    with lock:
        pending = state["pending"]
        if not pending or pending["id"] != body.id:
            raise HTTPException(409, "No matching action is waiting")
        decision.update(id=body.id, approve=body.approve, reason=body.reason.strip())
    decision_ready.set()
    return {"ok": True}


@app.post("/api/stop")
def stop(request: Request):
    check_user(request)
    stop_requested.set()
    return {"ok": True}


JOB_STATUSES = ("new", "approved", "applied", "interview", "rejected", "skipped")


class JobIn(BaseModel):
    id: str
    status: str = ""


@app.get("/api/jobs")
def jobs_summary(request: Request):
    check_user(request)
    return jobs.summary()


@app.get("/api/jobs/detail")
def jobs_detail(id: str, request: Request):
    check_user(request)
    job = jobs.detail(id)
    if not job:
        raise HTTPException(404, "No such job")
    return job


@app.post("/api/jobs/status")
def jobs_status(body: JobIn, request: Request):
    check_user(request)
    if body.status not in JOB_STATUSES:
        raise HTTPException(400, "Status must be one of " + ", ".join(JOB_STATUSES))
    if not jobs.set_status(body.id, body.status):
        raise HTTPException(404, "No such job")
    return {"ok": True}


@app.post("/api/jobs/run")
def jobs_run(request: Request):
    check_user(request)
    if not jobs.configured():
        raise HTTPException(409, "Job search isn't set up yet")
    if jobs.progress["running"]:
        raise HTTPException(409, "A job search is already running")
    threading.Thread(target=jobs.run, args=(panel_busy,), daemon=True).start()
    return {"ok": True}


@app.post("/api/jobs/prepare")
def jobs_prepare(body: JobIn, request: Request):
    check_user(request)
    if not jobs.detail(body.id):
        raise HTTPException(404, "No such job")
    threading.Thread(target=jobs.prepare_now, args=(body.id, panel_busy), daemon=True).start()
    return {"ok": True}


class ApproveIn(BaseModel):
    id: str
    answers: list[dict] = []
    agreed: list[str] = []


@app.post("/api/jobs/approve")
def jobs_approve(body: ApproveIn, request: Request):
    """Sai approves one application: his edited answers and the form's consents. The
    autofill script submits approved applications in his browser."""
    check_user(request)
    try:
        missing = jobs.approve(body.id, body.answers, body.agreed[:100])
    except ValueError as e:
        raise HTTPException(400, str(e))
    if missing:
        raise HTTPException(400, "Answer these first: " + "; ".join(missing))
    return {"ok": True}


@app.get("/api/jobs/next")
def jobs_next(request: Request):
    check_user(request)
    return jobs.next_approved()


@app.post("/api/jobs/defer")
def jobs_defer(body: JobIn, request: Request):
    check_user(request)
    if not jobs.defer(body.id):
        raise HTTPException(404, "No such job")
    return {"ok": True}


class MailIn(BaseModel):
    address: str
    password: str


@app.get("/api/inbox")
def inbox_get(request: Request):
    check_user(request)
    return jobs.inbox_summary()


@app.post("/api/inbox")
def inbox_setup(body: MailIn, request: Request):
    """Connect the agent's mailbox. The login is checked before anything is saved, and
    the password is never sent back."""
    check_user(request)
    if len(body.address) > 200 or len(body.password) > 200:
        raise HTTPException(400, "Too long")
    try:
        jobs.set_mail(body.address, body.password)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    threading.Thread(target=jobs.check_inbox, args=(notify,), daemon=True).start()
    return {"ok": True}


@app.post("/api/inbox/check")
def inbox_check(request: Request):
    check_user(request)
    try:
        return {"new": jobs.check_inbox(notify) or 0}
    except Exception as e:
        raise HTTPException(502, str(e))


@app.delete("/api/inbox")
def inbox_remove(request: Request):
    check_user(request)
    jobs.remove_mail()
    return {"ok": True}


class ProfileIn(BaseModel):
    values: dict[str, str]
    answers: list[dict] = []


@app.get("/api/jobs/profile")
def jobs_profile(request: Request):
    check_user(request)
    return jobs.profile_form()


@app.put("/api/jobs/profile")
def jobs_profile_save(body: ProfileIn, request: Request):
    check_user(request)
    if len(body.values) > 200 or len(body.answers) > jobs.MAX_SAVED_ANSWERS:
        raise HTTPException(400, "Too many fields")
    jobs.save_profile(body.values, body.answers)
    return {"ok": True}


class FillIn(BaseModel):
    url: str
    fields: list[dict]


@app.post("/api/jobs/fill")
def jobs_fill(body: FillIn, request: Request):
    check_user(request)
    if len(body.fields) > 200:
        raise HTTPException(400, "Too many fields")
    return jobs.fill(body.url, body.fields)


@app.get("/api/jobs/resume")
def jobs_resume(request: Request):
    check_user(request)
    data = jobs.resume_pdf()
    if data is None:
        raise HTTPException(404, "No resume.pdf in the jobs folder")
    return {"data": base64.b64encode(data).decode()}


@app.get("/jobs-fill.user.js")
def fill_script(request: Request):
    """The autofill userscript, pointed at whatever address the panel was opened on,
    so the tailnet name never has to be in the repo."""
    check_user(request)
    host = request.headers.get("host", "")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", host):
        raise HTTPException(400, "Bad host")
    local = host.split(":")[0] in ("localhost", "127.0.0.1")
    panel = f"{'http' if local else 'https'}://{host}"
    script = FILL_SCRIPT.replace("__PANEL__", panel).replace("__HOST__", host.split(":")[0])
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


class ChatIn(BaseModel):
    message: str
    chat_id: str = ""
    model: str = "better"
    files: list[FileIn] = []


class ExtractIn(BaseModel):
    name: str
    data: str  # base64


@app.post("/api/extract")
def extract(body: ExtractIn, request: Request):
    """Turn an attached file into text, so the page can show its size and reading time
    before sending. Nothing is stored."""
    check_user(request)
    try:
        data = base64.b64decode(body.data, validate=True)
    except ValueError:
        raise HTTPException(400, "Bad file data")
    if len(data) > FILE_MAX_BYTES:
        raise HTTPException(400, f"Files are limited to {FILE_MAX_BYTES // 1_000_000} MB")
    try:
        text = extract_text(body.name, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not text.strip():
        raise HTTPException(400, "No text found in this file")
    return {"name": safe_filename(body.name), "text": text[:FILE_MAX_CHARS], "chars": len(text),
            "chat_limit": CHAT_ATTACH_CHARS}


@app.get("/api/chats")
def chats_list(request: Request):
    check_user(request)
    with chat_lock:
        chats = load_chats()
    items = [{"id": k, "title": c["title"], "updated": c["updated"]} for k, c in chats.items()]
    return sorted(items, key=lambda c: c["updated"], reverse=True)


@app.get("/api/chats/{chat_id}")
def chat_get(chat_id: str, request: Request):
    check_user(request)
    with chat_lock:
        chat = load_chats().get(chat_id)
    if not chat:
        raise HTTPException(404, "No such chat")
    return chat


@app.delete("/api/chats/{chat_id}")
def chat_delete(chat_id: str, request: Request):
    check_user(request)
    with chat_lock:
        chats = load_chats()
        chats.pop(chat_id, None)
        save_chats(chats)
    return {"ok": True}


@app.post("/api/chat")
def chat_send(body: ChatIn, request: Request):
    """Add Sai's message and stream the reply as plain text. The chat id comes back
    in the X-Chat-Id header, so the first message of a new chat can create it."""
    check_user(request)
    text = body.message.strip()
    if not text and not body.files:
        raise HTTPException(400, "Empty message")
    if body.files and not text:
        text = "Summarize the attached file." if len(body.files) == 1 else "Summarize the attached files."
    content = (chat_attachment_text(body.files) + "\n\n" + text) if body.files else text
    model = CHAT_MODELS.get(body.model, CHAT_MODELS["better"])
    with chat_lock:
        chats = load_chats()
        chat_id = body.chat_id if body.chat_id in chats else uuid.uuid4().hex[:10]
        chat = chats.setdefault(chat_id, {"title": text[:60], "created": time.time(), "messages": []})
        msg = {"role": "user", "content": content, "time": now()}
        if body.files:  # the page shows the message and file names, not the file text
            msg.update(text=text, files=[{"name": f.name, "chars": len(f.text)} for f in body.files])
        chat["messages"].append(msg)
        chat["updated"] = time.time()
        remembered = not body.files and REMEMBER_RE.match(text)
        if remembered:  # "remember that ..." saves the fact itself; no model call
            fact = " ".join(remembered.group(1).split())
            mem = load_memory()
            if len(mem) + len(fact) + 3 > MEMORY_MAX_CHARS:
                reply = "Memory is full. Remove something under You > Memory first."
            else:
                save_memory(f"{mem}\n- {fact}")
                reply = f"Saved to memory: {fact}"
            chat["messages"].append({"role": "assistant", "content": reply, "time": now()})
        save_chats(chats)
    headers = {"X-Chat-Id": chat_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if remembered:
        return Response(reply, media_type="text/plain; charset=utf-8", headers=headers)
    if not chat_active.acquire(blocking=False):
        raise HTTPException(409, "Another reply is still being written")
    chat_active.release()
    return StreamingResponse(stream_reply(chat_id, model, request), media_type="text/plain; charset=utf-8",
                             headers=headers)


class MemoryIn(BaseModel):
    text: str


@app.get("/api/memory")
def memory_get(request: Request):
    check_user(request)
    return {"text": load_memory(), "max": MEMORY_MAX_CHARS}


@app.put("/api/memory")
def memory_put(body: MemoryIn, request: Request):
    check_user(request)
    if len(body.text) > MEMORY_MAX_CHARS:
        raise HTTPException(400, f"Memory is limited to {MEMORY_MAX_CHARS} characters")
    save_memory(body.text)
    return {"ok": True}


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: dict


@app.get("/api/push/key")
def push_key(request: Request):
    check_user(request)
    return {"key": VAPID_PUBLIC}


@app.post("/api/push/subscribe")
def push_subscribe(body: SubscriptionIn, request: Request):
    check_user(request)
    sub = {"endpoint": body.endpoint, "keys": body.keys}
    with push_lock:
        subs = [s for s in load_subs() if s.get("endpoint") != body.endpoint]
        subs.append(sub)
        save_subs(subs)
    return {"ok": True}


@app.post("/api/push/test")
def push_test(request: Request):
    check_user(request)
    notify("Agent", "Notifications are working.", tag="test")
    return {"ok": True}


@app.get("/manifest.json")
def manifest(request: Request):
    check_user(request)
    return Response(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker(request: Request):
    check_user(request)
    return Response(SERVICE_WORKER, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.post("/api/clear")
def clear(request: Request):
    check_user(request)
    with lock:
        if state["status"] != "idle":
            raise HTTPException(409, "Stop the task first")
        state["events"].clear()
        state["seq"] += 1
    return {"ok": True}


MANIFEST = json.dumps({
    "name": "Agent",
    "short_name": "Agent",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0b0b10",
    "theme_color": "#0b0b10",
})

SERVICE_WORKER = """
self.addEventListener("push", event => {
  let d = {};
  try { d = event.data.json(); } catch (e) { d = {title: "Agent", body: event.data ? event.data.text() : ""}; }
  event.waitUntil(self.registration.showNotification(d.title || "Agent", {
    body: d.body || "", tag: d.tag || "agent", data: {url: (d.tag === "jobs" || d.tag === "inbox") ? "/#jobs" : "/"}
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) {
      if ("focus" in c) { if (url !== "/") c.postMessage({view: "jobs"}); return c.focus(); }
    }
    return clients.openWindow(url);
  }));
});
"""

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Agent">
<meta name="theme-color" content="#0b0b10">
<link rel="manifest" href="manifest.json">
<title>Agent</title>
<style>
:root{
  --bg:#f5f5f9;--surface:#ffffff;--surface2:#f0f0f6;--raise:#ffffffcc;--line:#e3e3ec;--line2:#d4d4e0;
  --fg:#16161d;--muted:#6c6c7e;--faint:#9a9aab;
  --accent:#6a4cff;--accent2:#0ea5c6;--accent-fg:#fff;--accent-soft:#6a4cff14;
  --good:#12a150;--good-soft:#12a15016;--warn:#c97a06;--warn-soft:#c97a0618;--bad:#d9303e;--bad-soft:#d9303e14;
  --code:#f0f0f6;--shadow:0 1px 2px #0000000a,0 8px 24px #00000010;--ring-track:#e6e6ef;
  --r:14px;--r-sm:10px;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0b0b10;--surface:#13131b;--surface2:#1a1a24;--raise:#17172199;--line:#252532;--line2:#31313f;
  --fg:#ececf4;--muted:#9090a4;--faint:#63637a;
  --accent:#8b73ff;--accent2:#22d3ee;--accent-soft:#8b73ff1f;
  --good:#34d17c;--good-soft:#34d17c1a;--warn:#f3a93c;--warn-soft:#f3a93c1a;--bad:#ff5d6c;--bad-soft:#ff5d6c1a;
  --code:#1d1d28;--shadow:0 1px 2px #0006,0 12px 32px #0007;--ring-track:#262634;
}}
:root[data-theme="dark"]{
  --bg:#0b0b10;--surface:#13131b;--surface2:#1a1a24;--raise:#17172199;--line:#252532;--line2:#31313f;
  --fg:#ececf4;--muted:#9090a4;--faint:#63637a;
  --accent:#8b73ff;--accent2:#22d3ee;--accent-soft:#8b73ff1f;
  --good:#34d17c;--good-soft:#34d17c1a;--warn:#f3a93c;--warn-soft:#f3a93c1a;--bad:#ff5d6c;--bad-soft:#ff5d6c1a;
  --code:#1d1d28;--shadow:0 1px 2px #0006,0 12px 32px #0007;--ring-track:#262634;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,"Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased;overflow:hidden}
body::before{content:"";position:fixed;inset:-20%;z-index:-1;pointer-events:none;
  background:radial-gradient(40% 35% at 12% 8%,color-mix(in srgb,var(--accent) 22%,transparent),transparent 70%),
             radial-gradient(35% 30% at 92% 96%,color-mix(in srgb,var(--accent2) 16%,transparent),transparent 70%)}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none}
a{color:inherit}
svg{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}

/* ---------- shell ---------- */
#app{display:grid;grid-template-columns:236px minmax(0,1fr);height:100dvh}
#rail{display:flex;flex-direction:column;gap:4px;padding:18px 12px;border-right:1px solid var(--line);background:var(--raise);backdrop-filter:blur(18px)}
.brand{display:flex;align-items:center;gap:10px;padding:4px 10px 18px;font-weight:700;font-size:17px;letter-spacing:-.01em}
.logo{width:28px;height:28px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;box-shadow:0 4px 14px color-mix(in srgb,var(--accent) 40%,transparent)}
.logo svg{width:16px;height:16px;stroke:#fff;stroke-width:2.2}
.nav{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:10px;color:var(--muted);font-weight:550;text-align:left;width:100%;position:relative;transition:background .15s,color .15s}
.nav:hover{background:var(--surface2);color:var(--fg)}
.nav.on{background:var(--accent-soft);color:var(--fg)}
.nav.on svg{color:var(--accent)}
.badge{margin-left:auto;min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:var(--accent);color:var(--accent-fg);font-size:11.5px;font-weight:700;display:none;align-items:center;justify-content:center;font-variant-numeric:tabular-nums}
.badge.on{display:inline-flex}
.badge.warn{background:var(--warn)}
.rail-foot{margin-top:auto;display:flex;flex-direction:column;gap:8px;padding:10px 4px 0}
.status{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);min-width:0}
.status span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.dot.idle{background:var(--good);box-shadow:0 0 0 3px var(--good-soft)}
.dot.busy{background:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);animation:pulse 1.4s infinite}
.dot.wait{background:var(--warn);box-shadow:0 0 0 3px var(--warn-soft);animation:pulse 1.4s infinite}
.dot.off{background:var(--bad)}
@keyframes pulse{50%{opacity:.45}}
#topbar{display:none}
#tabbar{display:none}
main{min-width:0;min-height:0;position:relative}
.view{display:none;flex-direction:column;height:100%;min-height:0}
.view.on{display:flex}
.vhead{display:flex;align-items:center;gap:10px;padding:18px 24px 12px;flex-wrap:wrap}
.vhead h1{font-size:22px;letter-spacing:-.02em;margin:0;font-weight:700}
.vhead .sub{color:var(--muted);font-size:13px;width:100%;margin-top:-4px}
.spacer{flex:1}
.vbody{flex:1;min-height:0;overflow-y:auto;padding:4px 24px 24px;-webkit-overflow-scrolling:touch}

/* ---------- controls ---------- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:36px;padding:0 14px;border-radius:10px;font-weight:600;font-size:14px;white-space:nowrap;transition:transform .08s,background .15s,opacity .15s;text-decoration:none}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.5;cursor:default}
.btn.primary{background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent) 70%,var(--accent2)));color:var(--accent-fg);box-shadow:0 4px 14px color-mix(in srgb,var(--accent) 30%,transparent)}
.btn.good{background:var(--good);color:#fff}
.btn.danger{background:var(--bad-soft);color:var(--bad)}
.btn.ghost{background:var(--surface);border:1px solid var(--line);color:var(--fg)}
.btn.ghost:hover{border-color:var(--line2)}
.btn.icon{width:36px;padding:0}
.btn.sm{height:30px;padding:0 10px;font-size:13px;border-radius:8px}
.btn.block{width:100%}
.seg{display:inline-flex;background:var(--surface2);border:1px solid var(--line);border-radius:11px;padding:3px;gap:2px}
.seg button{height:28px;padding:0 11px;border-radius:8px;font-size:13px;font-weight:600;color:var(--muted);display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.seg button.on{background:var(--surface);color:var(--fg);box-shadow:var(--shadow)}
.seg .n{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}
.seg button.on .n{color:var(--accent)}
.scrollx{overflow-x:auto;scrollbar-width:none;max-width:100%}
.scrollx::-webkit-scrollbar{display:none}
.field{display:block;margin:0 0 14px}
.field>label,.flabel{display:block;font-size:13px;font-weight:600;margin-bottom:6px}
.help{font-size:12.5px;color:var(--muted);margin-top:5px}
.inp,textarea.inp,select.inp{width:100%;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:15px;outline:none;transition:border-color .15s,box-shadow .15s}
.inp:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
textarea.inp{min-height:84px;resize:vertical;line-height:1.45}
select.inp{appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);background-position:calc(100% - 17px) 55%,calc(100% - 12px) 55%;background-size:5px 5px;background-repeat:no-repeat;padding-right:32px}
.need .inp{border-color:color-mix(in srgb,var(--warn) 70%,var(--line));background:color-mix(in srgb,var(--warn) 5%,var(--surface))}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px;box-shadow:var(--shadow)}
.chip{display:inline-flex;align-items:center;gap:5px;height:24px;padding:0 9px;border-radius:12px;font-size:12px;font-weight:600;background:var(--surface2);color:var(--muted);white-space:nowrap}
.chip.good{background:var(--good-soft);color:var(--good)}
.chip.bad{background:var(--bad-soft);color:var(--bad)}
.chip.warn{background:var(--warn-soft);color:var(--warn)}
.chip.acc{background:var(--accent-soft);color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.muted{color:var(--muted)}
.small{font-size:13px}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;color:var(--muted);padding:56px 20px}
.empty svg{width:40px;height:40px;stroke-width:1.3;color:var(--faint)}
.empty b{color:var(--fg);font-size:16px}
pre{background:var(--code);padding:10px 12px;border-radius:10px;white-space:pre-wrap;word-break:break-word;font:12.5px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace;margin:8px 0 0;max-height:360px;overflow:auto}
code{font:13px ui-monospace,"SF Mono",Menlo,Consolas,monospace;background:var(--code);padding:1px 5px;border-radius:5px}
details>summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;font-weight:600;font-size:14px;padding:6px 0;user-select:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"";width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s;margin:0 3px}
details[open]>summary::before{transform:rotate(45deg)}
#toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,20px);background:var(--fg);color:var(--bg);padding:10px 16px;border-radius:12px;font-size:14px;font-weight:600;opacity:0;pointer-events:none;transition:all .2s;z-index:60;max-width:calc(100vw - 32px)}
#toast.on{opacity:1;transform:translate(-50%,0)}
#toast.bad{background:var(--bad);color:#fff}
#banner{display:none;position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:50;background:var(--warn);color:#111;border-radius:12px;padding:9px 14px;font-weight:650;font-size:14px;box-shadow:var(--shadow);align-items:center;gap:8px}
#banner.on{display:flex}

/* ---------- chat ---------- */
#v-chat{flex-direction:row}
.chatside{width:260px;border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.chatside .vhead{padding-bottom:8px}
#chatlist{flex:1;overflow-y:auto;padding:0 10px 12px}
.citem{display:block;width:100%;text-align:left;padding:9px 12px;border-radius:10px;color:var(--muted);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.citem:hover{background:var(--surface2);color:var(--fg)}
.citem.on{background:var(--accent-soft);color:var(--fg);font-weight:600}
.chatmain{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}
#chatpick{display:none;max-width:48vw}
#msgs{flex:1;overflow-y:auto;padding:8px 24px 16px;display:flex;flex-direction:column;gap:14px}
.msgwrap{width:100%;max-width:780px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
.msg{white-space:pre-wrap;word-break:break-word;padding:11px 15px;border-radius:18px;max-width:86%;line-height:1.55}
.msg.user{align-self:flex-end;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent) 75%,var(--accent2)));color:#fff;border-bottom-right-radius:6px}
.msg.assistant{align-self:flex-start;background:var(--surface);border:1px solid var(--line);border-bottom-left-radius:6px;box-shadow:var(--shadow)}
.msg.typing{color:var(--muted)}
.msg.typing::after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);margin-left:8px;animation:pulse 1s infinite}
.msg.err{color:var(--bad)}
.msg pre{margin:8px 0}
.msg .files{font-size:12px;opacity:.85;margin-top:6px}
.hello{margin:auto;text-align:center;max-width:460px;color:var(--muted);padding:40px 10px}
.hello .logo{width:52px;height:52px;border-radius:16px;margin:0 auto 16px}
.hello .logo svg{width:26px;height:26px}
.hello h2{color:var(--fg);margin:0 0 6px;font-size:22px;letter-spacing:-.02em}
.suggest{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:18px}
.suggest button{border:1px solid var(--line);background:var(--surface);border-radius:12px;padding:8px 12px;font-size:13.5px;color:var(--fg)}
.suggest button:hover{border-color:var(--accent)}
.composer{padding:10px 24px calc(14px + env(safe-area-inset-bottom,0px))}
.cbox{max-width:780px;margin:0 auto;background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:8px;box-shadow:var(--shadow);transition:border-color .15s,box-shadow .15s}
.cbox:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft),var(--shadow)}
.cfiles{display:none;flex-wrap:wrap;gap:6px;padding:2px 2px 8px}
.cfiles.on{display:flex}
.fchip{display:flex;align-items:center;gap:6px;font-size:12.5px;background:var(--surface2);border-radius:9px;padding:4px 4px 4px 10px;max-width:100%}
.fchip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fchip button{width:22px;height:22px;border-radius:6px;color:var(--muted);font-size:16px;line-height:1}
.fchip button:hover{background:var(--line)}
.crow{display:flex;align-items:flex-end;gap:6px}
.crow textarea{flex:1;border:0;outline:0;background:transparent;resize:none;padding:8px 6px;font-size:15.5px;line-height:1.45;max-height:200px;min-height:40px}
.cbtn{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;color:var(--muted);flex:none}
.cbtn:hover{background:var(--surface2);color:var(--fg)}
.cbtn.send{background:var(--accent);color:#fff}
.cbtn.send:hover{background:var(--accent);filter:brightness(1.08)}
.cbtn.send.stop{background:var(--bad)}
.chint{max-width:780px;margin:6px auto 0;font-size:11.5px;color:var(--faint);text-align:center}

/* ---------- jobs ---------- */
.stats{display:flex;gap:10px;flex-wrap:wrap;width:100%}
.stat{flex:1;min-width:110px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:10px 14px;box-shadow:var(--shadow);text-align:left;transition:border-color .15s}
.stat:hover{border-color:var(--line2)}
.stat b{display:block;font-size:22px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted);font-weight:600}
.stat.hl b{background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
#japply .short{display:none}
@media (max-width: 860px){#japply .short{display:inline}}
.jbar{display:flex;align-items:center;gap:8px;padding:0 24px 10px;flex-wrap:wrap}
#jmeta{font-size:12.5px;color:var(--muted);padding:0 24px 8px}
.jsplit{flex:1;min-height:0;display:grid;grid-template-columns:minmax(300px,420px) minmax(0,1fr);border-top:1px solid var(--line)}
#jlist{overflow-y:auto;padding:12px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
.jrow{display:flex;gap:12px;align-items:flex-start;padding:12px;border-radius:12px;border:1px solid transparent;cursor:pointer;text-align:left;width:100%;transition:background .12s,border-color .12s}
.jrow:hover{background:var(--surface2)}
.jrow.on{background:var(--surface);border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.ring{--p:0;--c:var(--muted);width:42px;height:42px;border-radius:50%;flex:none;display:grid;place-items:center;font-size:13px;font-weight:750;font-variant-numeric:tabular-nums;
  background:radial-gradient(closest-side,var(--surface) 76%,transparent 78% 100%),conic-gradient(var(--c) calc(var(--p)*1%),var(--ring-track) 0)}
.jrow:hover .ring,.jrow.on .ring{background:radial-gradient(closest-side,var(--surface) 76%,transparent 78% 100%),conic-gradient(var(--c) calc(var(--p)*1%),var(--ring-track) 0)}
.ring.strong{--c:var(--good)}
.ring.good{--c:var(--accent)}
.ring.big{width:58px;height:58px;font-size:17px}
.jinfo{min-width:0;flex:1}
.jt{font-weight:650;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.jm{font-size:13px;color:var(--muted);margin:2px 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#jdetail{overflow-y:auto;min-width:0;position:relative;display:flex;flex-direction:column}
.jd{padding:22px 26px 0;max-width:860px;width:100%}
.jdh{display:flex;gap:16px;align-items:flex-start}
.jdh h2{margin:0;font-size:21px;line-height:1.25;letter-spacing:-.02em}
.jdh .jm{white-space:normal;margin:4px 0 0;font-size:14px}
.jback{display:none}
.jsum{color:var(--muted);margin:14px 0 10px}
.skills{margin:10px 0 4px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 6px}
.sec{margin:22px 0 6px}
.sech{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:10px}
.sech .chip{text-transform:none;letter-spacing:0}
.q{padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--surface);margin-bottom:8px}
.q .ql{font-size:14px;font-weight:600;line-height:1.4}
.q .qk{font-size:12px;color:var(--muted);margin:2px 0 8px}
.q .qa{white-space:pre-wrap;word-break:break-word;font-size:14px;margin-top:6px}
.q .req{color:var(--warn);font-weight:700}
.q.need{border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}
.consent{display:flex;gap:10px;align-items:flex-start;cursor:pointer}
.consent input{width:20px;height:20px;accent-color:var(--accent);margin-top:1px;flex:none}
.stickyfoot{position:sticky;bottom:0;margin-top:auto;padding:12px 26px calc(12px + env(safe-area-inset-bottom,0px));background:linear-gradient(to top,var(--bg) 70%,transparent);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.stickyfoot .left{font-size:13px;color:var(--muted);flex:1;min-width:140px}
.mail{padding:14px;border:1px solid var(--line);border-radius:12px;background:var(--surface);margin-bottom:8px;box-shadow:var(--shadow)}
.mail .mt{font-weight:650;margin:6px 0 2px}
.mail .code{font:700 26px ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:4px;margin:8px 0}

/* ---------- tasks ---------- */
#events{max-width:860px;margin:0 auto;width:100%}
.ev{position:relative;padding:0 0 14px 22px;border-left:2px solid var(--line);margin-left:6px}
.ev::before{content:"";position:absolute;left:-6px;top:4px;width:10px;height:10px;border-radius:50%;background:var(--line2)}
.ev.task{border-left-color:transparent;padding-top:10px}
.ev.task::before{background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
.ev.task .tt{font-weight:700;font-size:15px}
.ev.answer::before{background:var(--good)}
.ev.err::before{background:var(--bad)}
.ev .thought{color:var(--muted);font-size:13.5px}
.ev .tool{font-size:12.5px;font-weight:650;color:var(--accent)}
.ev .ans{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--good);border-radius:10px;padding:10px 14px;white-space:pre-wrap;box-shadow:var(--shadow)}
.ev .bad{color:var(--bad);font-size:14px}
#pending{display:none;max-width:860px;margin:0 auto 14px;width:100%;border:1px solid var(--warn);background:color-mix(in srgb,var(--warn) 6%,var(--surface))}
#pending.on{display:block}

/* ---------- you ---------- */
.youwrap{max-width:900px;margin:0 auto;width:100%}
.progress{height:8px;border-radius:4px;background:var(--surface2);overflow:hidden;margin:8px 0 4px}
.progress i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:4px;transition:width .3s}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.pgrid .wide{grid-column:1/-1}
.psec{scroll-margin-top:12px}
.psec h3{font-size:16px;margin:26px 0 12px;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.pfield.empty>label::after{content:"empty";margin-left:8px;font-size:11px;font-weight:600;color:var(--warn);background:var(--warn-soft);padding:1px 7px;border-radius:8px}
.savebar{position:sticky;bottom:0;display:flex;align-items:center;gap:10px;padding:12px 0 calc(12px + env(safe-area-inset-bottom,0px));background:linear-gradient(to top,var(--bg) 75%,transparent);margin-top:10px}
#memtext{min-height:50vh;font:14px/1.55 ui-monospace,"SF Mono",Menlo,Consolas,monospace}

/* ---------- phone ---------- */
@media (max-width: 860px){
  #app{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr) auto}
  #rail{display:none}
  #topbar{display:flex;align-items:center;gap:10px;padding:calc(10px + env(safe-area-inset-top,0px)) 16px 8px;border-bottom:1px solid var(--line);background:var(--raise);backdrop-filter:blur(18px)}
  #topbar .brand{padding:0;font-size:16px}
  #topbar .status{margin-left:auto;max-width:48vw}
  #tabbar{display:flex;border-top:1px solid var(--line);background:var(--raise);backdrop-filter:blur(18px);padding:6px 4px calc(6px + env(safe-area-inset-bottom,0px))}
  #tabbar .nav{flex-direction:column;gap:3px;padding:6px 2px;font-size:11px;justify-content:center;border-radius:12px}
  #tabbar .nav.on{background:none;color:var(--accent)}
  #tabbar .badge{position:absolute;top:0;left:calc(50% + 6px);margin:0;height:17px;min-width:17px;font-size:10.5px;padding:0 5px}
  .vhead{padding:14px 16px 10px}
  .vhead h1{font-size:20px}
  .vbody{padding:4px 16px 20px}
  .chatside{display:none}
  #chatpick{display:block;flex:1;min-width:0;max-width:none}
  #chattitle{display:none}
  #msgs{padding:8px 14px 12px}
  .msg{max-width:92%}
  .composer{padding:8px 10px 10px}
  .chint{display:none}
  .jbar,#jmeta{padding-left:16px;padding-right:16px}
  #v-jobs .vhead{flex-wrap:nowrap}
  #japply .long{display:none}
  .stats{gap:8px}
  .stat{min-width:0;padding:8px 10px}
  .stat b{font-size:19px}
  .jsplit{grid-template-columns:1fr}
  #jlist{border-right:0;padding:10px}
  #jdetail{position:fixed;inset:0;z-index:40;background:var(--bg);transform:translateX(100%);transition:transform .22s ease;padding-top:env(safe-area-inset-top,0px)}
  #v-jobs.detail #jdetail{transform:none}
  .jback{display:inline-flex}
  .jd{padding:12px 16px 0}
  .stickyfoot{padding:10px 16px calc(10px + env(safe-area-inset-bottom,0px))}
  .pgrid{grid-template-columns:1fr}
  #banner{top:calc(8px + env(safe-area-inset-top,0px))}
  #toast{bottom:calc(84px + env(safe-area-inset-bottom,0px))}
}
</style></head><body>
<div id="app">
  <aside id="rail">
    <div class="brand"><div class="logo"><svg viewBox="0 0 24 24"><path d="M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z"/></svg></div>Agent</div>
    <div id="navs"></div>
    <div class="rail-foot">
      <button class="btn ghost sm" id="alerts" style="display:none"></button>
      <button class="btn ghost sm" id="theme" title="Switch theme"></button>
      <div class="status"><i class="dot" id="dot"></i><span id="status">connecting</span></div>
    </div>
  </aside>
  <header id="topbar">
    <div class="brand"><div class="logo"><svg viewBox="0 0 24 24"><path d="M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z"/></svg></div>Agent</div>
    <div class="status"><i class="dot" id="dot2"></i><span id="status2">connecting</span></div>
  </header>
  <main>
    <!-- chat -->
    <section id="v-chat" class="view">
      <div class="chatside">
        <div class="vhead"><h1>Chats</h1><span class="spacer"></span><button class="btn icon ghost" id="newchat" title="New chat" aria-label="New chat"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button></div>
        <div id="chatlist"></div>
      </div>
      <div class="chatmain">
        <div class="vhead">
          <select class="inp" id="chatpick" aria-label="Conversation" style="width:auto;height:36px;padding:0 30px 0 10px;font-size:14px"></select>
          <h1 id="chattitle" style="font-size:17px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:50%"></h1>
          <span class="spacer"></span>
          <div class="seg" id="chatmodel"><button data-m="better">8B · smart</button><button data-m="faster">4B · fast</button></div>
          <button class="btn icon ghost" id="chatdel" title="Delete chat" aria-label="Delete chat"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>
        </div>
        <div id="msgs"><div class="msgwrap" id="msgwrap"></div></div>
        <div class="composer">
          <div class="cbox">
            <div class="cfiles" id="c-files"></div>
            <div class="crow">
              <button class="cbtn" id="c-attach" title="Attach files" aria-label="Attach files"><svg viewBox="0 0 24 24"><path d="M21 11.5l-8.6 8.6a5 5 0 01-7-7l8.6-8.6a3.5 3.5 0 015 5l-8.6 8.6a2 2 0 01-2.8-2.8l7.9-7.9"/></svg></button>
              <textarea id="c-input" rows="1" placeholder="Message the agent" enterkeyhint="send"></textarea>
              <button class="cbtn send" id="c-send" title="Send" aria-label="Send"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
            </div>
          </div>
          <div class="chint">Runs on your server, offline. Say "remember that ..." to save a fact. Shift+Enter for a new line.</div>
        </div>
      </div>
    </section>

    <!-- jobs -->
    <section id="v-jobs" class="view">
      <div class="vhead">
        <h1>Jobs</h1><span class="spacer"></span>
        <a class="btn primary" id="japply" target="_blank" rel="noopener noreferrer" style="display:none"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg><span class="long"></span><span class="short"></span></a>
        <button class="btn ghost icon" id="jrun" title="Run the search now" aria-label="Run the search now"><svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 11-2.3-5.7M20 4v5h-5"/></svg></button>
        <a class="btn ghost icon" id="jsetup" href="jobs-fill.user.js" title="Install the autofill script" aria-label="Install the autofill script"><svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></a>
      </div>
      <div class="jbar"><div class="stats" id="jstats"></div></div>
      <div class="jbar"><div class="scrollx"><div class="seg" id="jfilter"></div></div></div>
      <div id="jmeta"></div>
      <div class="jsplit">
        <div id="jlist"></div>
        <div id="jdetail"></div>
      </div>
    </section>

    <!-- inbox -->
    <section id="v-inbox" class="view">
      <div class="vhead"><h1>Inbox</h1><span class="spacer"></span>
        <button class="btn ghost sm" id="icheck" style="display:none">Check now</button>
        <button class="btn danger sm" id="ioff" style="display:none">Disconnect</button>
        <div class="sub" id="imeta"></div>
      </div>
      <div class="jbar" id="ifilterbar" style="display:none"><div class="scrollx"><div class="seg" id="ifilter"></div></div></div>
      <div class="vbody"><div class="youwrap" id="ibody"></div></div>
    </section>

    <!-- tasks -->
    <section id="v-tasks" class="view">
      <div class="vhead"><h1>Tasks</h1><span class="spacer"></span>
        <button class="btn danger sm" id="stop">Stop</button>
        <button class="btn ghost sm" id="clear">Clear</button>
        <div class="sub">The agent runs commands in its sandbox. Anything with side effects waits for your approval.</div>
      </div>
      <div class="vbody">
        <div class="card" id="pending">
          <div style="display:flex;align-items:center;gap:8px;font-weight:700"><svg viewBox="0 0 24 24" style="color:var(--warn)"><path d="M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>Approve this action?</div>
          <div id="pbody"></div>
          <input class="inp" id="reason" placeholder="Reason if denying (optional)" style="margin-top:10px">
          <div style="display:flex;gap:8px;margin-top:10px"><button class="btn danger" id="deny" style="flex:1">Deny</button><button class="btn good" id="approve" style="flex:1">Approve</button></div>
        </div>
        <div id="events"></div>
      </div>
      <div class="composer">
        <div class="cbox">
          <div class="cfiles" id="t-files"></div>
          <div class="crow">
            <button class="cbtn" id="t-attach" title="Attach files" aria-label="Attach files"><svg viewBox="0 0 24 24"><path d="M21 11.5l-8.6 8.6a5 5 0 01-7-7l8.6-8.6a3.5 3.5 0 015 5l-8.6 8.6a2 2 0 01-2.8-2.8l7.9-7.9"/></svg></button>
            <textarea id="t-input" rows="1" placeholder="Give the agent a task" enterkeyhint="send"></textarea>
            <button class="cbtn send" id="t-send" title="Run" aria-label="Run"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
          </div>
        </div>
      </div>
    </section>

    <!-- you -->
    <section id="v-you" class="view">
      <div class="vhead"><h1>You</h1><span class="spacer"></span>
        <div class="seg" id="yousub"><button data-s="profile">Application profile</button><button data-s="memory">Memory</button></div>
      </div>
      <div class="vbody">
        <div class="youwrap" id="y-profile">
          <div class="card" style="margin-bottom:6px">
            <div style="display:flex;align-items:baseline;gap:8px"><b>Profile</b><span class="muted small" id="pstat"></span></div>
            <div class="progress"><i id="pbar" style="width:0"></i></div>
            <div class="muted small">Everything application forms ask for, built from the questions on real forms. The autofill and the prepared answers use it. Consents are never answered from here.</div>
            <div class="scrollx" style="margin-top:12px"><div class="chips" id="pjump" style="flex-wrap:nowrap"></div></div>
          </div>
          <div id="pform"></div>
          <div class="savebar"><span class="muted small" id="pdirty" style="flex:1"></span><button class="btn primary" id="psave">Save profile</button></div>
        </div>
        <div class="youwrap" id="y-memory" style="display:none">
          <div class="muted small" style="margin-bottom:10px">Facts the assistant knows about you, one per line. They go at the start of every chat and task, so keep them short. In a chat, "remember that ..." adds one.</div>
          <textarea class="inp" id="memtext" spellcheck="false"></textarea>
          <div class="savebar"><span class="muted small" id="memcount" style="flex:1"></span><button class="btn primary" id="memsave">Save memory</button></div>
        </div>
      </div>
    </section>
  </main>
  <nav id="tabbar"></nav>
</div>
<input type="file" id="filepick" multiple hidden>
<div id="banner" role="button" tabindex="0"><svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg><span>The agent is waiting for your approval</span></div>
<div id="toast"></div>
<script>
// Everything on this page is built with DOM calls and textContent, never innerHTML:
// model output and email text are untrusted.
const $ = id => document.getElementById(id);
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; }
function pre(text){ return el("pre", null, text); }
const SVGNS = "http://www.w3.org/2000/svg";
const ICONS = {
  chat: "M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z",
  jobs: "M4 8h16v11H4zM9 8V5h6v3M4 13h16",
  inbox: "M4 13l2.5-8h11L20 13v6H4zM4 13h5l1 2h4l1-2h5",
  tasks: "M5 7l2 2 4-4M5 17l2 2 4-4M14 7h6M14 17h6",
  you: "M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0",
  back: "M15 18l-6-6 6-6", send: "M5 12h14M13 6l6 6-6 6", ext: "M14 4h6v6M20 4l-9 9M18 14v5H5V6h5", copy: "M8 8h11v11H8zM5 16V5h11",
  check: "M5 12l5 5 9-10", x: "M6 6l12 12M18 6L6 18", mail: "M3 6h18v12H3zM3 7l9 6 9-6",
  sparkle: "M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z", sun: "M12 17a5 5 0 100-10 5 5 0 000 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z", bell: "M6 8a6 6 0 1112 0c0 7 3 9 3 9H3s3-2 3-9M10 21h4",
};
function icon(name){ const s = document.createElementNS(SVGNS, "svg"); s.setAttribute("viewBox", "0 0 24 24"); const p = document.createElementNS(SVGNS, "path"); p.setAttribute("d", ICONS[name]); s.append(p); return s; }
let toastTimer;
function toast(msg, bad){ const t = $("toast"); t.textContent = msg; t.className = "on" + (bad ? " bad" : ""); clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.className = ""; }, bad ? 5000 : 2600); }
async function api(path, opts){
  const r = await fetch(path, opts);
  if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Error " + r.status)); }
  return r.json().catch(() => ({}));
}
const send = (path, method, body) => api(path, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
function when(iso){ return iso ? new Date(iso).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : ""; }
function store(k, v){ try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }

// ---------- navigation ----------
const VIEWS = [["chat", "Chat"], ["jobs", "Jobs"], ["inbox", "Inbox"], ["tasks", "Tasks"], ["you", "You"]];
const OLD = {log: "tasks", memory: "you", profile: "you"};
let view = "";
for (const holder of [$("navs"), $("tabbar")]) {
  for (const [v, label] of VIEWS) {
    const b = el("button", "nav");
    b.dataset.view = v;
    b.append(icon(v), el("span", null, label), el("span", "badge"));
    b.onclick = () => showView(v);
    holder.append(b);
  }
}
function badge(v, n, warn){
  for (const b of document.querySelectorAll('.nav[data-view="' + v + '"] .badge')) {
    b.textContent = n > 99 ? "99+" : String(n);
    b.classList.toggle("on", n > 0);
    b.classList.toggle("warn", !!warn);
  }
}
function showView(v){
  v = OLD[v] || v;
  if (!VIEWS.some(x => x[0] === v)) v = "chat";
  view = v;
  for (const [name] of VIEWS) $("v-" + name).classList.toggle("on", name === v);
  for (const b of document.querySelectorAll(".nav")) b.classList.toggle("on", b.dataset.view === v);
  history.replaceState(null, "", "#" + v);
  store("view", v);
  if (v === "jobs") loadJobs(true);
  if (v === "inbox") { inboxSig = ""; loadInbox(); }
  if (v === "you") showYou(store("yousub") || "profile");
  if (v === "chat" && !chatLoaded) { chatLoaded = true; loadChatList(); openChat(chatId); }
  if (v === "tasks") { const b = $("v-tasks").querySelector(".vbody"); b.scrollTop = b.scrollHeight; }
}

// ---------- theme and alerts ----------
function applyTheme(t){
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const b = $("theme"); b.replaceChildren(icon(dark ? "sun" : "moon"), el("span", null, dark ? "Light mode" : "Dark mode"));
}
$("theme").onclick = () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const t = dark ? "light" : "dark"; store("theme", t); applyTheme(t);
};
applyTheme(store("theme"));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(store("theme")));
function b64ToBytes(s){
  const pad = "=".repeat((4 - s.length % 4) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
async function setupAlerts(){
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  const reg = await navigator.serviceWorker.register("sw.js");
  const btn = $("alerts");
  const label = on => btn.replaceChildren(icon("bell"), el("span", null, on ? "Alerts on" : "Turn on alerts"));
  btn.style.display = "";
  const existing = await reg.pushManager.getSubscription();
  label(!!existing);
  if (existing) send("api/push/subscribe", "POST", existing.toJSON()).catch(() => {});
  btn.onclick = async () => {
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") return toast("Notifications were not allowed.", true);
      const {key} = await api("api/push/key");
      const sub = (await reg.pushManager.getSubscription()) ||
        await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64ToBytes(key)});
      await send("api/push/subscribe", "POST", sub.toJSON());
      await send("api/push/test", "POST");
      label(true); toast("Alerts are on");
    } catch (e) { toast("Could not turn on alerts: " + e.message, true); }
  };
}

// ---------- tasks (the agent's log) ----------
let last = 0, pendingId = null;
function render(ev){
  const box = el("div", "ev");
  if (ev.kind === "task") { box.classList.add("task"); box.append(el("div", "tt", ev.text)); }
  else if (ev.kind === "thought") box.append(el("div", "thought", "Step " + ev.step + " · " + ev.stats + " — " + ev.text));
  else if (ev.kind === "action") {
    box.append(el("div", "tool", ev.tool + (ev.auto ? " · auto-approved, read-only" : "")), pre(ev.arg));
    if (ev.content) box.append(pre(ev.content));
  }
  else if (ev.kind === "result") { const d = el("details"); d.append(el("summary", null, "Result"), pre(ev.text)); box.append(d); }
  else if (ev.kind === "rejected") { box.classList.add("err"); box.append(el("div", "bad", "Denied" + (ev.text ? ": " + ev.text : ""))); }
  else if (ev.kind === "answer") { box.classList.add("answer"); box.append(el("div", "ans", ev.text)); }
  else { box.classList.add("err"); box.append(el("div", "bad", ev.text)); }
  $("events").append(box);
}
function setStatus(text, cls){
  for (const [d, s] of [["dot", "status"], ["dot2", "status2"]]) { $(s).textContent = text; $(d).className = "dot " + cls; }
}
async function poll(){
  try {
    const r = await fetch("api/state?since=" + last);
    if (!r.ok) return setStatus("error " + r.status, "off");
    const s = await r.json();
    if (s.seq < last) { last = 0; $("events").replaceChildren(); return poll(); }
    const body = $("v-tasks").querySelector(".vbody");
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    if (!last && !s.events.length && !$("events").childNodes.length) {
      const e = el("div", "empty"); e.append(icon("tasks"), el("b", null, "No tasks yet"), el("div", null, "Ask the agent to do something on the server: check disk space, organize files, summarize a log."));
      $("events").append(e);
    }
    if (s.events.length) { const e = $("events").querySelector(".empty"); if (e) e.remove(); }
    for (const e of s.events) { render(e); last = Math.max(last, e.n); }
    const p = s.pending;
    setStatus(p ? "waiting for approval" : s.status + (s.task ? " · " + s.task : ""), p ? "wait" : s.status === "idle" ? "idle" : "busy");
    if (p && p.id !== pendingId) {
      pendingId = p.id;
      const b = $("pbody");
      b.replaceChildren(el("div", "tool small", p.tool), pre(p.arg));
      if (p.content) b.append(pre(p.content));
      $("reason").value = "";
      $("pending").classList.add("on");
    }
    if (!p) { pendingId = null; $("pending").classList.remove("on"); }
    $("banner").classList.toggle("on", !!p && view !== "tasks");
    badge("tasks", p ? 1 : 0, true);
    if (s.events.length && nearBottom) body.scrollTop = body.scrollHeight;
  } catch (e) { setStatus("offline", "off"); }
}
$("banner").onclick = () => showView("tasks");
$("approve").onclick = () => { if (pendingId) { const id = pendingId; pendingId = "sent"; send("api/decision", "POST", {id, approve: true}).catch(e => toast(e.message, true)).then(poll); } };
$("deny").onclick = () => { if (pendingId) { const id = pendingId; pendingId = "sent"; send("api/decision", "POST", {id, approve: false, reason: $("reason").value}).catch(e => toast(e.message, true)).then(poll); } };
$("stop").onclick = () => send("api/stop", "POST").catch(e => toast(e.message, true)).then(poll);
$("clear").onclick = () => send("api/clear", "POST").then(() => { last = 0; $("events").replaceChildren(); poll(); }, e => toast(e.message, true));

// ---------- composers and attachments ----------
// Files are turned into text on the server first, so the page can show how long the
// model will take to read them. Chat puts the text in the message (first 16,000
// characters); Tasks saves each file into the agent's workspace.
const READ_RATE = {better: 17, faster: 35};  // tokens per second, measured on the server
const composers = {c: {files: []}, t: {files: []}};
let pickFor = "c";
function readTime(chars){
  const s = Math.round(chars / 4 / READ_RATE[chatModel]);
  return s < 60 ? "~" + Math.max(s, 1) + " s to read" : "~" + Math.round(s / 60) + " min to read";
}
function renderFiles(k){
  const box = $(k + "-files"), list = composers[k].files;
  box.replaceChildren();
  let left = 16000;
  for (const f of list) {
    let label = f.name;
    if (f.reading) label += " · reading...";
    else if (k === "c") {
      const used = Math.min(f.chars, Math.max(left, 0));
      left -= used;
      label += " · " + f.chars.toLocaleString() + " chars" + (used < f.chars ? ", first " + used.toLocaleString() + " used" : "") + " · " + readTime(used);
    } else label += " · " + f.chars.toLocaleString() + " chars, saved to the workspace";
    const chip = el("div", "fchip"); chip.title = label;
    const x = el("button", null, "×"); x.setAttribute("aria-label", "Remove " + f.name);
    x.onclick = () => { composers[k].files = list.filter(a => a !== f); renderFiles(k); };
    chip.append(el("span", null, label), x);
    box.append(chip);
  }
  box.classList.toggle("on", list.length > 0);
}
async function toBase64(file){
  const bytes = new Uint8Array(await file.arrayBuffer());
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
async function addFile(k, file){
  if (file.size > 5e6) return toast(file.name + " is over 5 MB.", true);
  const f = {name: file.name, reading: true, chars: 0, text: ""};
  composers[k].files.push(f);
  renderFiles(k);
  try {
    const d = await send("api/extract", "POST", {name: file.name, data: await toBase64(file)});
    Object.assign(f, {name: d.name, text: d.text, chars: d.chars, reading: false});
  } catch (e) {
    composers[k].files = composers[k].files.filter(a => a !== f);
    toast(file.name + ": " + e.message, true);
  }
  renderFiles(k);
}
$("filepick").onchange = async () => {
  const picked = [...$("filepick").files];
  $("filepick").value = "";
  for (const file of picked) await addFile(pickFor, file);
};
function grow(t){ t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
for (const k of ["c", "t"]) {
  const input = $(k + "-input");
  $(k + "-attach").onclick = () => { pickFor = k; $("filepick").click(); };
  input.addEventListener("input", () => grow(input));
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $(k + "-send").click(); } });
  $(k + "-send").onclick = () => {
    if (k === "c" && chatAbort) { chatAbort.abort(); return; }
    const text = input.value.trim(), list = composers[k].files;
    if (!text && !list.length) return;
    if (list.some(f => f.reading)) return toast("Still reading a file.");
    const files = list.map(f => ({name: f.name, text: f.text}));
    input.value = ""; grow(input);
    composers[k].files = []; renderFiles(k);
    if (k === "c") sendChat(text, files);
    else send("api/task", "POST", {task: text, files}).then(poll, e => toast(e.message, true));
  };
}

// ---------- chat ----------
let chatId = store("chat") || "", chatAbort = null, chatLoaded = false, chatModel = store("chatmodel") || "better", chats = [];
function setModel(m){ chatModel = m; store("chatmodel", m); for (const b of $("chatmodel").children) b.classList.toggle("on", b.dataset.m === m); renderFiles("c"); }
for (const b of $("chatmodel").children) b.onclick = () => setModel(b.dataset.m);
setModel(chatModel);
const msgBox = () => $("msgs");
function nearEnd(box){ return box.scrollHeight - box.scrollTop - box.clientHeight < 120; }
// Light formatting for replies: code blocks, **bold**, `code` and # headings, built
// from text nodes and elements.
function inline(parent, line){
  for (const piece of line.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/)) {
    if (/^\*\*[^*]+\*\*$/.test(piece)) { const b = el("strong"); inline(b, piece.slice(2, -2)); parent.append(b); }
    else if (/^`[^`]+`$/.test(piece)) parent.append(el("code", null, piece.slice(1, -1)));
    else if (piece) parent.append(document.createTextNode(piece));
  }
}
function rich(box, text){
  box.replaceChildren();
  const fence = /```[^\n]*\n?([\s\S]*?)(?:```|$)/g;
  let at = 0, m;
  const prose = t => t.split("\n").forEach((line, i) => {
    if (i) box.append(document.createTextNode("\n"));
    const h = line.match(/^#{1,6}\s+(.*)$/);
    if (h) box.append(el("strong", null, h[1])); else inline(box, line);
  });
  while ((m = fence.exec(text))) {
    prose(text.slice(at, m.index));
    box.append(pre(m[1].replace(/\n$/, "")));
    at = fence.lastIndex;
  }
  prose(text.slice(at));
}
function addMsg(role, text, files){
  const d = el("div", "msg " + role);
  if (role === "assistant") rich(d, text); else d.textContent = text;
  if (files && files.length) d.append(el("div", "files", "\u{1F4CE} " + files.map(f => f.name).join(", ")));
  $("msgwrap").append(d);
  return d;
}
function hello(){
  const h = el("div", "hello");
  const logo = el("div", "logo"); logo.append(icon("sparkle"));
  h.append(logo, el("h2", null, "What can I help with?"), el("div", null, "Replies are written on your own server, with no internet access."));
  const sg = el("div", "suggest");
  for (const s of ["Write a follow-up email after an interview", "Explain a Python error I paste", "Make a study plan for system design", "Rewrite my resume bullet to sound stronger"]) {
    const b = el("button", null, s);
    b.onclick = () => { const i = $("c-input"); i.value = s; grow(i); i.focus(); };
    sg.append(b);
  }
  h.append(sg);
  $("msgwrap").append(h);
}
function renderChatList(){
  const list = $("chatlist"), pick = $("chatpick");
  list.replaceChildren(); pick.replaceChildren();
  const o = el("option", null, "New chat"); o.value = ""; pick.append(o);
  if (!chats.length) list.append(el("div", "muted small", "No conversations yet."));
  for (const c of chats) {
    const b = el("button", "citem" + (c.id === chatId ? " on" : ""), c.title);
    b.onclick = () => { if (!chatAbort) openChat(c.id); };
    list.append(b);
    const op = el("option", null, c.title); op.value = c.id; pick.append(op);
  }
  pick.value = chatId;
}
async function loadChatList(){
  try { chats = await api("api/chats"); } catch (e) { chats = []; }
  if (chatId && !chats.some(c => c.id === chatId)) chatId = "";
  renderChatList();
}
async function openChat(id){
  chatId = id;
  store("chat", id);
  $("msgwrap").replaceChildren();
  $("chatdel").style.display = id ? "" : "none";
  const c = chats.find(x => x.id === id);
  $("chattitle").textContent = c ? c.title : "New chat";
  renderChatList();
  if (!id) return hello();
  try {
    const data = await api("api/chats/" + encodeURIComponent(id));
    for (const m of data.messages) addMsg(m.role, m.text || m.content, m.files);
  } catch (e) { return openChat(""); }
  msgBox().scrollTop = msgBox().scrollHeight;
}
async function sendChat(text, files){
  if (chatAbort) return;
  if (!chatId) $("msgwrap").replaceChildren();
  files = files || [];
  addMsg("user", text || (files.length > 1 ? "Summarize the attached files." : "Summarize the attached file."), files);
  const out = addMsg("assistant", "Thinking. After a pause or a model switch, the first reply can take a minute.");
  out.classList.add("typing");
  const box = msgBox();
  box.scrollTop = box.scrollHeight;
  chatAbort = new AbortController();
  const sendBtn = $("c-send");
  sendBtn.classList.add("stop"); sendBtn.replaceChildren(icon("x")); sendBtn.title = "Stop";
  let got = "";
  try {
    const r = await fetch("api/chat", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message: text, chat_id: chatId, model: chatModel, files}), signal: chatAbort.signal});
    if (!r.ok) {
      const t = await r.json().catch(() => ({}));
      out.textContent = t.detail || ("Error " + r.status);
      out.classList.remove("typing"); out.classList.add("err");
      return;
    }
    const id = r.headers.get("X-Chat-Id") || "";
    const isNew = id !== chatId;
    chatId = id;
    store("chat", id);
    const reader = r.body.getReader(), dec = new TextDecoder();
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      const follow = nearEnd(box);
      got += dec.decode(value, {stream: true});
      rich(out, got);
      out.classList.remove("typing");
      if (follow) box.scrollTop = box.scrollHeight;
    }
    if (isNew) { await loadChatList(); const c = chats.find(x => x.id === chatId); $("chattitle").textContent = c ? c.title : ""; $("chatdel").style.display = ""; }
  } catch (e) {
    out.classList.remove("typing");
    rich(out, got + (e.name === "AbortError" ? " (stopped)" : "\n(connection lost)"));
  } finally {
    chatAbort = null;
    sendBtn.classList.remove("stop"); sendBtn.replaceChildren(icon("send")); sendBtn.title = "Send";
  }
}
$("newchat").onclick = () => { if (!chatAbort) { openChat(""); $("c-input").focus(); } };
$("chatpick").onchange = () => { if (!chatAbort) openChat($("chatpick").value); };
$("chatdel").onclick = async () => {
  if (!chatId || !confirm("Delete this conversation?")) return;
  await fetch("api/chats/" + encodeURIComponent(chatId), {method: "DELETE"});
  await loadChatList();
  openChat("");
};

// ---------- jobs ----------
const FILTERS = [["review", "Review"], ["approved", "Approved"], ["new", "New"], ["applied", "Applied"], ["interview", "Interviewing"], ["rejected", "Rejected"], ["skipped", "Skipped"], ["all", "All"]];
let S = null, jfilter = store("jfilter") || "review", jsel = null, listSig = "", applySig = "";
function counts(s){
  const c = {review: (s.review || []).length, all: s.jobs.length};
  for (const j of s.jobs) c[j.status] = (c[j.status] || 0) + 1;
  return c;
}
function jobsMeta(s){
  const p = s.progress, lr = s.last_run;
  if (p.running) return "Searching now: " + p.step + (p.total ? " (" + p.done + "/" + p.total + ")" : "");
  if (!lr) return "The search hasn't run yet.";
  const n = x => (x || 0).toLocaleString();
  let t = "Last search " + when(lr.finished || lr.started) + ": " + n(lr.boards) + " companies, " + n(lr.new) + " new postings, " + n(lr.scored) + " scored";
  if (lr.backlog) t += ", " + n(lr.backlog) + " left for the next run";
  return t + ".";
}
function scoreRing(score, s, big){
  const r = el("div", "ring" + (score >= s.strong ? " strong" : score >= s.good ? " good" : "") + (big ? " big" : ""), String(score));
  r.style.setProperty("--p", Math.max(0, Math.min(100, score || 0)));
  return r;
}
function statusChip(st){
  const map = {approved: ["acc", "Approved"], applied: ["good", "Applied"], interview: ["good", "Interviewing"], rejected: ["bad", "Rejected"], skipped: ["", "Skipped"]};
  const m = map[st]; return m ? el("span", "chip " + m[0], m[1]) : null;
}
function listJobs(){
  if (jfilter === "review") return (S.review || []).map(id => S.jobs.find(j => j.id === id)).filter(Boolean);
  return S.jobs.filter(j => jfilter === "all" || j.status === jfilter);
}
async function loadJobs(force){
  let s;
  try { s = await api("api/jobs"); } catch (e) { return; }
  S = s;
  const c = counts(s);
  badge("jobs", c.review);
  if (view !== "jobs") return;
  $("jmeta").textContent = jobsMeta(s);
  $("jrun").disabled = s.progress.running || !s.configured;
  // stats
  const st = $("jstats"); st.replaceChildren();
  for (const [k, label, hl] of [["review", "To review", true], ["approved", "Approved"], ["applied", "Applied"], ["interview", "Interviews"]]) {
    const b = el("button", "stat" + (hl ? " hl" : ""));
    b.append(el("b", null, String(c[k] || 0)), el("span", null, label));
    b.onclick = () => setFilter(k);
    st.append(b);
  }
  // filters
  const f = $("jfilter"); f.replaceChildren();
  for (const [k, label] of FILTERS) {
    const b = el("button", k === jfilter ? "on" : "");
    b.append(document.createTextNode(label));
    if (c[k]) b.append(el("span", "n", String(c[k])));
    b.onclick = () => setFilter(k);
    f.append(b);
  }
  updateApply(c.approved || 0);
  const list = listJobs();
  const sig = jfilter + JSON.stringify(list.map(j => [j.id, j.status, j.prepared, j.score]));
  if (sig !== listSig || force) { listSig = sig; renderList(list, s); }
  if (!s.configured) $("jdetail").replaceChildren(emptyState("jobs", "Job search isn't set up", "Put config.json and resume.txt in the jobs folder on the server."));
  else if (!jsel && !document.querySelector("#jdetail .jd")) $("jdetail").replaceChildren(emptyState("jobs", list.length ? "Pick a job" : "Nothing here", list.length ? "Its answers, the posting and the actions open here." : (jfilter === "review" ? "New applications are prepared overnight." : "No jobs with this status.")));
}
function emptyState(ic, title, text){ const e = el("div", "empty"); e.append(icon(ic), el("b", null, title), el("div", null, text)); return e; }
function setFilter(k){ jfilter = k; store("jfilter", k); jsel = null; listSig = ""; $("jdetail").replaceChildren(); $("v-jobs").classList.remove("detail"); loadJobs(true); }
function renderList(list, s){
  const box = $("jlist"); box.replaceChildren();
  if (jfilter === "review" && list.length) box.append(el("div", "muted small", "Check each one, fill what's missing, tick the statements you agree to, approve. Approved ones are submitted from Apply to approved."));
  if (!list.length) box.append(emptyState(jfilter === "review" ? "check" : "jobs", jfilter === "review" ? "All caught up" : "Nothing here", jfilter === "review" ? "New applications are prepared overnight." : "No jobs with this status yet."));
  for (const j of list) {
    const r = el("button", "jrow" + (j.id === jsel ? " on" : ""));
    r.dataset.id = j.id;
    const info = el("div", "jinfo");
    info.append(el("div", "jt", j.title), el("div", "jm", [j.company, j.location].filter(Boolean).join(" · ")));
    const chips = el("div", "chips");
    const sc = statusChip(j.status); if (sc && jfilter !== j.status) chips.append(sc);
    if (j.prepared && j.status === "new") chips.append(el("span", "chip acc", "Answers ready"));
    if (j.no_sponsorship) chips.append(el("span", "chip bad", "No sponsorship"));
    else if (j.sponsors) chips.append(el("span", "chip good", "Sponsors"));
    if (j.level) chips.append(el("span", "chip", j.level));
    info.append(chips);
    r.append(scoreRing(j.score, s), info);
    r.onclick = () => selectJob(j.id);
    box.append(r);
  }
}
async function selectJob(id){
  jsel = id;
  for (const r of document.querySelectorAll(".jrow")) r.classList.toggle("on", r.dataset.id === id);
  $("v-jobs").classList.add("detail");
  const d = $("jdetail");
  d.replaceChildren(emptyState("jobs", "Loading...", ""));
  let j;
  try { j = await api("api/jobs/detail?id=" + encodeURIComponent(id)); } catch (e) { d.replaceChildren(emptyState("x", "Couldn't load this job", e.message)); return; }
  if (jsel !== id) return;
  renderDetail(j);
  d.scrollTop = 0;
}
function closeDetail(){ jsel = null; $("v-jobs").classList.remove("detail"); for (const r of document.querySelectorAll(".jrow")) r.classList.remove("on"); }
async function setStatus2(id, status, msg){
  try { await send("api/jobs/status", "POST", {id, status}); toast(msg); } catch (e) { return toast(e.message, true); }
  jsel = null; $("jdetail").replaceChildren(); $("v-jobs").classList.remove("detail"); listSig = ""; loadJobs(true);
}
function copyBtn(text){
  const b = el("button", "btn ghost sm"); b.append(icon("copy"), el("span", null, "Copy"));
  b.onclick = () => navigator.clipboard.writeText(text).then(() => toast("Copied"));
  return b;
}
function answerInput(a){
  let input;
  const opts = a.options || [];
  if (opts.length && opts.length <= 40) {
    input = el("select", "inp");
    for (const o of [""].concat(opts)) { const op = el("option", null, o || "Choose..."); op.value = o; input.append(op); }
    if (a.kind !== "you" && a.a && !opts.includes(a.a)) { const op = el("option", null, a.a); op.value = a.a; input.append(op); }
  } else input = el(a.kind === "draft" || (a.a || "").length > 70 ? "textarea" : "input", "inp");
  const orig = a.kind === "you" ? "" : (a.a || "");
  input.value = orig;
  if (a.kind === "you" && input.tagName !== "SELECT") input.placeholder = /Application profile/.test(a.a || "") ? a.a : "Your answer";
  return {input, orig};
}
const KINDTXT = {fact: "From your profile", draft: "Drafted by the local model. Check every claim.", you: "Needs your answer", legal: "Consent", file: "File", eeo: "Voluntary"};
function renderDetail(j){
  const d = $("jdetail"); d.replaceChildren();
  const wrap = el("div", "jd");
  const back = el("button", "btn ghost sm jback"); back.append(icon("back"), el("span", null, "Jobs")); back.onclick = closeDetail;
  back.style.marginBottom = "12px";
  wrap.append(back);
  const h = el("div", "jdh");
  const ht = el("div"); ht.style.flex = "1"; ht.style.minWidth = "0";
  ht.append(el("h2", null, j.title), el("div", "jm", [j.company, j.location, j.level, j.years != null ? j.years + "+ years" : ""].filter(Boolean).join(" · ")));
  h.append(ht, scoreRing(j.score, S || {strong: 72, good: 60}, true));
  wrap.append(h);
  const flags = el("div", "chips"); flags.style.marginTop = "10px";
  const sc = statusChip(j.status); if (sc) flags.append(sc);
  if (j.no_sponsorship) flags.append(el("span", "chip bad", "Won't sponsor"));
  else if (j.sponsors) flags.append(el("span", "chip good", "Sponsors visas"));
  if (flags.childNodes.length) wrap.append(flags);
  if (j.summary) wrap.append(el("div", "jsum", j.summary));
  const has = j.has_skills || [], miss = j.missing_skills || [];
  if (has.length + miss.length) {
    const sk = el("div", "chips skills");
    for (const x of has) { const c = el("span", "chip good"); c.append(icon("check"), document.createTextNode(x)); sk.append(c); }
    for (const x of miss) sk.append(el("span", "chip bad", x));
    wrap.append(el("div", "muted small", "Has " + has.length + " of " + (has.length + miss.length) + " required skills"), sk);
  }
  // actions
  const act = el("div", "actions");
  if (j.apply_url && j.apply_url.startsWith("https://")) {
    const a = el("a", "btn ghost"); a.href = j.apply_url; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.append(icon("ext"), el("span", null, "Open application")); act.append(a);
  }
  const STATUS_ACTIONS = {
    new: [["skipped", "Skip", "Skipped"], ["applied", "Mark applied", "Marked applied"]],
    approved: [["new", "Back to review", "Moved back to review"], ["applied", "Mark applied", "Marked applied"]],
    applied: [["interview", "Interviewing", "Nice! Marked interviewing"], ["rejected", "Rejected", "Marked rejected"], ["new", "Back to new", "Moved back"]],
    interview: [["rejected", "Rejected", "Marked rejected"], ["applied", "Back to applied", "Moved back"]],
    rejected: [["new", "Back to new", "Moved back"]], skipped: [["new", "Back to new", "Moved back"]],
  };
  const reviewable = j.answers && j.status === "new" && j.auto_apply;
  for (const [st, label, msg] of STATUS_ACTIONS[j.status] || []) {
    if (reviewable && st === "skipped") continue;  // Skip sits next to Approve
    const b = el("button", "btn ghost", label); b.onclick = () => setStatus2(j.id, st, msg); act.append(b);
  }
  wrap.append(act);
  // emails
  if (j.emails && j.emails.length) {
    const sec = el("div", "sec"); sec.append(el("div", "sech", "Emails"));
    for (const m of j.emails) sec.append(mailCard(m, false));
    wrap.append(sec);
  }
  d.append(wrap);
  if (reviewable) reviewForm(j, wrap, d);
  else {
    if (j.answers) readOnlyAnswers(j, wrap);
    else {
      const sec = el("div", "sec"); sec.append(el("div", "sech", "Answers"));
      const b = el("button", "btn primary"); b.append(icon("sparkle"), el("span", null, "Prepare answers"));
      b.onclick = async () => { b.disabled = true; b.lastChild.textContent = "Preparing, a few minutes..."; try { await send("api/jobs/prepare", "POST", {id: j.id}); toast("Preparing answers in the background"); } catch (e) { toast(e.message, true); } };
      sec.append(el("div", "muted small", "No answers prepared for this job yet."), b);
      sec.lastChild.style.marginTop = "10px";
      wrap.append(sec);
    }
    posting(j, wrap);
  }
}
function posting(j, wrap){
  const dt = el("details", "sec"); dt.append(el("summary", null, "Posting text"), pre(j.description || "(none)"));
  const gap = el("div"); gap.style.height = "28px";
  wrap.append(dt, gap);
}
function readOnlyAnswers(j, wrap){
  const groups = [["you", "Needs you"], ["legal", "Consents"], ["draft", "Drafts"], ["fact", "From your profile"], ["eeo", "Voluntary"], ["file", "Files"]];
  for (const [k, title] of groups) {
    const items = j.answers.filter(a => a.kind === k);
    if (!items.length) continue;
    const sec = el("div", "sec");
    const hd = el("div", "sech", title); hd.append(el("span", "chip", String(items.length)));
    sec.append(hd);
    for (const a of items) {
      const q = el("div", "q");
      const ql = el("div", "ql"); if (a.required) ql.append(el("span", "req", "* ")); ql.append(document.createTextNode(a.q));
      q.append(ql);
      if (a.a) q.append(el("div", "qa" + (k === "fact" || k === "draft" ? "" : " muted"), a.a));
      if ((k === "fact" || k === "draft") && a.a) { const c = copyBtn(a.a); c.style.marginTop = "8px"; q.append(c); }
      sec.append(q);
    }
    wrap.append(sec);
  }
  if (j.note) wrap.append(el("div", "muted small", j.note));
}
function reviewForm(j, wrap, d){
  const rows = [], consents = [];
  const byKind = k => j.answers.filter(a => a.kind === k);
  const need = byKind("you").sort((a, b) => (b.required ? 1 : 0) - (a.required ? 1 : 0));
  const left = el("span", "left");
  const refresh = () => {
    const miss = rows.filter(r => r.need && !r.input.value.trim()).length + consents.filter(c => c.required && !c.box.checked).length;
    left.textContent = miss ? miss + " required " + (miss === 1 ? "item" : "items") + " left" : "Ready to approve";
    left.style.color = miss ? "var(--warn)" : "var(--good)";
  };
  const qbox = (a, withInput) => {
    const q = el("div", "q");
    const ql = el("div", "ql"); if (a.required) ql.append(el("span", "req", "* ")); ql.append(document.createTextNode(a.q));
    q.append(ql, el("div", "qk", KINDTXT[a.kind] || a.kind));
    if (withInput) {
      const {input, orig} = answerInput(a);
      q.append(input);
      const r = {q: a.q, input, orig, need: a.required && a.kind === "you"};
      if (r.need) q.classList.add("need");
      input.addEventListener("input", () => { if (r.need) q.classList.toggle("need", !input.value.trim()); refresh(); });
      input.addEventListener("change", () => { if (r.need) q.classList.toggle("need", !input.value.trim()); refresh(); });
      rows.push(r);
    }
    return q;
  };
  const section = (title, count, extra) => {
    const sec = el("div", "sec");
    const hd = el("div", "sech", title); hd.append(el("span", "chip" + (extra || ""), String(count)));
    sec.append(hd); wrap.append(sec); return sec;
  };
  if (need.length) { const sec = section("Needs you", need.length, " warn"); for (const a of need) sec.append(qbox(a, true)); }
  const legal = byKind("legal");
  if (legal.length) {
    const sec = section("Statements you agree to", legal.length);
    if (legal.length > 1) {
      const all = el("button", "btn ghost sm", "Agree to all " + legal.length);
      all.onclick = () => { for (const c of consents) c.box.checked = true; refresh(); };
      all.style.marginLeft = "auto"; sec.firstChild.append(all);
    }
    for (const a of legal) {
      const q = el("div", "q");
      const lab = el("label", "consent");
      const box = el("input"); box.type = "checkbox";
      const txt = el("div");
      const ql = el("div", "ql"); if (a.required) ql.append(el("span", "req", "* ")); ql.append(document.createTextNode(a.q));
      txt.append(ql, el("div", "qk", a.required ? "Required to apply. Only tick it if it's true for you." : "Optional. Left blank unless you tick it."));
      lab.append(box, txt); q.append(lab); sec.append(q);
      box.onchange = refresh;
      consents.push({q: a.q, box, required: a.required});
    }
  }
  const drafts = byKind("draft");
  if (drafts.length) { const sec = section("Drafts", drafts.length, " acc"); for (const a of drafts) sec.append(qbox(a, true)); }
  const facts = byKind("fact");
  if (facts.length) {
    const dt = el("details", "sec");
    const sm = el("summary", null, "From your profile"); sm.append(el("span", "chip good", String(facts.length)));
    dt.append(sm);
    for (const a of facts) dt.append(qbox(a, true));
    wrap.append(dt);
  }
  const other = j.answers.filter(a => a.kind === "file" || a.kind === "eeo");
  if (other.length) {
    const dt = el("details", "sec");
    dt.append(el("summary", null, "Resume and voluntary questions"));
    for (const a of other) {
      const q = el("div", "q");
      q.append(el("div", "ql", a.q), el("div", "qa muted", a.kind === "file" ? "Your resume PDF is attached automatically." : "Voluntary. Left blank; set it in You › Application profile to share it."));
      dt.append(q);
    }
    wrap.append(dt);
  }
  if (j.note) wrap.append(el("div", "muted small", j.note));
  posting(j, wrap);
  // sticky approve bar
  const foot = el("div", "stickyfoot");
  const skip = el("button", "btn ghost", "Skip");
  skip.onclick = () => setStatus2(j.id, "skipped", "Skipped");
  const ok = el("button", "btn primary"); ok.append(icon("check"), el("span", null, "Approve"));
  ok.onclick = async () => {
    const answers = rows.filter(r => r.input.value.trim() && r.input.value.trim() !== r.orig).map(r => ({q: r.q, a: r.input.value.trim()}));
    const agreed = consents.filter(c => c.box.checked).map(c => c.q);
    ok.disabled = true;
    try { await send("api/jobs/approve", "POST", {id: j.id, answers, agreed}); }
    catch (e) { ok.disabled = false; return toast(e.message, true); }
    toast("Approved. It's in Apply to approved.");
    jsel = null; $("jdetail").replaceChildren(); $("v-jobs").classList.remove("detail"); listSig = ""; applySig = ""; loadJobs(true);
  };
  foot.append(left, skip, ok);
  d.append(foot);
  refresh();
}
async function updateApply(n){
  const a = $("japply");
  if (!n) { a.style.display = "none"; applySig = ""; return; }
  if (applySig === String(n) && a.href) return;
  try {
    const r = await api("api/jobs/next");
    if (!r.job || !r.job.apply_url.startsWith("https://")) { a.style.display = "none"; return; }
    a.href = r.job.apply_url + "#agent-auto";
    a.querySelector(".long").textContent = "Apply to approved (" + r.left + ")";
    a.querySelector(".short").textContent = "Apply (" + r.left + ")";
    a.style.display = "";
    applySig = String(n);
  } catch (e) {}
}
$("jrun").onclick = () => send("api/jobs/run", "POST").then(() => { toast("Search started"); setTimeout(() => loadJobs(), 600); }, e => toast(e.message, true));

// ---------- inbox ----------
const MAIL_KIND = {interview: ["good", "Interview"], rejection: ["bad", "Rejection"], confirmation: ["acc", "Application received"], verification: ["warn", "Verification"], other: ["", "Other"]};
let inboxSig = "", ifilter = "all";
function mailCard(m, withJob){
  const c = el("div", "mail");
  const top = el("div", "chips");
  const k = MAIL_KIND[m.kind] || ["", m.kind];
  top.append(el("span", "chip " + k[0], k[1]));
  if (withJob && m.company) top.append(el("span", "chip", m.company));
  c.append(top, el("div", "mt", m.subject || "(no subject)"), el("div", "muted small", [m.from, when(m.date), withJob && m.title ? m.title : ""].filter(Boolean).join(" · ")));
  if (m.code) {
    c.append(el("div", "code", m.code));
    const b = copyBtn(m.code); b.lastChild.textContent = "Copy code"; c.append(b);
  }
  if (m.snippet) { const d = el("details"); d.style.marginTop = "6px"; d.append(el("summary", null, "Email text"), pre(m.snippet)); c.append(d); }
  return c;
}
function inboxSetup(box){
  const c = el("div", "card");
  const h = el("div"); h.style.cssText = "display:flex;align-items:center;gap:10px;margin-bottom:8px";
  const lg = el("div", "logo"); lg.append(icon("mail")); h.append(lg, el("b", null, "Connect the agent's inbox"));
  c.append(h, el("div", "muted small", "An address just for applications. Every 5 minutes the agent reads new mail without marking it read: confirmations mark jobs Applied, interview requests and rejections move them along, and verification codes show up here and as alerts. Links in emails are never opened. Use an app password: Gmail › Google Account › Security › App passwords. Your profile's email becomes this address."));
  const addr = el("input", "inp"); addr.type = "email"; addr.placeholder = "you.applications@gmail.com"; addr.autocomplete = "off";
  const pw = el("input", "inp"); pw.type = "password"; pw.placeholder = "App password"; pw.autocomplete = "new-password";
  const f1 = el("div", "field"); f1.append(el("label", null, "Address"), addr);
  const f2 = el("div", "field"); f2.append(el("label", null, "App password"), pw);
  f1.style.marginTop = "14px";
  const b = el("button", "btn primary", "Connect");
  b.onclick = async () => {
    b.disabled = true; b.textContent = "Checking the login...";
    try { await send("api/inbox", "POST", {address: addr.value, password: pw.value}); pw.value = ""; toast("Inbox connected"); inboxSig = ""; loadInbox(); }
    catch (e) { toast(e.message, true); }
    b.disabled = false; b.textContent = "Connect";
  };
  c.append(f1, f2, b);
  box.append(c);
}
async function loadInbox(){
  let s;
  try { s = await api("api/inbox"); } catch (e) { return; }
  const fresh = (s.messages || []).filter(m => (m.kind === "interview" || m.kind === "verification") && m.date > (store("inboxSeen") || ""));
  badge("inbox", fresh.length);
  if (view !== "inbox") return;
  if (s.messages && s.messages[0]) store("inboxSeen", s.messages[0].date);
  badge("inbox", 0);
  const sig = JSON.stringify(s) + ifilter;
  if (sig === inboxSig) return;
  inboxSig = sig;
  const box = $("ibody"); box.replaceChildren();
  $("icheck").style.display = $("ioff").style.display = s.configured ? "" : "none";
  $("ifilterbar").style.display = s.configured ? "" : "none";
  if (!s.configured) { $("imeta").textContent = ""; return inboxSetup(box); }
  $("imeta").textContent = s.address + " · checked " + (when(s.checked) || "not yet");
  if (s.error) box.append(el("div", "chip bad", s.error));
  const f = $("ifilter"); f.replaceChildren();
  const cnt = {all: s.messages.length};
  for (const m of s.messages) cnt[m.kind] = (cnt[m.kind] || 0) + 1;
  for (const [k, label] of [["all", "All"], ["interview", "Interviews"], ["verification", "Codes"], ["confirmation", "Received"], ["rejection", "Rejections"], ["other", "Other"]]) {
    const b = el("button", k === ifilter ? "on" : ""); b.append(document.createTextNode(label));
    if (cnt[k]) b.append(el("span", "n", String(cnt[k])));
    b.onclick = () => { ifilter = k; inboxSig = ""; loadInbox(); };
    f.append(b);
  }
  const list = s.messages.filter(m => ifilter === "all" || m.kind === ifilter);
  if (!list.length) box.append(emptyState("inbox", "No emails here", "Emails about your applications show up here."));
  for (const m of list) box.append(mailCard(m, true));
}
$("icheck").onclick = async () => {
  $("icheck").disabled = true;
  try { const r = await send("api/inbox/check", "POST"); toast(r.new ? r.new + " new" : "No new mail"); } catch (e) { toast(e.message, true); }
  $("icheck").disabled = false; inboxSig = ""; loadInbox();
};
$("ioff").onclick = async () => {
  if (!confirm("Disconnect the inbox? The saved login and the email list are deleted from the server.")) return;
  await fetch("api/inbox", {method: "DELETE"}); inboxSig = ""; loadInbox();
};

// ---------- you: profile and memory ----------
function showYou(sub){
  store("yousub", sub);
  for (const b of $("yousub").children) b.classList.toggle("on", b.dataset.s === sub);
  $("y-profile").style.display = sub === "profile" ? "" : "none";
  $("y-memory").style.display = sub === "memory" ? "" : "none";
  if (sub === "profile") loadProfile(); else loadMemory();
}
for (const b of $("yousub").children) b.onclick = () => showYou(b.dataset.s);
let profileDirty = false;
function pInput(value, choices, multi){
  let input;
  if (choices) {
    input = el("select", "inp");
    const opts = [""].concat(choices);
    if (value && !choices.includes(value)) opts.push(value);
    for (const c of opts) { const o = el("option", null, c || "Choose..."); o.value = c; input.append(o); }
  } else if (multi) input = el("textarea", "inp");
  else { input = el("input", "inp"); input.autocomplete = "off"; }
  input.value = value || "";
  return input;
}
function pField(label, input, help, wide){
  const f = el("div", "field pfield" + (input.value ? "" : " empty") + (wide ? " wide" : ""));
  f.append(el("label", null, label), input);
  if (help) f.append(el("div", "help", help));
  const on = () => { profileDirty = true; f.classList.toggle("empty", !input.value.trim()); $("pdirty").textContent = "Unsaved changes"; progress(); };
  input.addEventListener("input", on); input.addEventListener("change", on);
  return f;
}
function progress(){
  const all = [...$("pform").querySelectorAll("[data-key]")];
  const filled = all.filter(i => i.value.trim()).length;
  $("pbar").style.width = (all.length ? Math.round(filled / all.length * 100) : 0) + "%";
  $("pstat").textContent = filled + " of " + all.length + " filled";
}
async function loadProfile(){
  if (profileDirty) return;
  let p;
  try { p = await api("api/jobs/profile"); } catch (e) { return; }
  const box = $("pform"), jump = $("pjump");
  box.replaceChildren(); jump.replaceChildren();
  const sections = p.form.map(s => [s.section, s.fields]);
  let n = 0;
  for (const [name, fields] of sections) {
    const id = "ps" + (n++);
    const sec = el("div", "psec"); sec.id = id;
    sec.append(el("h3", null, name));
    const grid = el("div", "pgrid");
    for (const f of fields) {
      const input = pInput(p.profile[f.key], f.choices, false);
      input.dataset.key = f.key;
      grid.append(pField(f.label, input, f.help, (f.help || "").length > 70));
    }
    sec.append(grid); box.append(sec);
    const j = el("button", "chip", name); j.onclick = () => sec.scrollIntoView({behavior: "smooth", block: "start"}); jump.append(j);
  }
  const extra = [["Questions it still can't answer", p.unanswered, "q"], ["Your saved answers", p.answers, "a"]];
  for (const [name, items, kind] of extra) {
    const sec = el("div", "psec"); sec.id = "ps" + (n++);
    const h = el("h3", null, name); h.append(el("span", "chip" + (kind === "q" && items.length ? " warn" : ""), String(items.length)));
    sec.append(h);
    if (kind === "q") sec.append(el("div", "muted small", items.length ? "From the " + p.prepared_jobs + " jobs with prepared answers, most common first. Answer once and every form that asks gets it. Leave empty to skip." : "None right now. Questions show up here as jobs get prepared."));
    if (kind === "a" && !items.length) sec.append(el("div", "muted small", "None yet. Clear one to remove it."));
    const grid = el("div"); grid.style.marginTop = "12px";
    for (const u of items) {
      if (kind === "q") {
        const input = pInput("", u.options.length && u.options.length <= 12 ? u.options : null, !u.options.length);
        input.dataset.q = u.q;
        const where = u.jobs + (u.jobs === 1 ? " job" : " jobs") + ": " + u.companies.join(", ");
        grid.append(pField(u.q, input, where + (u.options.length > 12 ? ". Choices include " + u.options.slice(0, 6).join(", ") : ""), true));
      } else {
        const input = pInput(u.a, null, true); input.dataset.q = u.q;
        grid.append(pField(u.q, input, "", true));
      }
    }
    sec.append(grid); box.append(sec);
    const j = el("button", "chip" + (kind === "q" && items.length ? " warn" : ""), kind === "q" ? "Open questions" : "Saved answers");
    j.onclick = () => sec.scrollIntoView({behavior: "smooth", block: "start"}); jump.append(j);
  }
  $("pdirty").textContent = "";
  progress();
}
$("psave").onclick = async () => {
  const values = {}, answers = [];
  for (const i of $("pform").querySelectorAll("[data-key]")) values[i.dataset.key] = i.value.trim();
  for (const i of $("pform").querySelectorAll("[data-q]")) if (i.value.trim()) answers.push({q: i.dataset.q, a: i.value.trim()});
  try { await send("api/jobs/profile", "PUT", {values, answers}); } catch (e) { return toast(e.message, true); }
  profileDirty = false; toast("Profile saved"); loadProfile();
};
let memMax = 2000;
function memCount(){ const n = $("memtext").value.length; $("memcount").textContent = n + " / " + memMax + " characters"; }
async function loadMemory(){
  try { const m = await api("api/memory"); $("memtext").value = m.text; memMax = m.max; } catch (e) {}
  memCount();
}
$("memtext").oninput = memCount;
$("memsave").onclick = async () => {
  try { await send("api/memory", "PUT", {text: $("memtext").value}); toast("Memory saved"); } catch (e) { toast(e.message, true); }
};

// ---------- start ----------
if ("serviceWorker" in navigator) navigator.serviceWorker.addEventListener("message", e => { if (e.data && e.data.view) showView(e.data.view); });
window.addEventListener("hashchange", () => { const v = location.hash.slice(1); if (v && (OLD[v] || v) !== view) showView(v); });
setInterval(() => { if (view === "jobs") loadJobs(); }, 5000);
setInterval(() => { if (view !== "jobs") loadJobs(); loadInbox(); }, 60000);
setInterval(() => { if (view === "inbox") loadInbox(); }, 15000);
let startView = location.hash.slice(1) || store("view") || "chat";
showView(startView);
setInterval(poll, 1500);
poll();
loadJobs();
loadInbox();
setupAlerts();
</script>
</body></html>
"""

# Installed in Sai's browser (Userscripts on iPhone Safari, Tampermonkey or Userscripts on
# the Mac). On any form it fills what it can and leaves the rest to Sai. Applications Sai
# approved in the panel it also submits, but only when opened from "Apply to approved"
# (#agent-auto): it ticks the consents he agreed to, checks every required field is
# filled, clicks Submit after a countdown he can stop, waits for the confirmation and
# opens the next one. CAPTCHA challenges stay with Sai. Page text is only ever set with
# textContent.
FILL_SCRIPT = r"""// ==UserScript==
// @name         Agent application autofill
// @namespace    local-agent
// @version      4
// @description  Fills job application forms from the agent panel, and submits the ones you approved there.
// @match        https://job-boards.greenhouse.io/*
// @match        https://boards.greenhouse.io/*
// @match        https://jobs.lever.co/*
// @match        https://jobs.ashbyhq.com/*
// @grant        GM.xmlHttpRequest
// @grant        GM_xmlhttpRequest
// @connect      __HOST__
// @updateURL    __PANEL__/jobs-fill.user.js
// @downloadURL  __PANEL__/jobs-fill.user.js
// ==/UserScript==
(function () {
  "use strict";
  const PANEL = "__PANEL__";
  const KIND = {legal: "Read and answer yourself", eeo: "Voluntary, your choice", you: "Answer yourself",
                file: "Attach the file yourself", draft: "No draft for this one, answer yourself"};
  const COLORS = {filled: "#2f9e62", review: "#2f6fd6", you: "#e08a1e"};
  const AGREE_RE = /^(yes|i agree|agree|i acknowledge|acknowledge|i confirm|confirm|i have read|i accept|accept|i understand|i certify|i consent|consent)/i;
  const SUCCESS_RE = /thank(s| you) for (applying|your application|submitting)|application (has been |was )?(submitted|received)|we('ve| have) received your application|successfully (submitted|applied)/i;
  const AUTO_KEY = "agent-auto", SENT_KEY = "agent-submitted";
  const store = {  // this tab only; the next job's link carries #agent-auto across sites
    get: k => { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) {} },
    del: k => { try { sessionStorage.removeItem(k); } catch (e) {} },
  };
  if (location.hash.includes("agent-auto")) store.set(AUTO_KEY, "1");
  let stopped = false;
  const gmx = (typeof GM !== "undefined" && GM.xmlHttpRequest) ? GM.xmlHttpRequest.bind(GM)
            : (typeof GM_xmlhttpRequest !== "undefined" ? GM_xmlhttpRequest : null);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const clean = t => (t || "").replace(/[*✱]/g, "").replace(/\s+/g, " ").trim();
  // Forms spell some answers differently: "United States" is "US" on Stripe's form.
  const ALIASES = {"united states": "us", "united states of america": "us", "usa": "us", "u s": "us", "u s a": "us",
                   "united kingdom": "uk", "great britain": "uk"};
  const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const alias = s => ALIASES[norm(s)] || norm(s);

  function api(method, path, body) {
    return new Promise((resolve, reject) => {
      if (!gmx) return reject(new Error("This userscript manager can't make cross-site requests."));
      gmx({
        method, url: PANEL + path, timeout: 20000,
        headers: {"Content-Type": "application/json"},
        data: body ? JSON.stringify(body) : undefined,
        onload: r => (r.status >= 200 && r.status < 300)
          ? resolve(JSON.parse(r.responseText)) : reject(new Error("The panel answered " + r.status)),
        onerror: () => reject(new Error("Can't reach the panel. Is Tailscale on?")),
        ontimeout: () => reject(new Error("The panel didn't answer in time.")),
      });
    });
  }

  const byIds = ids => clean((ids || "").split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(n => n.innerText).join(" "));

  // The question text for a field: the nearest label-like element around it that
  // holds no inputs of its own (so a "No" radio label never names the question).
  function questionLabel(el) {
    for (let p = el.parentElement, k = 0; p && k < 7; p = p.parentElement, k++) {
      const t = byIds(p.getAttribute("aria-labelledby"));
      if (t) return t;
      const l = [...p.children].find(c => !c.contains(el) && !c.querySelector("input, select, textarea")
        && c.matches("legend, label, .application-label, [class*=label], [class*=Label], [class*=heading], [class*=title]")
        && clean(c.innerText));
      if (l) return clean(l.innerText);
    }
    return "";
  }

  function labelFor(el) {
    if (el.labels && el.labels.length && clean(el.labels[0].innerText)) return clean(el.labels[0].innerText);
    const t = byIds(el.getAttribute("aria-labelledby")) || clean(el.getAttribute("aria-label"));
    return t || questionLabel(el) || clean(el.placeholder || el.name || "");
  }

  // Upload inputs are usually labeled by their button ("Attach"); use the question's label.
  function fileLabel(el) {
    const own = labelFor(el);
    if (!/^(attach|upload|browse|choose file|select file)?$/i.test(own)) return own;
    return questionLabel(el) || (el.id || el.name || "").replace(/[_-]+/g, " ");
  }

  function collect() {
    const fields = [], els = [], radios = new Set();
    for (const el of document.querySelectorAll("input, textarea, select")) {
      const type = (el.type || "").toLowerCase();
      if (el.disabled || ["hidden", "submit", "button", "image", "reset", "password", "search"].includes(type)) continue;
      if (type === "checkbox") {
        // single boxes only (consents, opt-ins); groups of choices are left to Sai
        if (el.name && [...document.querySelectorAll("input[type=checkbox]")].filter(c => c.name === el.name).length > 1) continue;
        const own = labelFor(el), q = questionLabel(el);
        fields.push({label: own.length < 30 && q && q !== own ? q + " " + own : own, type: "checkbox", options: []});
        els.push(el);
        continue;
      }
      if (type === "file") { fields.push({label: fileLabel(el), type: "file", options: []}); els.push(el); continue; }
      if (el.getAttribute("aria-hidden") === "true" || el.tabIndex < 0) continue;  // validation helpers
      if (type === "radio") {
        if (!el.name || radios.has(el.name)) continue;
        radios.add(el.name);
        const group = [...document.querySelectorAll("input[type=radio]")].filter(r => r.name === el.name);
        fields.push({label: questionLabel(el), type: "radio", options: group.map(r => labelFor(r))});
        els.push(group);
        continue;
      }
      if (!el.offsetParent) continue;  // not shown
      const combo = el.getAttribute("role") === "combobox";
      fields.push({
        label: labelFor(el),
        type: el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select" : combo ? "combobox" : "text",
        options: el.tagName === "SELECT" ? [...el.options].map(o => o.text.trim()) : [],
      });
      els.push(el);
    }
    return {fields, els};
  }

  function setValue(el, v) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
                : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);  // works with React forms
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function pick(options, value) {
    if (!norm(value)) return -1;
    for (const f of [norm, alias]) {  // as written first, then "United States" as "US"
      const v = f(value);
      let i = options.findIndex(o => f(o) === v);
      if (i < 0) i = options.findIndex(o => { const n = f(o); return n && (v.startsWith(n + " ") || n.startsWith(v + " ")); });
      if (i >= 0) return i;
    }
    return -1;
  }

  // Option index for a dropdown answer. Location searches also accept the first word
  // ("Albany, NY" takes "Albany, New York, United States"); other dropdowns, like
  // schools, need a real match or are left for Sai.
  function pickLoose(labels, value, loose) {
    let i = pick(labels, value);
    const first = norm(value).split(" ")[0];
    if (i < 0 && loose) i = labels.findIndex(l => first && norm(l).startsWith(first));
    return i;
  }

  // react-select dropdowns (Greenhouse, Ashby) ignore scripted typing, and userscripts
  // usually run in an isolated world that can't see page components. This helper runs
  // in the page instead: it finds the component, starts its search and selects the
  // option. The two sides talk through attributes on the input.
  function pageHelper() {
    if (document.documentElement.hasAttribute("data-agent-helper")) return;
    document.documentElement.setAttribute("data-agent-helper", "1");
    const ALIASES = {"united states": "us", "united states of america": "us", "usa": "us", "u s": "us", "u s a": "us",
                     "united kingdom": "uk", "great britain": "uk"};
    const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const alias = s => ALIASES[norm(s)] || norm(s);
    const find = (raw, value, loose) => {  // as written first, then "United States" as "US"
      for (const f of [norm, alias]) {
        const labels = raw.map(f), v = f(value), first = v.split(" ")[0];
        let i = labels.findIndex(l => l === v);
        if (i < 0) i = labels.findIndex(l => l && (v.startsWith(l + " ") || l.startsWith(v + " ")));
        if (i < 0 && loose) i = labels.findIndex(l => first && l.startsWith(first));
        if (i >= 0) return i;
      }
      return -1;
    };
    const comp = el => {
      const fk = Object.keys(el).find(k => k.startsWith("__reactFiber"));
      for (let f = fk && el[fk], k = 0; f && k < 40; k++, f = f.return)
        if (f.stateNode && typeof f.stateNode.selectOption === "function") return f.stateNode;
      return null;
    };
    document.addEventListener("agent-fill-combo", async e => {
      const el = e.target, value = el.getAttribute("data-agent-fill") || "";
      const loose = el.getAttribute("data-agent-loose") === "1";
      let inst = comp(el);
      if (!inst) return el.setAttribute("data-agent-fill-result", "none");
      const labelOf = o => String((inst.props.getOptionLabel ? inst.props.getOptionLabel(o) : o.label) || "");
      if (typeof inst.props.loadOptions === "function") {
        // search-as-you-type lists (Greenhouse school, degree, discipline) load options
        // only when opened; ask the list's own loader, with the full answer and its first part
        for (const q of [...new Set([value, value.split(/,| - /)[0].trim()])]) {
          try {
            const r = await inst.props.loadOptions(q, [], {page: 1});
            const opts = (r && r.options) || [];
            const i = find(opts.map(labelOf), value, loose);
            if (i >= 0) { comp(el).selectOption(opts[i]); return el.setAttribute("data-agent-fill-result", "ok"); }
          } catch (err) {}
        }
      }
      if (inst.props.onInputChange) inst.props.onInputChange(value.split(",")[0], {action: "input-change", prevInputValue: ""});
      for (let t = 0; t < 12; t++) {  // options can load from the network
        await new Promise(r => setTimeout(r, 250));
        inst = comp(el);
        const opts = inst.props.options || [];
        const i = find(opts.map(labelOf), value, loose);
        if (i >= 0) { inst.selectOption(opts[i]); return el.setAttribute("data-agent-fill-result", "ok"); }
      }
      el.setAttribute("data-agent-fill-result", "no");
    }, true);
  }

  function injectHelper() {
    if (document.documentElement.hasAttribute("data-agent-helper")) return;
    const s = document.createElement("script");
    s.textContent = "(" + pageHelper.toString() + ")();";
    document.documentElement.append(s);  // blocked on sites whose policy forbids inline scripts
    s.remove();
  }

  async function fillCombo(el, value, loose) {
    injectHelper();
    if (document.documentElement.hasAttribute("data-agent-helper")) {
      el.removeAttribute("data-agent-fill-result");
      el.setAttribute("data-agent-fill", value);
      el.setAttribute("data-agent-loose", loose ? "1" : "0");
      el.dispatchEvent(new CustomEvent("agent-fill-combo", {bubbles: true}));
      for (let t = 0; t < 20; t++) {
        await sleep(250);
        const r = el.getAttribute("data-agent-fill-result");
        if (r === "ok") return true;
        if (r === "no") return false;
        if (r === "none") break;  // not a react-select box: type into it instead
      }
    }
    el.focus();
    setValue(el, value);
    for (let t = 0; t < 12; t++) {
      await sleep(250);
      // only this box's own list: the page can have others, like the phone country list
      const list = document.getElementById(el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "");
      const opts = list ? [...list.querySelectorAll("[role=option]")] : [];
      const i = pickLoose(opts.map(o => o.innerText), value, loose);
      if (i >= 0) {
        for (const ev of ["mousedown", "mouseup", "click"]) opts[i].dispatchEvent(new MouseEvent(ev, {bubbles: true}));
        return true;
      }
    }
    el.blur();
    return false;
  }

  async function put(el, f, v, kind) {
    if (kind === "consent") {  // only sent for applications Sai approved
      if (f.type === "checkbox") { if (!el.checked) el.click(); return true; }
      const i = f.options.findIndex(o => AGREE_RE.test(clean(o)));
      if (i < 0) return false;
      if (f.type === "radio") { el[i].click(); return true; }
      if (f.type === "select") { setValue(el, el.options[i].value); return true; }
      if (f.type === "combobox") return fillCombo(el, f.options[i], false);
      return false;
    }
    if (f.type === "checkbox") { if (/^yes$/i.test(v) && !el.checked) el.click(); return true; }
    if (f.type === "radio") { const i = pick(f.options, v); if (i < 0) return false; el[i].click(); return true; }
    if (f.type === "select") { const i = pick(f.options, v); if (i < 0) return false; setValue(el, el.options[i].value); return true; }
    if (f.type === "combobox") return fillCombo(el, v, /location|city/i.test(f.label));
    if (!el.value || !el.value.trim()) setValue(el, v);  // never overwrite what's there
    return true;
  }

  function mark(el, state) {
    // outline what's visible: the question around radios, the box around dropdowns and uploads
    const target = Array.isArray(el) ? (el[0].closest("fieldset, [role=radiogroup], li") || el[0].parentElement)
      : el.getAttribute("role") === "combobox" ? (el.closest("[class*=control]") || el)
      : el.type === "file" ? (el.closest("[class*=upload], [class*=Upload]") || el.parentElement) : el;
    if (!target || !target.style) return;
    target.style.outline = "3px solid " + COLORS[state];
    target.style.outlineOffset = "2px";
  }

  async function attachResume(el, name) {
    const {data} = await api("GET", "/api/jobs/resume");
    const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], name, {type: "application/pdf"}));
    el.files = dt.files;
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }

  // ---------- floating panel ----------

  let box, body, pill;
  function node(tag, text, style) {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (style) e.style.cssText = style;
    return e;
  }
  const BTN = "font:600 14px -apple-system,system-ui,sans-serif;border:0;border-radius:8px;padding:9px 12px;cursor:pointer;";
  const SHADOW = "z-index:2147483647;box-shadow:0 6px 24px rgba(0,0,0,.35);font:14px/1.4 -apple-system,system-ui,sans-serif;";

  // Minimized, the panel is a small round button in the corner, so it never
  // covers the form. The choice is remembered for the site.
  function minimize(on) {
    box.style.display = on ? "none" : "block";
    pill.style.display = on ? "block" : "none";
    try { localStorage.setItem("agent-fill-min", on ? "1" : "0"); } catch (e) {}
  }

  function ui() {
    box = node("div", undefined, SHADOW + "position:fixed;right:12px;bottom:12px;width:min(340px,calc(100vw - 24px));background:#1f1e1c;color:#ebeae4;border-radius:12px;padding:10px");
    const row = node("div", undefined, "display:flex;gap:8px");
    const go = node("button", "Fill from agent", BTN + "background:#57a37a;color:#fff;flex:1");
    go.onclick = () => { go.disabled = true; run().finally(() => { go.disabled = false; go.textContent = "Fill again"; }); };
    const min = node("button", "\u2013", BTN + "background:#33322e;color:#ebeae4;width:40px");
    min.title = "Minimize";
    min.setAttribute("aria-label", "Minimize");
    min.onclick = () => minimize(true);
    row.append(go, min);
    body = node("div", undefined, "max-height:45vh;overflow:auto");
    box.append(row, body);
    pill = node("button", "Agent", SHADOW + BTN + "position:fixed;right:12px;bottom:12px;background:#57a37a;color:#fff;border-radius:22px;padding:10px 14px;display:none");
    pill.setAttribute("aria-label", "Show the autofill panel");
    pill.onclick = () => minimize(false);
    document.body.append(box, pill);
    let saved = "0";
    try { saved = localStorage.getItem("agent-fill-min") || "0"; } catch (e) {}
    minimize(saved === "1");
  }
  function say(text) { body.replaceChildren(node("div", text, "margin-top:8px")); }

  async function run() {
    say("Reading the form...");
    const {fields, els} = collect();
    if (!fields.length) return say("No form fields on this page.");
    let res;
    try { res = await api("POST", "/api/jobs/fill", {url: location.href, fields}); }
    catch (e) { return say(e.message); }
    const todo = [], counts = {filled: 0, review: 0, you: 0};
    // the resume first: some forms fill fields from it and would overwrite ours
    for (const a of res.answers) {
      if (a.kind !== "file" || fields[a.i].type !== "file") continue;
      try { await attachResume(els[a.i], res.resume_name); mark(els[a.i], "filled"); counts.filled++; }
      catch (e) { todo.push([fields[a.i].label, "Resume: " + e.message]); mark(els[a.i], "you"); counts.you++; }
      await sleep(1500);
    }
    for (const a of res.answers) {
      const el = els[a.i], f = fields[a.i];
      if (a.kind === "file" && f.type === "file") continue;
      let state = "you";
      if (a.value && f.type !== "file") {
        if (await put(el, f, a.value, a.kind)) state = a.kind === "draft" ? "review" : "filled";
        else todo.push([f.label, "Couldn't choose an option for: " + a.value]);
      } else if (a.value) {
        todo.push([f.label, "Upload this as a file or paste it: ", a.value]);
      } else if (f.label) {
        todo.push([f.label, KIND[a.kind] || "Answer yourself"]);
      }
      mark(el, state);
      counts[state]++;
    }
    const out = [node("div", (res.job ? res.job.company + ": " + res.job.title : "Not a job from the agent; filled from your profile.") , "margin-top:8px;font-weight:600"),
                 node("div", counts.filled + " filled (green), " + counts.review + " drafts to review (blue), " + counts.you + " for you (orange). Check everything, solve the CAPTCHA and submit yourself.", "margin-top:4px")];
    for (const [q, what, text] of todo) {
      const row = node("div", undefined, "margin-top:8px;border-top:1px solid #33322e;padding-top:6px");
      row.append(node("div", q, "font-weight:600"), node("div", what, "color:#9b9992"));
      if (text) {
        const copy = node("button", "Copy text", BTN + "background:#33322e;color:#ebeae4;margin-top:4px");
        copy.onclick = () => navigator.clipboard.writeText(text).then(() => { copy.textContent = "Copied"; });
        row.append(copy);
      }
      out.push(row);
    }
    lastRes = res;
    if (res.job) {
      const done = node("button", "Mark applied in the panel", BTN + "background:#2f6fd6;color:#fff;margin-top:10px;width:100%");
      done.onclick = () => api("POST", "/api/jobs/status", {id: res.job.id, status: "applied"})
        .then(() => { done.textContent = "Marked applied"; done.disabled = true; }, e => { done.textContent = e.message; });
      out.push(done);
    }
    body.replaceChildren(...out);
    return res;
  }

  // ---------- submitting approved applications ----------

  let lastRes = null;
  const visible = e => !!(e && e.offsetParent !== null && e.getClientRects().length);

  // Required fields still empty, by their question text
  function missingRequired() {
    const out = [], seen = new Set();
    for (const el of document.querySelectorAll("input, textarea, select")) {
      const req = el.required || el.getAttribute("aria-required") === "true";
      if (!req || el.disabled || el.type === "hidden") continue;
      if (el.getAttribute("aria-hidden") === "true" && el.tabIndex < 0) continue;  // react-select's own validation input
      let empty;
      if (el.type === "radio") {
        if (seen.has(el.name)) continue;
        seen.add(el.name);
        empty = ![...document.querySelectorAll("input[type=radio]")].some(r => r.name === el.name && r.checked);
      } else if (el.type === "checkbox") empty = !el.checked;
      else if (el.type === "file") empty = !(el.files && el.files.length) && !visible(el.closest("[class*=upload], [class*=Upload]")?.querySelector("[class*=filename], [class*=file-name], [class*=FileName]"));
      else if (el.getAttribute("role") === "combobox") {
        // react-select shows the choice next to the input, inside the control
        const box = el.closest("[class*=__control], [class*=-control], [class*=Control]");
        empty = !(box && box.querySelector("[class*=single-value], [class*=singleValue], [class*=multi-value], [class*=multiValue]"));
      } else {
        if (!visible(el)) continue;
        empty = !String(el.value || "").trim();
      }
      if (empty) out.push(labelFor(el) || questionLabel(el) || el.name || "A required field");
    }
    return out;
  }

  function submitButton() {
    return [...document.querySelectorAll("button, input[type=submit]")].find(b => visible(b) && !b.disabled
      && /^(submit( your)?( application)?|apply|send application)$/i.test(clean(b.innerText || b.value)));
  }

  const challengeOpen = () => [...document.querySelectorAll("iframe")].some(f =>
    /recaptcha.*bframe|hcaptcha.*challenge|challenge/i.test(f.src + " " + (f.title || "")) && visible(f) && f.offsetHeight > 100);
  const succeeded = () => SUCCESS_RE.test(document.body.innerText) || /\/(thanks|confirmation|thank_you|success)\b/i.test(location.pathname);

  function row(...nodes) { const d = node("div", undefined, "margin-top:8px"); d.append(...nodes); return d; }
  function stopButton(label) {
    const b = node("button", label || "Stop applying", BTN + "background:#a3392b;color:#fff;margin-top:8px;width:100%");
    b.onclick = () => { stopped = true; store.del(AUTO_KEY); store.del(SENT_KEY); say("Stopped. Approved applications stay approved; open Apply to approved in the panel to go on."); };
    return b;
  }

  // After Submit: wait for the confirmation (this page or the next one), then move on.
  async function watch(job) {
    for (let t = 0; t < 240 && !stopped; t++) {  // 2 minutes, then a CAPTCHA may still be open
      if (succeeded()) return finished(job);
      if (challengeOpen()) body.replaceChildren(row(node("div", "Solve the CAPTCHA for " + job.company + ". It goes on by itself after that.")), stopButton());
      await sleep(500);
    }
    if (stopped) return;
    const again = node("button", "It went through", BTN + "background:#2f6fd6;color:#fff;margin-top:8px;width:100%");
    again.onclick = () => finished(job);
    body.replaceChildren(row(node("div", "No confirmation from " + job.company + " yet. Fix what the form points out and tap its Submit button, or confirm it went through.")), again, laterButton(job), stopButton());
    for (let t = 0; t < 1200 && !stopped; t++) {  // keep watching while Sai fixes things
      if (succeeded()) return finished(job);
      await sleep(500);
    }
  }

  function laterButton(job) {
    const b = node("button", "Skip for now, next one", BTN + "background:#33322e;color:#ebeae4;margin-top:8px;width:100%");
    b.onclick = async () => { stopped = true; store.del(SENT_KEY); await api("POST", "/api/jobs/defer", {id: job.id}).catch(() => {}); goNext(); };
    return b;
  }

  async function finished(job) {
    store.del(SENT_KEY);
    try { await api("POST", "/api/jobs/status", {id: job.id, status: "applied"}); } catch (e) {}
    say("Submitted: " + job.company + ", " + job.title + ".");
    if (store.get(AUTO_KEY)) { await sleep(2500); if (!stopped) goNext(); }
  }

  async function goNext() {
    let n;
    try { n = await api("GET", "/api/jobs/next"); } catch (e) { return say(e.message); }
    if (!n.job) { store.del(AUTO_KEY); return say("All approved applications are done."); }
    if (!/^https:\/\//.test(n.job.apply_url)) return say("The next job has no application link.");
    say("Next: " + n.job.company + ", " + n.job.title + " (" + n.left + " left).");
    location.href = n.job.apply_url + "#agent-auto";
  }

  async function autoApply() {
    stopped = false;
    const res = await run();
    if (!res || !res.job) return;
    if (!res.approved) return body.append(row(node("div", "Not approved in the panel, so it won't be submitted. Review it in the Jobs tab of the panel.", "color:#e08a1e")));
    await sleep(1200);  // let the form settle after the last choices
    const missing = missingRequired();
    if (missing.length) {
      body.replaceChildren(row(node("div", "Fill these, then tap the form's Submit button:", "font-weight:600")),
        ...missing.slice(0, 8).map(q => row(node("div", q, "color:#e08a1e"))), laterButton(res.job), stopButton());
      store.set(SENT_KEY, JSON.stringify({id: res.job.id, company: res.job.company, title: res.job.title, at: Date.now()}));
      return watch(res.job);
    }
    const btn = submitButton();
    if (!btn) {
      body.replaceChildren(row(node("div", "Couldn't find the Submit button. Tap it yourself.")), laterButton(res.job), stopButton());
      store.set(SENT_KEY, JSON.stringify({id: res.job.id, company: res.job.company, title: res.job.title, at: Date.now()}));
      return watch(res.job);
    }
    for (let s = 3; s > 0 && !stopped; s--) {
      body.replaceChildren(row(node("div", "Submitting " + res.job.company + ": " + res.job.title + " in " + s + "...", "font-weight:600")), stopButton("Stop"));
      await sleep(1000);
    }
    if (stopped) return;
    store.set(SENT_KEY, JSON.stringify({id: res.job.id, company: res.job.company, title: res.job.title, at: Date.now()}));
    btn.click();
    body.replaceChildren(row(node("div", "Submitted, waiting for " + res.job.company + " to confirm...")), stopButton());
    watch(res.job);
  }

  // A page loaded after Submit (the confirmation) picks up the job it was waiting for.
  let sent = null;
  try { sent = JSON.parse(store.get(SENT_KEY) || "null"); } catch (e) {}
  if (sent && Date.now() - sent.at < 30 * 60 * 1000 && !document.querySelector("input[type=file]")) {
    ui(); minimize(false);
    say("Checking whether " + sent.company + " confirmed...");
    watch(sent);
  } else {
    // Application forms often render after load; wait for one before showing the button.
    let tries = 0;
    const timer = setInterval(() => {
      if (++tries > 40) return clearInterval(timer);
      if (document.querySelector("input[type=email], input[name*=email i], input[type=file]")) {
        clearInterval(timer);
        ui();
        if (store.get(AUTO_KEY)) { minimize(false); autoApply(); }
      }
    }, 500);
  }
  window.__agentFill = {collect, put, mark, attachResume, run, missingRequired, submitButton};  // for testing from the console
})();
"""
