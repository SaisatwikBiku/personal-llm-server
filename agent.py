#!/usr/bin/env python3
"""Local approval-gated agent, phase 2 (terminal).

Run as the unprivileged agent user:
    sudo -u agent -H /opt/agent/venv/bin/python /opt/agent/agent.py
"""
import html
import json
import os
import re
import subprocess
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import ollama

MODEL = os.environ.get("AGENT_MODEL", "agent-4b")
WORKSPACE = Path(os.environ.get("AGENT_WORKSPACE", "/home/agent/workspace"))
LOG_FILE = Path(os.environ.get("AGENT_LOG", "/home/agent/actions.jsonl"))
MAX_STEPS = 15            # hard stop per task
MAX_RESULT_CHARS = 2000   # tool output fed back to the model
KEEP_RECENT = 10          # messages kept after the task (5 action/result pairs)
CMD_TIMEOUT = 120         # seconds per shell command

AUTO_APPROVE = {"list_dir", "read_file"}

SYSTEM_PROMPT = """You are an assistant that operates a Linux server (Ubuntu 24.04) for Sai. You act through tools, one tool per reply. Sai approves or rejects each action before it runs.

Tools:
- run_shell: arg = a bash command. Runs as user "agent" (no sudo) in /home/agent/workspace. Use for anything not covered below.
- read_file: arg = file path. Returns the start of the file.
- write_file: arg = file path, content = full file text. Overwrites the file.
- list_dir: arg = directory path.
- fetch_url: arg = an http(s) URL. Returns the page text.
- finish: arg = your final answer to Sai. Use when the task is done, impossible, or you need to ask Sai something.

Rules:
- Reply with JSON only: {"thought": "...", "tool": "...", "arg": "...", "content": "..."}.
- "thought" is one short sentence about why you are taking this step.
- Take one small step at a time and check each result before the next step.
- Use read-only commands to learn about the system before changing anything.
- You have no sudo. If a task needs root, finish and tell Sai the exact command to run.
- If an action is rejected, read Sai's reason and adjust.
- Never invent results. Report only what tool output showed.
- Keep final answers short and specific."""

SCHEMA = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "tool": {
            "type": "string",
            "enum": ["run_shell", "read_file", "write_file", "list_dir", "fetch_url", "finish"],
        },
        "arg": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["thought", "tool", "arg"],
}


# ---------- helpers ----------

def clip(text, limit=MAX_RESULT_CHARS):
    text = text.strip()
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n...[{len(text) - limit} chars cut]...\n{text[-half:]}"


def resolve(path):
    p = Path(path or ".").expanduser()
    return p if p.is_absolute() else WORKSPACE / p


def log(event):
    event["time"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    try:
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(event) + "\n")
    except OSError:
        pass


def indent(text, prefix="    ", max_lines=20):
    lines = text.splitlines()
    shown = lines[:max_lines]
    if len(lines) > max_lines:
        shown.append(f"...[{len(lines) - max_lines} more lines]")
    return "\n".join(prefix + line for line in shown)


# ---------- tools ----------

def run_shell(cmd, _content):
    try:
        r = subprocess.run(
            ["bash", "-lc", cmd], cwd=WORKSPACE,
            capture_output=True, text=True, timeout=CMD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"Timed out after {CMD_TIMEOUT}s."
    out = r.stdout + (("\n" + r.stderr) if r.stderr else "")
    return f"exit code {r.returncode}\n{clip(out) or '(no output)'}"


def read_file(path, _content):
    try:
        with resolve(path).open(errors="replace") as f:
            return clip(f.read(20_000)) or "(empty file)"
    except Exception as e:
        return f"Error: {e}"


def write_file(path, content):
    p = resolve(path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content or "")
        return f"Wrote {len(content or '')} chars to {p}"
    except Exception as e:
        return f"Error: {e}"


def list_dir(path, _content):
    p = resolve(path)
    try:
        entries = sorted(p.iterdir(), key=lambda e: (not e.is_dir(), e.name))
        lines = [
            f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.lstat().st_size} bytes)"
            for e in entries
        ]
        return clip("\n".join(lines)) or "(empty)"
    except Exception as e:
        return f"Error: {e}"


