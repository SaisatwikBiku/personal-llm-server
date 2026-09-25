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
except OSError:
    VAPID_PUBLIC = ""


def load_subs():
    try:
        return json.loads(SUBS_FILE.read_text())
    except (OSError, ValueError):
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
    body: d.body || "", tag: d.tag || "agent", data: {url: "/"}
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) { if ("focus" in c) return c.focus(); }
    return clients.openWindow("/");
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
</style></head><body>
<header>
  <strong>Agent</strong>
  <span id="status">connecting</span>
  <span><button class="ghost" id="alerts" style="display:none">Alerts</button> <button class="ghost" id="clear">Clear</button> <button class="ghost" id="stop">Stop</button></span>
</header>
<div id="log"></div>
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
setInterval(poll, 1500);
poll();
setupAlerts();
</script>
</body></html>
"""
