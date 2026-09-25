#!/usr/bin/env python3
"""Phase 3: web control panel for the local agent.

Reuses the prompt, model call and tools from agent.py (same folder).
Runs from /opt/agent as the agentd user (see deploy/agent-web.service); tools run
as the agent user through toolrunner.py when AGENT_USE_TOOLRUNNER=1.
"""
import base64
import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel
from pywebpush import WebPushException, webpush

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
    for path in (core.LOG_FILE, SUBS_FILE):
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
    system = {"role": "system", "content": core.SYSTEM_PROMPT}
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


# ---------- job search ----------

def panel_busy():
    return state["status"] != "idle"


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


# ---------- HTTP ----------

def check_user(request: Request):
    if ALLOWED_LOGIN and request.headers.get("Tailscale-User-Login", "") != ALLOWED_LOGIN:
        raise HTTPException(403, "Not allowed")


class TaskIn(BaseModel):
    task: str


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
    check_user(request)
    task = body.task.strip()
    if not task:
        raise HTTPException(400, "Empty task")
    with lock:
        if state["status"] != "idle":
            raise HTTPException(409, "A task is already running")
        state["task"] = task
        state["status"] = "thinking"
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
    if body.status not in ("new", "applied", "skipped"):
        raise HTTPException(400, "Status must be new, applied or skipped")
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
    "background_color": "#151513",
    "theme_color": "#151513",
})

