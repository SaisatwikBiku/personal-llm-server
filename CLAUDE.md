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
| `/home/agentd/chats.json` | Chat conversations, 50 most recent kept (mode 600) |
| `/home/agentd/memory.md` | Facts about Sai, one per line, max 2,000 characters; added to every chat and panel task (mode 600) |
| `/home/agent/actions.jsonl` | Log for the terminal version only |
| `/opt/searxng/settings.yml` | SearXNG config (JSON output on, limiter off, contains a secret key) |
| `/home/agentd/jobs/` | Job search: `config.json`, `profile.json` (Sai's application profile, edited in the panel under You, Application profile), `resume.txt` (for scoring and drafts), `resume.pdf` (attached by the autofill script), `mail.json` (the agent inbox's address and app password), `inbox.json` (emails read so far), `jobs.json` (results). Mode 700, files 600, owned by `agentd` |

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
| 8B generation, 8 threads | 6.54 tokens/s (5.5 tokens/s streamed in chat) |
| 8B load / prompt processing uncached | about 5 s / 17 tokens/s |
| 8B first chat token, cached prompt | 0.2 s |
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
- The page, manifest and service worker are inline strings (`PAGE`, `MANIFEST`, `SERVICE_WORKER`). The page renders all model output with `textContent`, never `innerHTML`. Keep it that way. `PAGE` and `FILL_SCRIPT` are raw strings (`r"""`), so JavaScript escapes like `\n` and regex backslashes are written exactly as the browser should get them.
- Tabs: Chat (default), Tasks (the approval agent), Jobs, Memory. The approval card shows over any tab.
- Chat: `POST /api/chat` stores Sai's message, then streams the reply as plain text (`X-Chat-Id` header names the conversation). `write_reply()` runs the Ollama stream in a thread and `stream_reply()` checks `request.is_disconnected()` every 0.1 s; on Stop or a closed page it sets the stop flag and the thread closes the Ollama stream, which makes Ollama stop generating. The first version streamed from a plain generator and Ollama kept writing for minutes after Stop, so keep the thread. `chat_active` allows one reply at a time, and `panel_busy()` counts it so the job search waits. Chat has no tools and no network access, so it needs no approvals; don't give it tools without the approval flow. Models: "better" = `AGENT_CHAT_MODEL` (default `qwen3:8b`, thinking off), "faster" = `agent-4b`. History sent to the model is capped at `CHAT_HISTORY_CHARS` (12,000, about 3,000 tokens) because after a model switch the 8B rereads it at 17 tokens/s. The system prompt, memory and date come first and stay byte-stable within a day so Ollama's cache covers them.
- Attachments: the "+" button reads a file in the browser, `POST /api/extract` turns it into text (`extract_text()`: PDF text layer through `pypdf`, Word `.docx` from `word/document.xml`, otherwise text if there are no NUL bytes; 5 MB per file, 1,000,000 characters kept; nothing is stored) and the page shows its size and estimated reading time (characters / 4 / 17 tokens/s for the 8B, 35 for the 4B). Chat puts the files ahead of the message, 16,000 characters in total (`CHAT_ATTACH_CHARS`, with a cut note), and stores `text` and `files` separately so the page shows the message and file names. Tasks save each file to `uploads/<safe name>` in the workspace through `run_tool("write_file", ...)`, so as the `agent` user, and list the paths in the task text. `safe_filename()` keeps names inside `uploads/`. Images aren't supported (neither model has vision); scanned PDFs have no text layer and are refused.
- Memory: `memory.md`, edited under You, Memory (`GET/PUT /api/memory`) or by a chat message starting "remember that ..." / "remember: ...", which `REMEMBER_RE` catches and saves verbatim without calling the model. `memory_block()` is appended to the chat system prompt and to `core.SYSTEM_PROMPT` for panel tasks (not the terminal version, which runs as `agent` and can't read `/home/agentd`).

`toolrunner.py` reads `{"arg", "content"}` JSON on stdin, runs one tool from `agent.TOOLS`, prints the result.

`jobs.py` is the job search. `server.py` starts `jobs_loop()`, which calls `jobs.tick()` every minute; `tick` runs the search once a day after `run_at` and pushes a digest after `digest_at` (times in `config.json`, America/New_York). It does nothing until `/home/agentd/jobs` has `config.json` and `resume.txt`.

- Sources: Greenhouse (`boards-api.greenhouse.io`), Lever (`api.lever.co`) and Ashby (`api.ashbyhq.com`) public board APIs, Workday (`/wday/cxs/<tenant>/<site>/jobs`, searched with `workday_search` terms because boards hold thousands of jobs, then one detail request per new posting that passes the title and location filters) and SmartRecruiters (`/v1/companies/<id>/postings?country=us`, then one detail request per new passing posting), for the configured companies plus boards discovered through `site:` searches in SearXNG (60 most recent kept in `meta.discovered`). Workday and SmartRecruiters board names are case-sensitive and are not lowercased. Workday boards that answer 422 have a different site name; find the real one from a job URL (`<tenant>.wdN.myworkdayjobs.com/<site>/job/...`). A run over the 27 Workday and 8 SmartRecruiters boards in the example config takes a few minutes, with 0.2 s between requests. The autofill script doesn't run on Workday or SmartRecruiters forms (Workday needs an account per company and a multi-page form).
- Filters before the model: title must contain a configured role and no excluded word (`norm_title` handles "Staff+", "fullstack", "backend"); US or remote location (`location_ok`, non-US checked before two-letter state codes); no citizenship, clearance or ITAR requirement; smallest "N+ years ... experience" at most `max_years`. Postings saying they don't sponsor are flagged, not dropped, unless `exclude_no_sponsorship` is set.
- Scoring (`SCORE_VERSION` 2): `agent-4b` only extracts `{required_skills, level, summary}` from the posting (clipped to 2,500 characters; the resume is not in the prompt). `compare_skills()` checks each skill as a whole phrase against the resume after `skill_text()` normalization (aliases such as GCP, Node.js, C/C++; "X or Y" and "X/Y" count if either matches), and `fit_score()` computes 65% skills matched, 25% level, 10% years, minus 10 when the posting won't sponsor. About 20 s per posting. The first version asked the model for a 0-100 score directly: it gave 85 to 97 of 205 postings and invented gaps (said C++ and Kubernetes were missing), so don't go back to that. Bumping `SCORE_VERSION` puts every new job back in the queue for rescoring. Thresholds: good 60, strong 72.
- Drafts: `qwen3:8b` with thinking off (`draft_model`), about 33 s per answer plus about 100 s to swap models, then `restore_default_model()` loads `agent-4b` back. The 4B invented project details in cover letters; the 8B mostly stuck to the resume but still overstated once ("experience with observability systems"), so drafts are labeled for checking. Greenhouse (`?questions=true`) and Ashby (`ashby_questions()`, the `ApiJobPosting` GraphQL query its job pages use) publish each job's form questions; `answer_for()` fills facts from `profile.json`, marks legal/policy questions for Sai, and drafts open questions. Lever doesn't publish form questions, so those get the standard fields plus one "why this role" draft.
- Question traps found on real forms: "Visa and Mastercard" (a Stripe payments question) matched the sponsorship pattern and was answered Yes, so `\bvisa\b` skips card-network mentions; "Do you possess N years of experience in X?" is about skills and goes to Sai (`EXPERIENCE_RE`), never to `years_experience`. Drafts prepared before a profile field existed give way to the field in `refresh_answers()` ("current employer" had been drafted as a paragraph).
- Panel layout (redesigned 2026-09-26): `PAGE` is one page with five views, `#chat`, `#jobs`, `#inbox`, `#tasks` and `#you` (old `#log`, `#profile` and `#memory` links map to them). Above 860 px wide the navigation is a sidebar and Jobs is a list beside the open job; below it's a bottom tab bar and the open job is a full-screen layer with a back button. Colors are CSS variables on `:root`, dark ones under `prefers-color-scheme` and `data-theme` (the sidebar switch, kept in localStorage). The review card groups a job's questions: Needs you, Statements you agree to (one tick each), Drafts, then profile answers folded away, with Approve and a live count of required items left pinned at the bottom. Keep building it with DOM calls and `textContent`; nothing on the page uses `innerHTML`.
- Profile: `PROFILE_FORM` in `jobs.py` is the application profile, built on 2026-09-26 from the 450 distinct questions on the Greenhouse and Ashby forms of 189 matches. The panel's Profile tab (`GET/PUT /api/jobs/profile`, `profile_form()` and `save_profile()`) edits `profile.json`: form fields by key, plus `answers`, Sai's own answers to questions no field covers, matched by question text (`saved_answer()`, checked first). The tab lists the questions from prepared jobs that are still Sai's, most common first, leaving out ones an empty profile field would answer and "If yes, ..." follow-ups. `FACTS` maps question text to a field, first match wins, so specific patterns go before general ones ("without sponsorship" before "sponsor", "how many years" only, never "N years of X?"). A value that isn't one of a question's choices (`fits_options()`, matched like the userscript) leaves the question to Sai. "Have you worked at X before?" is answered from the `past_employers` and `past_interviews` lists only when the question names the company; parent companies (Capital One for Brex) stay Sai's. Demographic questions are filled only with an answer Sai chose in the Profile tab. `detail()` works out the non-draft answers again from the current profile (`refresh_answers()`), so profile changes reach jobs prepared earlier.
- Inbox: an address just for applications, connected in the Inbox tab (`POST /api/inbox` checks the login before saving `mail.json`, then sets the profile's email to it; the password is never returned). `inbox_loop()` in `server.py` calls `jobs.check_inbox()` every 5 minutes on its own thread, since `jobs_loop` is busy for hours during the nightly run. The first check reads 30 days back, later ones only UIDs above `last_uid` (reset when `UIDVALIDITY` changes), 60 at most per check, messages over 2 MB by their headers only. `classify_email()` is regex only, checked in this order: verification (subject), rejection, interview, confirmation, then verification codes in the body; rejection comes before interview because rejections mention interviews, and confirmations saying "we'll reach out to schedule an interview" must not count as interviews. `match_job()` needs the company named by the sender or subject (or, for names of 6+ letters, the start of the body), then prefers the most title words in common. A confirmation moves a job from new to applied; interview and rejection emails set `interview` and `rejected`. Interview requests and verification codes are pushed. Tested 2026-09-26 against a fake IMAP server only.
- State: `jobs.json` holds scored jobs, `seen` (filtered-out IDs, pruned after 90 days) and `meta` (last run stats, digest times). All writes go through `update_db()` under `db_lock`. A restart mid-run loses nothing already saved; unscored postings are picked up by the next run.
- Review and apply (Sai asked for it on 2026-09-26): the nightly run drafts up to `max_drafts_per_run` new matches at or above `good_score`, Greenhouse, Lever and Ashby first (`AUTO_ATS`, the forms the userscript handles). The Jobs tab's Review list shows the best `queue_size` (50) prepared ones and the digest says how many wait. Each card is an editable form; `POST /api/jobs/approve` saves the edits as `overrides` and the consents Sai ticked as `consents`, and refuses while a required question has no answer or a required consent is unticked. Every consent needs its own tick ("Agree to all" ticks them in one tap) because `LEGAL_RE` also catches factual attestations like "I confirm my graduation date will be Fall 2026 or Spring 2027"; never go back to agreeing to all of a job's consents on approval. "Apply to approved" opens `next_approved()`'s form with `#agent-auto`. There the userscript fills it, ticks only the consents in `consents` (`fill()` sends kind `consent` for those alone, and fills signature fields with `full_name`), checks required fields (`missingRequired()`), clicks Submit after a 3-second countdown with a Stop button, waits for a confirmation (`SUCCESS_RE` or a /thanks-like URL, carried across the page load in `sessionStorage`), marks the job applied and opens the next one. It stops and hands over when a required field is empty, the Submit button isn't found, a CAPTCHA challenge opens, or no confirmation comes; "Skip for now" defers the job (`deferred_at`, sorted last). CAPTCHAs are never worked around: they stay for Sai. Forms opened without `#agent-auto`, and jobs that aren't approved, are only filled, never submitted. Tested 2026-09-26 on a mock form in the local panel only, not yet on a real application.
- Autofill: `/jobs-fill.user.js` serves a userscript (`FILL_SCRIPT` in `server.py`, with the panel's own address filled in so the tailnet name stays out of the repo) that Sai installs in Userscripts (iPhone Safari) or Tampermonkey. On a Greenhouse, Lever or Ashby form he taps "Fill from agent": it collects each field's question text, `POST /api/jobs/fill` answers from the job's prepared answers or `answer_for()`, and it fills text fields, selects and radios, attaches `resume.pdf` from `GET /api/jobs/resume`, and outlines what's left. It only ticks checkboxes for consents Sai agreed to on an approved job (and opt-ins his profile says Yes to), never fills legal questions he didn't agree to, unknown questions, or demographic ones he left unanswered in the Profile tab, never overwrites a filled field, and never submits. react-select dropdowns (Greenhouse, Ashby) ignore scripted typing, so an inline page helper (`pageHelper()`) calls the component's `onInputChange` and `selectOption`; it only works where the site's CSP allows inline scripts (Greenhouse does), and otherwise the script falls back to typing. Tested 2026-09-25 against real Greenhouse (full fill, fake profile, not submitted), Lever and Ashby (field labels) forms. Greenhouse application links use the embeddable form (`job-boards.greenhouse.io/embed/job_app?for=<board>&token=<id>`, see `apply_url()`): about 40% of Greenhouse postings point at the company's own careers site, and even `job-boards.greenhouse.io/<board>/jobs/<id>` redirects there, where the userscript doesn't run. Greenhouse's school, degree and discipline dropdowns are search-as-you-type lists whose options load only when opened, so the page helper calls the list's own `loadOptions(search)` (the full answer, then its part before a comma) and selects from what comes back (tested 2026-09-26 on Stripe's form: "University at Albany, SUNY" picks "University at Albany - SUNY"). Answers are matched as written first and then through `ALIASES` ("United States" is "US" on Stripe's residence question, while its phone country list only matches "United States +1" as written). `missingRequired()` finds a react-select's choice in the `__control` element next to the input and skips react-select's hidden `requiredInput`; looking in the input's own container reported every filled dropdown as empty and stopped the first real run. The first-word match for dropdowns ("Albany, NY" to "Albany, New York, United States") applies only to location fields. Form markup changes will break parts of it; `window.__agentFill` exposes `collect()` for debugging from the console.

Environment variables are listed in the README's configuration table.

## Security rules that must hold

The model is assumed to be sometimes wrong and sometimes manipulated by text it reads. These properties are the point of the design; don't trade them for convenience without Sai explicitly deciding to.

1. Any tool with side effects or outbound network access needs Sai's approval. `web_search` and `fetch_url` stay approval-gated because queries and URLs can carry data out. The one exception is `jobs.py`, which Sai approved on 2026-09-25: it sends unapproved requests, but only to URLs its own code builds from `config.json` or from board names parsed out of search results: the Greenhouse, Lever and Ashby board APIs, Ashby's form query (`jobs.ashbyhq.com/api/non-user-graphql`, a POST whose body holds only the board name and posting ID from the job's own ID), `api.smartrecruiters.com`, Workday tenants at `<tenant>.wdN.myworkdayjobs.com` (board names must match `WORKDAY_RE`; Workday search is a POST whose body holds only the configured search terms), and the local SearXNG. The agent's inbox (added 2026-09-26 at Sai's request) is read over IMAP on port 993, on a server picked from the address's domain in `IMAP_HOSTS`, never from input; mailboxes are opened read-only and only fetched with `BODY.PEEK`. Model output must never become a URL, query or request body there, email text must never reach a model or have its links opened, search results are reduced to a board name matched against those hosts, and nothing from `profile.json` or `resume.txt` may be sent anywhere. Keep it that way. `jobs.py` itself never submits applications; only the autofill script does, in Sai's browser, for applications he approved one by one (see Review and apply).
2. Approval cards show the exact command, path and full content preview, not a model-written summary.
3. The panel runs as `agentd`; tools run as `agent`. The sudo rule is exactly `agentd ALL=(agent) NOPASSWD: /opt/agent/venv/bin/python /opt/agent/toolrunner.py *`. Don't widen it.
4. The `agent` user must not be able to reach the panel by any route or read `/home/agentd`. There are two routes: loopback port 8000, and `tailscale serve` on this machine's own tailnet address, which `tailscaled` forwards to port 8000 as root, so the port 8000 rule alone doesn't cover it. The firewall unit blocks both. Verify all three: `sudo -u agent curl -s -m 3 http://127.0.0.1:8000/api/state; echo $?` (expect 7), `sudo -u agent curl -s -m 5 -o /dev/null -w "%{http_code}\n" https://sai-ai.<tailnet>.ts.net/api/state` (expect 000), and `sudo -u agent ls /home/agentd` (expect permission denied). Also check `sudo ls -la /home/agentd`: every file must belong to `agentd`. On 2026-09-25 `vapid_private.pem`, `push_subscriptions.json` and `actions.jsonl` still belonged to `agent` from before the panel moved to `agentd`, which silently turned off all push notifications and the action log for about two hours; the panel now reports such problems at startup (journal and panel log). Before 2026-09-25 only the first rule existed and the second check returned 200.
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

- Files created while the panel ran as `agent` kept that owner after the move to `agentd`, so `agentd` couldn't read the push key or write the action log. Fixed with `sudo chown agentd:agentd` on the three files in `/home/agentd`. If notifications stop, check ownership there first.

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
