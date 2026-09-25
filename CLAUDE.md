# CLAUDE.md

Context for working on this repository. Read this before changing anything; the security section lists rules that must hold after every change.

## What this is

A personal AI agent running entirely on a spare laptop at Sai's home. A local 4B model (Ollama) proposes one action at a time as JSON, Sai approves or denies it from a web panel on his iPhone, the action runs as an unprivileged user, and the result goes back to the model until it finishes. No cloud model or API key is involved. The laptop runs headless with the lid closed and is reached over Tailscale.

Status as of 2026-09-25: all five build phases are done, and a job search (`jobs.py`, see below) was added the same day (base system and model, agent core, phone control panel, notifications and web search, hardening), and the server deploys itself from `main` on GitHub. The first automatic deploy (commit `1a9b055`) succeeded. A test deploy of a visible change (panel title "Agent v2") was the next planned step and may not have been done yet.

## The server

Hardware: Lenovo V15 G4 IRU. Intel Core i3-1315U (2 performance cores, 4 efficiency cores, 8 threads), 16 GB DDR4-3200 in dual channel (two 8 GB modules, likely one soldered and one socketed), 512 GB NVMe, Intel UHD graphics (not used for inference), Intel CNVi Wi-Fi, Realtek Ethernet (unused).

OS: Ubuntu Server 24.04.5 LTS, kernel 7.0 series, Secure Boot on, Windows removed. Partitions: `nvme0n1p1` is `/boot/efi`, `nvme0n1p2` is `/` (ext4, about 468 GB).