SERVICE_WORKER = """
self.addEventListener("push", event => {
  let d = {};
  try { d = event.data.json(); } catch (e) { d = {title: "Agent", body: event.data ? event.data.text() : ""}; }
  event.waitUntil(self.registration.showNotification(d.title || "Agent", {
    body: d.body || "", tag: d.tag || "agent", data: {url: d.tag === "jobs" ? "/#jobs" : "/"}
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

PAGE = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Agent">
<link rel="manifest" href="manifest.json">
<title>Agent</title>
<style>
:root{--bg:#f5f4f0;--fg:#1d1c1a;--muted:#6d6b65;--card:#fff;--line:#dfddd6;--accent:#2f6f4f;--deny:#a3392b;--code:#eeede7}
@media (prefers-color-scheme: dark){:root{--bg:#151513;--fg:#ebeae4;--muted:#9b9992;--card:#1f1e1c;--line:#33322e;--accent:#57a37a;--deny:#cf5f4e;--code:#292825}}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);font:15px/1.45 -apple-system,system-ui,"Segoe UI",sans-serif;display:flex;flex-direction:column;padding-top:env(safe-area-inset-top,0px)}
header{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:10px 14px;border-bottom:1px solid var(--line)}
header strong{font-size:16px}
#status{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:55vw}
#log{flex:1;overflow-y:auto;padding:12px 14px;-webkit-overflow-scrolling:touch}
.ev{margin:0 0 10px}
.task{font-weight:600;margin-top:14px}
.thought{color:var(--muted);font-size:13px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px}
.tool{font-size:13px;color:var(--muted)}
pre{background:var(--code);padding:8px;border-radius:6px;white-space:pre-wrap;word-break:break-word;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;margin:6px 0 0;max-height:320px;overflow:auto}
.answer{border-left:3px solid var(--accent);padding:2px 0 2px 10px;white-space:pre-wrap}
.err{color:var(--deny);font-size:14px}
details summary{cursor:pointer;color:var(--muted);font-size:13px}
#pending{display:none;border-top:2px solid var(--accent);background:var(--card);padding:12px 14px}
.btns{display:flex;gap:8px;margin-top:10px}
button{font:inherit;border:0;border-radius:8px;padding:11px 14px;cursor:pointer}
.approve{background:var(--accent);color:#fff;flex:1}
.deny{background:var(--deny);color:#fff;flex:1}
.ghost{background:transparent;color:var(--muted);border:1px solid var(--line);padding:6px 10px;font-size:13px}
footer{display:flex;gap:8px;padding:10px 14px calc(10px + env(safe-area-inset-bottom,0px));border-top:1px solid var(--line)}
input{font:inherit;font-size:16px;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px;width:100%}
#send{background:var(--accent);color:#fff}
#jobs{display:none;flex:1;overflow-y:auto;padding:12px 14px;-webkit-overflow-scrolling:touch}
.jbar{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.jbar a.ghost{text-decoration:none;border-radius:8px;margin-left:auto}
.jbar select{font:inherit;font-size:13px;color:var(--fg);background:var(--card);border:1px solid var(--line);border-radius:8px;padding:5px 6px}
#jmeta{font-size:13px;color:var(--muted);margin-bottom:10px}
.job{margin-bottom:10px;cursor:pointer}
.jtop{display:flex;gap:8px;align-items:baseline}
.score{font-size:12px;font-weight:600;border-radius:6px;padding:1px 6px;background:var(--code);color:var(--muted);flex:none}
.score.strong{background:var(--accent);color:#fff}
.score.good{border:1px solid var(--accent);color:var(--accent)}
.flags{display:flex;flex-wrap:wrap;gap:6px;margin:4px 0}
.flag{font-size:12px;border:1px solid var(--line);border-radius:6px;padding:0 6px;color:var(--muted)}
.flag.no{border-color:var(--deny);color:var(--deny)}
.flag.yes{border-color:var(--accent);color:var(--accent)}
.jbody{cursor:auto}
.jbody:not(:empty){margin-top:10px;border-top:1px solid var(--line);padding-top:10px}
.ans{margin:0 0 10px}
.q{font-size:13px;font-weight:600}
.ans .ghost{margin-top:4px}
.applylink{display:inline-block;background:var(--accent);color:#fff;text-decoration:none;border-radius:8px;padding:9px 14px;margin-bottom:10px}
</style></head><body>
<header>
  <strong>Agent</strong>
  <span id="status">connecting</span>
  <span><button class="ghost" id="jobsbtn">Jobs</button> <button class="ghost" id="alerts" style="display:none">Alerts</button> <button class="ghost" id="clear">Clear</button> <button class="ghost" id="stop">Stop</button></span>
</header>
<div id="log"></div>
<div id="jobs">
  <div class="jbar">
    <select id="jfilter"><option value="new">New</option><option value="applied">Applied</option><option value="skipped">Skipped</option><option value="all">All</option></select>
    <button class="ghost" id="jrun">Run now</button>
    <a class="ghost" id="jsetup" href="jobs-fill.user.js">Autofill script</a>
  </div>
  <div id="jmeta"></div>
  <div id="jlist"></div>
</div>
<div id="pending">
  <strong>Approve this action?</strong>
  <div id="pbody"></div>
  <input id="reason" placeholder="Reason if denying (optional)" style="margin-top:8px">
  <div class="btns"><button class="deny" id="deny">Deny</button><button class="approve" id="approve">Approve</button></div>
</div>
<footer>
  <input id="task" placeholder="Give the agent a task" autocomplete="off" enterkeyhint="send">
  <button id="send">Send</button>
</footer>
<script>
const $ = id => document.getElementById(id);
let last = 0, pendingId = null;
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
function pre(text){ return el("pre", null, text); }
function render(ev){
  const box = el("div", "ev");
  if (ev.kind === "task") box.append(el("div", "task", "You: " + ev.text));
  else if (ev.kind === "thought") box.append(el("div", "thought", "Step " + ev.step + " (" + ev.stats + "): " + ev.text));
  else if (ev.kind === "action") {
    const c = el("div", "card");
    c.append(el("div", "tool", ev.tool + (ev.auto ? " (auto-approved, read-only)" : "")), pre(ev.arg));
    if (ev.content) c.append(pre(ev.content));
    box.append(c);
  }
  else if (ev.kind === "result") { const d = el("details"); d.append(el("summary", null, "Result"), pre(ev.text)); box.append(d); }
  else if (ev.kind === "rejected") box.append(el("div", "err", "Denied" + (ev.text ? ": " + ev.text : "")));
  else if (ev.kind === "answer") box.append(el("div", "answer", ev.text));
  else box.append(el("div", "err", ev.text));
  $("log").append(box);
}
async function poll(){
  try {
    const r = await fetch("api/state?since=" + last);
    if (!r.ok) { $("status").textContent = "error " + r.status; return; }
    const s = await r.json();
    if (s.seq < last) { last = 0; $("log").replaceChildren(); return poll(); }
    const log = $("log");
    const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
    for (const e of s.events) { render(e); last = Math.max(last, e.n); }
    $("status").textContent = s.status + (s.task ? " \u00b7 " + s.task : "");
    const p = s.pending;
    if (p && p.id !== pendingId) {
      pendingId = p.id;
      const b = $("pbody");
      b.replaceChildren(el("div", "tool", p.tool), pre(p.arg));
      if (p.content) b.append(pre(p.content));
      $("reason").value = "";
      $("pending").style.display = "block";
      log.scrollTop = log.scrollHeight;
    }
    if (!p) { pendingId = null; $("pending").style.display = "none"; }
    if (s.events.length && nearBottom) log.scrollTop = log.scrollHeight;
  } catch (e) { $("status").textContent = "offline"; }
}
async function post(path, body){
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
  if (!r.ok) { const t = await r.json().catch(() => ({})); alert(t.detail || ("Error " + r.status)); }
  poll();
}
$("send").onclick = () => { const t = $("task").value.trim(); if (!t) return; $("task").value = ""; post("api/task", {task: t}); };
$("task").addEventListener("keydown", e => { if (e.key === "Enter") $("send").click(); });
$("approve").onclick = () => { if (pendingId) { const id = pendingId; pendingId = "sent"; post("api/decision", {id: id, approve: true}); } };
$("deny").onclick = () => { if (pendingId) { const id = pendingId; pendingId = "sent"; post("api/decision", {id: id, approve: false, reason: $("reason").value}); } };
$("stop").onclick = () => post("api/stop");
$("clear").onclick = () => { post("api/clear").then(() => { last = 0; $("log").replaceChildren(); }); };
function b64ToBytes(s){
  const pad = "=".repeat((4 - s.length % 4) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
async function setupAlerts(){
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return;
  const reg = await navigator.serviceWorker.register("sw.js");
  const btn = $("alerts");
  btn.style.display = "inline-block";
  const existing = await reg.pushManager.getSubscription();
  if (existing) {
    btn.textContent = "Alerts on";
    await post("api/push/subscribe", existing.toJSON());  // refresh on the server
  }
  btn.onclick = async () => {
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { alert("Notifications were not allowed."); return; }
      const {key} = await (await fetch("api/push/key")).json();
      const sub = (await reg.pushManager.getSubscription()) ||
        await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64ToBytes(key)});
      await post("api/push/subscribe", sub.toJSON());
      await post("api/push/test");
      btn.textContent = "Alerts on";
    } catch (e) { alert("Could not turn on alerts: " + e); }
  };
}
const KIND = {fact: "from profile.json", draft: "draft by the local model, check every claim",
  legal: "read and answer yourself", you: "needs you", file: "attach", eeo: "voluntary"};
let view = "log", jobsSig = "", openJob = null, openBody = null;
function showView(v){
  view = v;
  $("log").style.display = v === "log" ? "" : "none";
  $("jobs").style.display = v === "jobs" ? "block" : "none";
  $("jobsbtn").textContent = v === "jobs" ? "Log" : "Jobs";
  history.replaceState(null, "", v === "jobs" ? "#jobs" : location.pathname);
  if (v === "jobs") { jobsSig = ""; loadJobs(); }
}
function jobsMeta(s){
  const p = s.progress, lr = s.last_run;
  if (p.running) return "Running: " + p.step + (p.total ? " (" + p.done + "/" + p.total + ")" : "");
  if (!lr) return "Not run yet.";
  const when = new Date(lr.finished || lr.started).toLocaleString([], {dateStyle: "medium", timeStyle: "short"});
  const n = x => (x || 0).toLocaleString();
  let t = "Last run " + when + ": " + n(lr.boards) + " companies, " + n(lr.new) + " new postings, " + n(lr.matched) + " passed the filters, " + n(lr.scored) + " scored";
  if (lr.backlog) t += ", " + n(lr.backlog) + " left for the next run";
  return t + ". " + s.discovered + " companies found through search.";
}
async function loadJobs(){
  let s;
  try { const r = await fetch("api/jobs"); if (!r.ok) return; s = await r.json(); } catch (e) { return; }
  $("jmeta").textContent = jobsMeta(s);
  $("jrun").disabled = s.progress.running || !s.configured;
  const f = $("jfilter").value;
  const list = s.jobs.filter(j => f === "all" || j.status === f);
  const sig = f + JSON.stringify(list);
  if (sig === jobsSig) return;
  jobsSig = sig;
  const box = $("jlist");
  box.replaceChildren();
  if (!s.configured) { box.append(el("div", "thought", "Job search isn't set up. Put config.json, profile.json and resume.txt in the jobs folder on the server.")); return; }
  if (!list.length) box.append(el("div", "thought", "Nothing here yet."));
  for (const j of list) box.append(jobCard(j, s));
}
function jobCard(j, s){
  const c = el("div", "card job");
  const top = el("div", "jtop");
  top.append(el("span", "score " + (j.score >= s.strong ? "strong" : j.score >= s.good ? "good" : ""), String(j.score)), el("strong", null, j.title));
  c.append(top, el("div", "tool", [j.company, j.location, j.level, j.years != null ? j.years + "+ years" : ""].filter(Boolean).join(" \u00b7 ")));
  const f = el("div", "flags");
  if (j.no_sponsorship) f.append(el("span", "flag no", "No sponsorship"));
  else if (j.sponsors) f.append(el("span", "flag yes", "Sponsors visas"));
  if (j.prepared) f.append(el("span", "flag", "Answers ready"));
  if (j.status !== "new") f.append(el("span", "flag", j.status));
  if (f.childNodes.length) c.append(f);
  if (j.summary) c.append(el("div", "thought", j.summary));
  const has = j.has_skills || [], miss = j.missing_skills || [];
  if (has.length + miss.length) {
    c.append(el("div", "thought", "Has " + has.length + " of " + (has.length + miss.length) + " required skills" + (has.length ? ": " + has.join(", ") : "")));
    if (miss.length) c.append(el("div", "thought", "Missing: " + miss.join(", ")));
  }
  const body = el("div", "jbody");
  c.append(body);
  c.onclick = e => { if (!e.target.closest(".jbody")) toggleJob(j.id, body); };
  if (openJob === j.id) toggleJob(j.id, body, true);
  return c;
}
async function toggleJob(id, body, reopen){
  if (!reopen && openJob === id) { openJob = null; body.replaceChildren(); return; }
  if (openBody && openBody !== body) openBody.replaceChildren();
  openJob = id; openBody = body;
  let j;
  try { const r = await fetch("api/jobs/detail?id=" + encodeURIComponent(id)); if (!r.ok) return; j = await r.json(); } catch (e) { return; }
  body.replaceChildren();
  if (j.apply_url && j.apply_url.startsWith("https://")) {
    const a = el("a", "applylink", "Open application");
    a.href = j.apply_url; a.target = "_blank"; a.rel = "noopener noreferrer";
    body.append(a);
  }
  if (j.answers) {
    for (const a of j.answers) {
      const row = el("div", "ans");
      row.append(el("div", "q", (a.required ? "* " : "") + a.q), el("div", "tool", KIND[a.kind] || a.kind), pre(a.a || ""));
      if (a.options && a.options.length) row.append(el("div", "thought", "Options: " + a.options.join(", ")));
      if (a.kind === "fact" || a.kind === "draft") {
        const b = el("button", "ghost", "Copy");
        b.onclick = () => navigator.clipboard.writeText(a.a).then(() => { b.textContent = "Copied"; });
        row.append(b);
      }
      body.append(row);
    }
    if (j.note) body.append(el("div", "thought", j.note));
  } else {
    const b = el("button", "ghost", "Prepare answers");
    b.onclick = () => { b.disabled = true; b.textContent = "Preparing, a few minutes"; post("api/jobs/prepare", {id: id}); };
    body.append(b);
  }
  const d = el("details");
  d.append(el("summary", null, "Posting text"), pre(j.description || ""));
  body.append(d);
  const btns = el("div", "btns");
  for (const [label, st] of [["Applied", "applied"], ["Skip", "skipped"], ["Back to new", "new"]]) {
    if (st === j.status) continue;
    const b = el("button", st === "applied" ? "approve" : "ghost", label);
    b.onclick = () => post("api/jobs/status", {id: id, status: st}).then(() => { openJob = null; jobsSig = ""; loadJobs(); });
    btns.append(b);
  }
  body.append(btns);
}
$("jobsbtn").onclick = () => showView(view === "jobs" ? "log" : "jobs");
$("jfilter").onchange = () => { openJob = null; jobsSig = ""; loadJobs(); };
$("jrun").onclick = () => post("api/jobs/run").then(() => setTimeout(loadJobs, 500));
if ("serviceWorker" in navigator) navigator.serviceWorker.addEventListener("message", e => { if (e.data && e.data.view) showView(e.data.view); });
setInterval(() => { if (view === "jobs") loadJobs(); }, 5000);
window.addEventListener("hashchange", () => showView(location.hash === "#jobs" ? "jobs" : "log"));
if (location.hash === "#jobs") showView("jobs");
setInterval(poll, 1500);
poll();
setupAlerts();
</script>
</body></html>
"""