def fetch_url(url, _content):
    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (local-agent)"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            charset = resp.headers.get_content_charset() or "utf-8"
            raw = resp.read(300_000).decode(charset, errors="replace")
    except Exception as e:
        return f"Error: {e}"
    text = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", html.unescape(text))
    return clip(text) or "(no text found)"


TOOLS = {
    "run_shell": run_shell,
    "read_file": read_file,
    "write_file": write_file,
    "list_dir": list_dir,
    "fetch_url": fetch_url,
}


# ---------- approval ----------

def describe(action):
    lines = [f"  tool: {action.get('tool')}", f"  arg:  {action.get('arg', '')}"]
    if action.get("tool") == "write_file":
        c = action.get("content") or ""
        preview = c if len(c) <= 800 else c[:800] + f"\n...[{len(c) - 800} more chars]"
        lines.append("  content:\n" + indent(preview, max_lines=40))
    return "\n".join(lines)


def ask_approval(action):
    print(describe(action))
    while True:
        ans = input("  Run it? [y]es / [n]o / [q]uit task: ").strip().lower()
        if ans in ("y", "yes"):
            return True, ""
        if ans in ("q", "quit"):
            raise KeyboardInterrupt
        if ans in ("n", "no"):
            return False, input("  Reason (optional, helps the model): ").strip()


# ---------- model ----------

def next_action(messages):
    t0 = time.time()
    resp = ollama.chat(
        model=MODEL, messages=messages, format=SCHEMA,
        options={"temperature": 0.3}, keep_alive=-1,
    )
    elapsed = time.time() - t0
    raw = resp.message.content or ""
    try:
        action = json.loads(raw)
    except json.JSONDecodeError:
        action = {"thought": "", "tool": "finish",
                  "arg": f"(model returned invalid JSON) {raw[:300]}"}
    stats = (f"{elapsed:.1f}s, prompt {getattr(resp, 'prompt_eval_count', 0) or 0} tok, "
             f"output {getattr(resp, 'eval_count', 0) or 0} tok")
    return action, raw, stats


# ---------- loop ----------

def run_task(task):
    system = {"role": "system", "content": SYSTEM_PROMPT}
    task_msg = {"role": "user", "content": f"Task: {task}"}
    history = []
    log({"event": "task", "task": task})

    for step in range(1, MAX_STEPS + 1):
        print(f"\n[step {step}] thinking...", flush=True)
        action, raw, stats = next_action([system, task_msg] + history[-KEEP_RECENT:])
        print(f"  ({stats})")
        if action.get("thought"):
            print(f"  thought: {action['thought']}")
        history.append({"role": "assistant", "content": raw})

        tool = action.get("tool")
        if tool == "finish":
            answer = action.get("arg", "")
            print(f"\nAGENT: {answer}")
            log({"event": "finish", "answer": answer})
            return

        if tool not in TOOLS:
            result = f"Unknown tool '{tool}'."
            approved = False
        elif tool in AUTO_APPROVE:
            print(describe(action) + "\n  (auto-approved: read-only)")
            approved, reason = True, ""
        else:
            approved, reason = ask_approval(action)

        if tool in TOOLS:
            if approved:
                result = TOOLS[tool](action.get("arg", ""), action.get("content", ""))
                print("  result:\n" + indent(result))
            else:
                result = "Sai rejected this action." + (f" Reason: {reason}" if reason else "")
                print("  (rejected)")

        log({"event": "action", "tool": tool, "arg": action.get("arg", ""),
             "approved": approved, "result": result[:500]})
        history.append({"role": "user", "content": f"Result of {tool}:\n{result}"})

    print(f"\nStopped after {MAX_STEPS} steps without finishing.")
    log({"event": "step_limit"})


def main():
    WORKSPACE.mkdir(parents=True, exist_ok=True)
    print(f"Local agent ready. Model: {MODEL}. Workspace: {WORKSPACE}.")
    print("Type a task. 'exit' or Ctrl+C at this prompt quits; Ctrl+C during a task stops it.")
    while True:
        try:
            task = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not task:
            continue
        if task.lower() in ("exit", "quit"):
            break
        try:
            run_task(task)
        except KeyboardInterrupt:
            print("\n(task stopped)")
            log({"event": "stopped"})


if __name__ == "__main__":
    main()