Hostname `sai-ai`. Reach it with `ssh sai@sai-ai` (key-based from Sai's Mac, over Tailscale MagicDNS). The local IP on home Wi-Fi is 192.168.1.204 but can change; prefer the hostname.

Network is Wi-Fi only, configured in `/etc/netplan/50-cloud-init.yaml`. cloud-init's network handling is disabled (`/etc/cloud/cloud.cfg.d/99-disable-network-config.cfg`) so it won't overwrite that file.

### Users

| User | Purpose |
|---|---|
| `sai` | Sai's login, has sudo (password required) |
| `agentd` | System user that runs the web panel. Holds approval state, the action log and the push key. Home `/home/agentd`, mode 750 |
| `agent` | Unprivileged user that every tool runs as. No sudo. Owns `/home/agent/workspace` |

### Paths

| Path | Contents |
|---|---|
| `/opt/agent` | Live code (`agent.py`, `server.py`, `toolrunner.py`, `requirements.txt`) and the venv at `/opt/agent/venv`. Owned by root. Only the deploy script writes here |
| `/opt/agent-src` | Root-owned Git clone of this repo, used by the deploy script |
| `/opt/agent-backups` | Timestamped copies of the live files before each deploy, 10 newest kept |
| `/var/lib/agent-update/` | `deployed` (last deployed commit) and `bad` (commit that failed and must not be retried) |
| `/usr/local/sbin/agent-update` | Installed copy of `deploy/agent-update.sh` |
| `/home/agent/workspace` | Default working directory for tools |
| `/home/agentd/actions.jsonl` | Action log for the web panel, rotated weekly, 12 kept |
| `/home/agentd/vapid_private.pem` | Web Push signing key (mode 600) |
| `/home/agentd/push_subscriptions.json` | Saved push subscriptions for Sai's phone |
| `/home/agent/actions.jsonl` | Log for the terminal version only |
| `/opt/searxng/settings.yml` | SearXNG config (JSON output on, limiter off, contains a secret key) |
| `/home/agentd/jobs/` | Job search: `config.json`, `profile.json` (Sai's contact details and form answers), `resume.txt`, `jobs.json` (results). Mode 700, files 600, owned by `agentd` |

### Services

| Unit | What it does |
|---|---|
| `ollama.service` | Model server on `127.0.0.1:11434`. Override at `/etc/systemd/system/ollama.service.d/override.conf`: `OLLAMA_KEEP_ALIVE=-1`, `OLLAMA_CONTEXT_LENGTH=8192`, `OLLAMA_MAX_LOADED_MODELS=1` |
| `agent-web.service` | The panel (uvicorn on `127.0.0.1:8000`) as `agentd`, with `AGENT_USE_TOOLRUNNER=1`. Drop-in `agent-web.service.d/push.conf` sets `AGENT_PUSH_SUB` to Sai's email |
| `agent-firewall.service` | iptables/ip6tables rules rejecting connections from uid `agent` to loopback port 8000 and to all Tailscale addresses (`100.64.0.0/10`, `fd7a:115c:a1e0::/48`) |
| `agent-update.timer` / `.service` | Pulls and deploys `main` every 5 minutes |
| `wifi-powersave-off.service` | Turns off Intel Wi-Fi power saving at boot (oneshot, shows inactive after running) |
| `docker` + container `searxng` | SearXNG on `127.0.0.1:8888`, `--restart unless-stopped` |
| `tailscaled` | Tailscale. `tailscale serve --bg 8000` publishes the panel over HTTPS to the tailnet only. Key expiry is disabled for this machine |

Other host settings: logind drop-in `/etc/systemd/logind.conf.d/lid.conf` ignores the lid switch; sleep targets are masked; `consoleblank=60` via `/etc/default/grub.d/99-consoleblank.cfg`; Lenovo battery conservation mode is on (`/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/conservation_mode` = 1); unattended-upgrades with automatic reboot at 04:00 (`/etc/apt/apt.conf.d/52unattended-reboot`); logrotate config at `/etc/logrotate.d/agent`; sudo rule at `/etc/sudoers.d/agentd`.

### Models

`agent-4b` is the default: Qwen3-4B-Instruct-2507, Q4_K_M, from `hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M`, built with `deploy/Modelfile.agent` to set `num_thread 8`. The source tag was removed after building; `agent-4b` still references the same weights.

`qwen3:8b` (Q4_K_M) is installed as a fallback. Call it with thinking off (`think=False` in the API, `--think=false` on the CLI).

Do not use the `qwen3:4b` tag. It resolved to a thinking model that reasons in plain text even with thinking disabled, producing thousands of tokens per answer.

## Performance budget

Measured on this machine with Ollama `--verbose`:

| Measurement | Value |
|---|---|
| 4B generation, 2 / 4 / 6 / 8 threads | 9.90 / 10.58 / 10.85 / 11.83 tokens/s |
| 8B generation, 8 threads | 6.54 tokens/s |
| Prompt processing (520 and 934 token prompts) | about 30 tokens/s |
| Cached prompt example | 974 of 1,011 tokens cached, 1.7 s |
| Typical agent step | 4 to 16 s |
| Step after long command output | 23 to 39 s |
| CPU temperature idle / generating / peak | 42 to 44°C / 67 to 77°C / 83°C |
| Model memory when loaded | 4B about 3.9 GB, 8B about 6.7 GB |

What follows from these numbers, and should stay true:

- Every token in the prompt costs about 33 ms when not cached. The system prompt is kept short and byte-for-byte stable so Ollama's prefix cache covers it. Any edit to `SYSTEM_PROMPT` makes the next call slower once, which is fine, but don't add per-task dynamic content (timestamps, random IDs) before the conversation, or the cache stops working.
- Tool output fed back to the model is capped: stdout 1,500 characters in `run_shell` (2,000 via `clip` elsewhere), stderr 400. Twenty-one "Permission denied" lines once pushed a step from about 23 s to 39 s.
- Only the last 10 messages (5 action/result pairs) are kept after the task message.
- Output tokens cost about 85 ms each. The model's "thought" field tends to run long; shorter is better.

## Code

`agent.py` is the core and is imported by the other two files.

- `SYSTEM_PROMPT`: role, tool list and rules. The owner name comes from `AGENT_OWNER` (default "Sai") via `.replace("Sai", OWNER)` at the end of the string.
- `SCHEMA`: JSON schema passed as Ollama's `format`. Fields `thought`, `tool` (enum), `arg`, optional `content`. The schema is flat on purpose; small models handle one string argument better than per-tool nested objects.
- Tools: `run_shell` (bash -lc in the workspace, 120 s timeout), `read_file` (first 20,000 characters), `write_file`, `list_dir`, `fetch_url` (300 KB cap, tags stripped), `web_search` (SearXNG JSON, top 5), plus `finish` handled by the loop.
- `AUTO_APPROVE = {"list_dir", "read_file"}`. Everything else needs a decision.
- `next_action()` calls `ollama.chat` with `format=SCHEMA`, temperature 0.3, `keep_alive=-1`, and falls back to a `finish` action if the JSON fails to parse.
- `run_task()` and `main()` are the terminal version, with `input()` approvals. It runs every tool in-process as whoever launched it (`sudo -u agent -H /opt/agent/venv/bin/python /opt/agent/agent.py`).

`server.py` is the FastAPI panel.

- Global `state` dict guarded by `lock`: status (`idle`, `thinking`, `waiting`, `running`), current task, event list (last 300), pending action, and a `seq` counter the page uses for incremental polling (`GET /api/state?since=n`).
- One task at a time, in a daemon thread. `wait_for_decision()` blocks on a `threading.Event` until `POST /api/decision` arrives with the matching pending ID, checking the stop flag each second.
- `run_tool()` calls `toolrunner.py` through `sudo -n -u agent -H` when `AGENT_USE_TOOLRUNNER=1`, otherwise in-process.
- Web Push: VAPID key generated on first start, subscriptions stored as JSON, `notify()` sends in a background thread and drops subscriptions that return 404 or 410. Pushes go out for "Approval needed", "Task done", step limit, and agent errors.
- `check_user()` enforces `AGENT_ALLOWED_LOGIN` against the `Tailscale-User-Login` header when set (not set currently).
- The page, manifest and service worker are inline strings (`PAGE`, `MANIFEST`, `SERVICE_WORKER`). The page renders all model output with `textContent`, never `innerHTML`. Keep it that way.

`toolrunner.py` reads `{"arg", "content"}` JSON on stdin, runs one tool from `agent.TOOLS`, prints the result.

`jobs.py` is the job search. `server.py` starts `jobs_loop()`, which calls `jobs.tick()` every minute; `tick` runs the search once a day after `run_at` and pushes a digest after `digest_at` (times in `config.json`, America/New_York). It does nothing until `/home/agentd/jobs` has `config.json` and `resume.txt`.

- Sources: Greenhouse (`boards-api.greenhouse.io`), Lever (`api.lever.co`) and Ashby (`api.ashbyhq.com`) public board APIs for the configured companies, plus boards discovered through `site:` searches in SearXNG (60 most recent kept in `meta.discovered`).
- Filters before the model: title must contain a configured role and no excluded word (`norm_title` handles "Staff+", "fullstack", "backend"); US or remote location (`location_ok`, non-US checked before two-letter state codes); no citizenship, clearance or ITAR requirement; smallest "N+ years ... experience" at most `max_years`. Postings saying they don't sponsor are flagged, not dropped, unless `exclude_no_sponsorship` is set.
- Scoring: `agent-4b`, resume in the system prompt (stable, so cached), posting clipped to 2,500 characters, JSON schema `{score, fit, gaps}`. About 27 s per posting on this machine; first call about 55 s. The fit and gaps sentences are sometimes wrong (it once said C++ and Kubernetes were missing), so the panel labels them as the model's view.
- Drafts: `qwen3:8b` with thinking off (`draft_model`), about 33 s per answer plus about 100 s to swap models, then `restore_default_model()` loads `agent-4b` back. The 4B invented project details in cover letters; the 8B mostly stuck to the resume but still overstated once ("experience with observability systems"), so drafts are labeled for checking. Greenhouse publishes each job's form questions (`?questions=true`); `answer_for()` fills facts from `profile.json`, marks legal/policy and demographic questions for Sai, and drafts open questions. Lever and Ashby don't publish form questions, so those get the standard fields plus one "why this role" draft.
- State: `jobs.json` holds scored jobs, `seen` (filtered-out IDs, pruned after 90 days) and `meta` (last run stats, digest times). All writes go through `update_db()` under `db_lock`. A restart mid-run loses nothing already saved; unscored postings are picked up by the next run.
- It never submits applications. Greenhouse and Lever application forms use reCAPTCHA and hCaptcha (checked 2026-09-25), and Sai decided the agent should not work around them.

Environment variables are listed in the README's configuration table.

## Security rules that must hold

The model is assumed to be sometimes wrong and sometimes manipulated by text it reads. These properties are the point of the design; don't trade them for convenience without Sai explicitly deciding to.

1. Any tool with side effects or outbound network access needs Sai's approval. `web_search` and `fetch_url` stay approval-gated because queries and URLs can carry data out. The one exception is `jobs.py`, which Sai approved on 2026-09-25: it sends unapproved GET requests, but only to URLs its own code builds (the three job board API hosts and the local SearXNG) from `config.json`. Model output must never become a URL, query or request body there, search results are reduced to a board name matched against the three hosts, and nothing from `profile.json` or `resume.txt` may be sent anywhere. Keep it that way, and never make it submit applications.
2. Approval cards show the exact command, path and full content preview, not a model-written summary.
3. The panel runs as `agentd`; tools run as `agent`. The sudo rule is exactly `agentd ALL=(agent) NOPASSWD: /opt/agent/venv/bin/python /opt/agent/toolrunner.py *`. Don't widen it.
4. The `agent` user must not be able to reach the panel by any route or read `/home/agentd`. There are two routes: loopback port 8000, and `tailscale serve` on this machine's own tailnet address, which `tailscaled` forwards to port 8000 as root, so the port 8000 rule alone doesn't cover it. The firewall unit blocks both. Verify all three: `sudo -u agent curl -s -m 3 http://127.0.0.1:8000/api/state; echo $?` (expect 7), `sudo -u agent curl -s -m 5 -o /dev/null -w "%{http_code}\n" https://sai-ai.<tailnet>.ts.net/api/state` (expect 000), and `sudo -u agent ls /home/agentd` (expect permission denied). Before 2026-09-25 only the first rule existed and the second check returned 200.
5. The panel binds to `127.0.0.1` only and is exposed solely through `tailscale serve` (tailnet only). Never bind to `0.0.0.0` and never use `tailscale funnel`.
6. Code in `/opt/agent` stays root-owned so neither service user can change what runs.
7. The deploy script applies only the five code files (`agent.py`, `server.py`, `toolrunner.py`, `jobs.py`, `requirements.txt`). Files under `deploy/` change root-level configuration and are applied by hand after review.
8. Nothing secret goes in the repo: no email address, tailnet domain, Wi-Fi details, keys or subscriptions. `.gitignore` covers `*.pem`, `push_subscriptions.json`, `*.jsonl`, `venv/`, `__pycache__/`.

## Deploying changes

Push to `main`. Within about 5 minutes `agent-update.timer` runs the deploy script, which:

1. Exits if `main` equals the deployed commit or the recorded bad commit.
2. Records the commit and exits without a restart if none of the five code files changed (README-only or `deploy/`-only commits), noting `deploy/` changes in the log.
3. Postpones if the panel reports a task in progress.
4. Resets `/opt/agent-src` to the commit, runs `py_compile` on the four Python files, backs up the live files, runs `pip install -r requirements.txt` if it differs from the live copy, installs the files as root, restarts `agent-web`.
5. Polls `/api/state` for up to 20 seconds. On failure it restores the backup, restarts, and writes the commit to `bad`.

Useful commands on the server: `sudo systemctl start agent-update` (deploy now), `journalctl -u agent-update -n 20 --no-pager` (history), `systemctl list-timers agent-update.timer`.

If `deploy/agent-update.sh` itself changes, reinstall it by hand: `sudo install -m 755 /opt/agent-src/deploy/agent-update.sh /usr/local/sbin/agent-update`.

The repo is public, so the server clones over HTTPS without credentials. If it's ever made private, the server needs a read-only deploy key.

## Working on the server from Claude Code

`ssh sai@sai-ai` works without a password from Sai's Mac, so read-only checks are fine to run directly (`systemctl status`, `journalctl`, `ollama ps`, `free -m`, `sensors`, reading files). `sudo` asks for Sai's password, so anything needing root has to be handed to Sai as a command to run, with a one-line explanation of what it changes. Don't edit files in `/opt/agent` on the server; change the repo and let the deploy script ship it.

Before pushing code: run `python3 -m py_compile agent.py server.py toolrunner.py jobs.py`. If a change adds a new file that `server.py` imports, push and install the updated `deploy/agent-update.sh` first; the installed script only ships the files in its own `FILES` list, so the panel would fail its health check and the commit would be marked bad. There is no test suite yet. During the build, changes were checked with a stub `ollama` package (a `chat()` that returns scripted JSON actions) and FastAPI's `TestClient`, driving a task through pending approval, decision and finish. Adding that as `tests/` is on the backlog.

## Hardware and OS quirks already solved

- Lenovo's airplane-mode state carried over from Windows and soft-blocked the Wi-Fi radio (`ideapad_wlan` in `/sys/class/rfkill`). The F8 key toggles it. If Wi-Fi drops, check `grep . /sys/class/rfkill/rfkill*/soft` first.
- Intel Wi-Fi power saving made SSH laggy; the oneshot service turns it off.
- Keeping models loaded forever let benchmarking leave both the 4B and 8B in memory (11 GB used). `OLLAMA_MAX_LOADED_MODELS=1` prevents it.
- In the Ollama CLI, any message starting with `/` is parsed as a command, so `/no_think` at the start of a prompt fails. Use `/set nothink` or `--think=false`.
- The installer was booted from a partition on the internal SSD with the `toram` kernel option, since no working USB stick was available. Not relevant day to day, but explains why there's no Windows or recovery partition.

## Known model behavior

The 4B model gets simple sysadmin tasks right in 2 to 4 steps but makes confident mistakes: it once suggested `sort -hr | tail -5` for the five largest directories (that returns the smallest) and once ranked 16K above 32K. The prompt now tells it to let commands do sorting and arithmetic and to flag permission errors, which fixed those cases. It sometimes opens with a pointless filtered command (`df -h | grep udev`); the "simplest command first" rule reduced that. Prompt changes should be tested against a few real tasks, since small wording changes shift behavior.

## Backlog

Roughly in priority order, as discussed with Sai:

1. Memory across tasks: a short notes file the agent reads at task start (preferences, common paths), kept small for the prompt budget.
2. Scheduled tasks from the panel (for example a daily disk and update check) with the result pushed to the phone. `jobs_loop()` in `server.py` is a working pattern for this.
3. An allowlist of harmless read-only commands (`df`, `free`, `uptime`, `ls`, `du`) that skip approval. Match exact commands, not prefixes, and reject anything with `;`, `|` to non-allowlisted programs, `>`, backticks or `$(`.
4. A 4B/8B switch in the panel for harder tasks.
5. A `tests/` folder using the stub-Ollama approach above.
6. Headscale to replace Tailscale's coordination server, if Sai wants zero third-party control plane.
7. Docker log size limits for the SearXNG container.
8. A LICENSE file (none chosen yet).

## Working with Sai

Sai is a full-stack developer (Python, Java, React/Next.js, cloud, Docker/Kubernetes) and prefers step-by-step guidance with paste-ready commands, each followed by what the output should look like, so he can check progress as he goes. When something fails, ask for the exact output before guessing.

For anything written for him (docs, commit messages, explanations): American English, direct and specific, prose over bullet lists unless the content is a real list, sentence-case headings, no em dashes, no bold for emphasis, no emojis, no promotional adjectives, no filler openers or closing summaries, and real numbers and names instead of general claims. Never invent facts, benchmarks or sources.
