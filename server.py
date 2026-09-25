#!/usr/bin/env python3
"""Phase 3: web control panel for the local agent.

Reuses the prompt, model call and tools from agent.py (same folder).
Runs from /opt/agent as the agentd user (see deploy/agent-web.service); tools run
as the agent user through toolrunner.py when AGENT_USE_TOOLRUNNER=1.
"""
import base64
import json
import os
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
  c.append(top, el("div", "tool", [j.company, j.location, j.years != null ? j.years + "+ years" : ""].filter(Boolean).join(" \u00b7 ")));
  const f = el("div", "flags");
  if (j.no_sponsorship) f.append(el("span", "flag no", "No sponsorship"));
  else if (j.sponsors) f.append(el("span", "flag yes", "Sponsors visas"));
  if (j.prepared) f.append(el("span", "flag", "Answers ready"));
  if (j.status !== "new") f.append(el("span", "flag", j.status));
  if (f.childNodes.length) c.append(f);
  if (j.fit) c.append(el("div", "thought", "Model's view of the fit: " + j.fit));
  if (j.gaps) c.append(el("div", "thought", "Model's view of the gaps: " + j.gaps));
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
  if (j.url && j.url.startsWith("https://")) {
    const a = el("a", "applylink", "Open posting");
    a.href = j.url; a.target = "_blank"; a.rel = "noopener noreferrer";
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