# Installed in Sai's browser (Userscripts on iPhone Safari, Tampermonkey or Userscripts on
# the Mac). It fills the form it's on and never submits it: the CAPTCHA, the consents
# and the Submit button stay with Sai. Page text is only ever set with textContent.
FILL_SCRIPT = r"""// ==UserScript==
// @name         Agent application autofill
// @namespace    local-agent
// @version      2
// @description  Fills job application forms with answers from the agent panel. You review and submit.
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
  const gmx = (typeof GM !== "undefined" && GM.xmlHttpRequest) ? GM.xmlHttpRequest.bind(GM)
            : (typeof GM_xmlhttpRequest !== "undefined" ? GM_xmlhttpRequest : null);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const clean = t => (t || "").replace(/[*✱]/g, "").replace(/\s+/g, " ").trim();
  const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();

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
      if (el.disabled || ["hidden", "submit", "button", "image", "reset", "password", "search", "checkbox"].includes(type)) continue;
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
    const v = norm(value);
    if (!v) return -1;
    let i = options.findIndex(o => norm(o) === v);
    if (i < 0) i = options.findIndex(o => { const n = norm(o); return n && (v.startsWith(n + " ") || n.startsWith(v + " ")); });
    return i;
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
    const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
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
      if (inst.props.onInputChange) inst.props.onInputChange(value.split(",")[0], {action: "input-change", prevInputValue: ""});
      const v = norm(value), first = v.split(" ")[0];
      for (let t = 0; t < 12; t++) {  // options can load from the network
        await new Promise(r => setTimeout(r, 250));
        inst = comp(el);
        const opts = inst.props.options || [];
        const labels = opts.map(o => norm(String((inst.props.getOptionLabel ? inst.props.getOptionLabel(o) : o.label) || "")));
        let i = labels.findIndex(l => l === v);
        if (i < 0) i = labels.findIndex(l => l && (v.startsWith(l + " ") || l.startsWith(v + " ")));
        if (i < 0 && loose) i = labels.findIndex(l => first && l.startsWith(first));
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

  async function put(el, f, v) {
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
        if (await put(el, f, a.value)) state = a.kind === "draft" ? "review" : "filled";
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
    if (res.job) {
      const done = node("button", "Mark applied in the panel", BTN + "background:#2f6fd6;color:#fff;margin-top:10px;width:100%");
      done.onclick = () => api("POST", "/api/jobs/status", {id: res.job.id, status: "applied"})
        .then(() => { done.textContent = "Marked applied"; done.disabled = true; }, e => { done.textContent = e.message; });
      out.push(done);
    }
    body.replaceChildren(...out);
  }

  // Application forms often render after load; wait for one before showing the button.
  let tries = 0;
  const timer = setInterval(() => {
    if (++tries > 40) return clearInterval(timer);
    if (document.querySelector("input[type=email], input[name*=email i], input[type=file]")) {
      clearInterval(timer);
      ui();
    }
  }, 500);
  window.__agentFill = {collect, put, mark, attachResume, run};  // for testing from the console
})();
"""
