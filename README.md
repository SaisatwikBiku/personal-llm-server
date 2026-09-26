# Local approval-gated agent

A personal AI agent that runs entirely on a spare laptop: a 4B language model served by Ollama, a small Python loop that lets the model act on the operating system through a handful of tools, and a web control panel on my phone where I approve or deny every action before it runs. It needs no cloud model or API key. The laptop stays on around the clock with the lid closed, and I reach it from anywhere over Tailscale.

The model never gets a free hand. For each task it proposes exactly one action as JSON, the action waits for my approval, the result goes back to the model, and the loop repeats until the model reports an answer. Reading files and listing directories run without asking, since they can't change anything. Shell commands, file writes, web fetches and web searches all stop for a decision.

## Hardware

The host is a Lenovo V15 G4 IRU that was sitting unused:

| Part | Spec |
|---|---|
| CPU | Intel Core i3-1315U (2 performance cores, 4 efficiency cores, 8 threads) |
| RAM | 16 GB DDR4-3200, dual channel (two 8 GB modules) |
| GPU | Intel UHD Graphics, unused for inference |
| Storage | 512 GB NVMe |
| OS | Ubuntu Server 24.04.5 LTS, headless |

There is no usable GPU, so everything runs on the CPU. Token generation on a CPU is limited by memory bandwidth, which is why confirming dual-channel RAM was the first hardware check.

## Benchmarks

All numbers are from Ollama's `--verbose` output on this laptop, using the same prompt (a Linux disk-usage question) with a fresh context each run.

Generation speed for Qwen3-4B-Instruct-2507 (Q4_K_M) by thread count:

| Threads | Tokens per second |
|---|---|
| 2 | 9.90 |
| 4 | 10.58 |
| 6 | 10.85 |
| 8 | 11.83 |

Qwen3 8B (Q4_K_M, thinking off) at 8 threads generated 6.54 tokens per second. The 4B runs almost twice as fast, so it is the default, and the approval step catches its mistakes.

Prompt processing ran at about 30 tokens per second on 520- and 934-token prompts. That makes prompt size the main cost of an agent step: a cold 1,000-token prompt takes around 30 seconds before the first output token. Ollama's prompt cache removes most of it. In one run, 974 of a 1,011-token prompt were cached, and the step's prompt processing took 1.7 seconds.

In real use, most agent steps took 4 to 16 seconds. Steps that fed long command output back to the model took 23 to 39 seconds, which led to the output limits described below.

CPU temperature idled at 42 to 44°C, sat at 67 to 77°C while generating, and peaked at 83°C during the benchmarks. The chip throttles near 100°C.

## How it works

`agent.py` holds the core: the system prompt, the tools, the model call and a terminal version of the loop. The model is called with Ollama's structured output option and a JSON schema, so every reply is a valid object with a one-sentence `thought`, a `tool` name and an `arg`. The tools are:

- `run_shell` runs a bash command in the workspace directory, with a 120-second timeout
- `read_file` and `list_dir` read from the filesystem and are auto-approved
- `write_file` overwrites a file, and the approval prompt shows a preview of the content
- `fetch_url` downloads a page and strips it to text
- `web_search` queries a local SearXNG instance and returns the top five results
- `finish` ends the task with an answer, or with a question back to me

Several design choices come straight from the benchmarks. The system prompt is short and never changes, so it stays in Ollama's cache. Tool output is cut to 2,000 characters before the model sees it, and stderr gets a separate 400-character budget, because a few dozen "Permission denied" lines were once enough to double a step's time. Only the last five action and result pairs stay in context. Each task is capped at 15 steps.

`server.py` is a FastAPI app that serves a single-page control panel with four tabs. Chat is a plain conversation with the 8B model (or the 4B for speed): replies stream in as they're written, conversations are saved, and there are no tools, so nothing needs approval. Memory is a short list of facts about me that goes at the start of every chat and task; I edit it in the Memory tab or say "remember that ..." in a chat. Tasks runs the tool loop above in a background thread. Both Chat and Tasks take attached files (text of any kind, PDFs with a text layer, Word .docx): in Chat the file's text goes into the message, up to 16,000 characters, and the page shows how long the model will take to read it; in Tasks the file is saved into the agent's workspace so commands can work on it. When the model proposes an action that needs approval, the loop blocks until I tap Approve or Deny on the phone. A denial can carry a reason, which goes back to the model as the tool result, and that is how I steer it. The panel polls the server every 1.5 seconds and is installed on the phone as a home-screen web app. It sends Web Push notifications when an action needs approval and when a task finishes or fails. The push payload is encrypted end to end, so Apple's push service relays it without being able to read it.

`toolrunner.py` is a short helper that executes one tool as the unprivileged `agent` user. The server calls it through sudo, which is the core of the security model.

`jobs.py` is a job search that runs overnight inside the panel process. It reads the public job board APIs of Greenhouse, Lever and Ashby, plus Workday and SmartRecruiters, which most banks, insurers, hospitals and universities use, for a list of companies, finds more companies through site-restricted SearXNG searches, and drops postings whose title, location, required years or citizenship and clearance requirements don't fit. For each remaining posting, the 4B model lists the required skills and the seniority level, in about 20 seconds, and code scores the fit: how many of those skills appear as phrases in my resume, whether the level is new grad to mid, the years asked for, and whether the company sponsors visas. Asked for a fit score directly, the 4B gave nearly every software role 85. For the strongest matches, the 8B model drafts answers to the form's open questions (the 4B invented project details in testing; the 8B stuck to the resume). Factual answers such as name, contact details and work authorization come from a profile file, never from the model. A push notification each morning lists the new matches, and a Jobs view in the panel shows each one with its score, the skills it matched and missed, the prepared answers, and a link to the application. A userscript fills the application form itself (see below). It never submits an application: the Greenhouse and Lever application forms are behind reCAPTCHA and hCaptcha, and forms include legal agreements that I should read myself.

The panel listens only on `127.0.0.1:8000`. `tailscale serve` publishes it over HTTPS to devices signed into my tailnet and nowhere else.

## Security model

The model can be wrong, and it can be manipulated by text it reads from web pages or files. The design assumes both.

Every action with side effects needs a human decision. The approval card shows the exact command, file path and content, so what I approve is exactly what runs.

The server and the tools run as different users. The server runs as `agentd`, which holds the approval state, the action log and the push signing key in a home directory the agent can't read. Tools run as `agent`, which has no sudo rights and owns only its workspace. The sudoers rule lets `agentd` run one program as one user and nothing else:

```
agentd ALL=(agent) NOPASSWD: /opt/agent/venv/bin/python /opt/agent/toolrunner.py *
```

An approved command can't approve itself. Firewall rules matched on user ID reject any connection from the `agent` user to port 8000 on loopback and to every Tailscale address, so a command can't send a fake approval to the panel, either directly or through `tailscale serve` on the machine's own tailnet address. Tailscale's own connection to the panel runs as root and is unaffected.

The code in `/opt/agent` belongs to my own account, so neither service user can modify it.

Web search needs approval too, even though it only reads. A search query leaves the machine, and a model that has just read a hostile page could be talked into putting private data in one.

Every action, approved or not, is appended to `/home/agentd/actions.jsonl`, which logrotate keeps for 12 weeks.

The job search is the one part that reaches the network without an approval. It is plain code, not the tool loop: it only sends requests to URLs it builds itself, to the job board APIs and the local SearXNG, from the company names and search phrases in its config. From search results it keeps only the company name, and only when the result's URL matches one of the three job board hosts. Model output never becomes a URL or request data, and my resume and profile never leave the machine. They live in `/home/agentd/jobs`, which the `agent` user can't read.

## Repository layout

```
agent.py              prompt, tools, model call, terminal loop
server.py             web control panel, approval flow, push notifications
toolrunner.py         runs one tool as the agent user
jobs.py               nightly job search, scoring and application drafts
requirements.txt
deploy/
  agent-web.service           systemd unit for the panel (runs as agentd)
  agent-firewall.service      blocks the agent user from the panel and the tailnet
  sudoers-agentd              the single sudo rule
  logrotate-agent             log rotation for the action log
  ollama-override.conf        Ollama settings (keep model loaded, 8k context, one model)
  Modelfile.agent             builds the agent-4b model with 8 threads
  wifi-powersave-off.service  keeps the Intel Wi-Fi card responsive
  agent-update.sh             pulls main from GitHub and deploys it
  agent-update.service        runs the deploy script
  agent-update.timer          triggers it every 5 minutes
  jobs-config.example.json    job search settings: roles, companies, filters, schedule
  jobs-profile.example.json   fields for application forms (the real file stays on the server)
```

## Setup

These steps assume Ubuntu Server 24.04 with SSH access and Tailscale already running on the machine and on your phone.

1. Install Ollama, apply the service override and build the model:

   ```bash
   curl -fsSL https://ollama.com/install.sh | sh
   sudo mkdir -p /etc/systemd/system/ollama.service.d
   sudo cp deploy/ollama-override.conf /etc/systemd/system/ollama.service.d/override.conf
   sudo systemctl daemon-reload && sudo systemctl restart ollama
   ollama pull hf.co/unsloth/Qwen3-4B-Instruct-2507-GGUF:Q4_K_M
   ollama create agent-4b -f deploy/Modelfile.agent
   ```

   Set `num_thread` in the Modelfile to whatever benchmarks best on your CPU.

2. Create the two service users and the workspace:

   ```bash
   sudo adduser --disabled-password --gecos "" agent
   sudo adduser --system --group --home /home/agentd agentd
   sudo chmod 750 /home/agentd
   sudo -u agent mkdir -p /home/agent/workspace
   ```

3. Install the code and its dependencies:

   ```bash
   sudo mkdir -p /opt/agent && sudo chown "$USER":"$USER" /opt/agent
   cp agent.py server.py toolrunner.py /opt/agent/
   python3 -m venv /opt/agent/venv
   /opt/agent/venv/bin/pip install -r requirements.txt
   ```

4. Add the sudo rule and check the syntax before closing the terminal:

   ```bash
   sudo cp deploy/sudoers-agentd /etc/sudoers.d/agentd
   sudo chmod 440 /etc/sudoers.d/agentd
   sudo visudo -c
   ```

5. Run SearXNG for web search, bound to localhost with JSON output turned on:

   ```bash
   sudo apt install -y docker.io
   sudo mkdir -p /opt/searxng
   SECRET=$(openssl rand -hex 32)
   printf 'use_default_settings: true\nserver:\n  secret_key: "%s"\n  limiter: false\n  image_proxy: false\nsearch:\n  formats:\n    - html\n    - json\n' "$SECRET" | sudo tee /opt/searxng/settings.yml > /dev/null
   sudo docker run -d --name searxng --restart unless-stopped \
     -p 127.0.0.1:8888:8080 -v /opt/searxng:/etc/searxng \
     docker.io/searxng/searxng:latest
   ```

6. Install the firewall rule and the panel service. Apple's push service requires a contact address from every sender, so replace `you@example.com` with your own address before running this:

   ```bash
   sudo cp deploy/agent-firewall.service deploy/agent-web.service /etc/systemd/system/
   sudo mkdir -p /etc/systemd/system/agent-web.service.d
   printf '[Service]\nEnvironment="AGENT_PUSH_SUB=mailto:you@example.com"\n' | sudo tee /etc/systemd/system/agent-web.service.d/local.conf
   sudo systemctl daemon-reload
   sudo systemctl enable --now agent-firewall agent-web
   sudo cp deploy/logrotate-agent /etc/logrotate.d/agent
   ```

7. Publish the panel on your tailnet. HTTPS certificates need to be turned on in the Tailscale admin console under DNS.

   ```bash
   sudo tailscale serve --bg 8000
   tailscale serve status
   ```

8. On an iPhone, open the address from `tailscale serve status` in Safari, add it to the home screen, open it from the icon, and tap Alerts. Web Push on iOS works only for home-screen web apps, on iOS 16.4 or later.

To check the isolation, run `sudo -u agent curl -s -m 3 http://127.0.0.1:8000/api/state; echo $?`. It should print `7` (connection refused). Then ask the agent to run `whoami`; it should report `agent`.

The terminal version still works for testing at the keyboard. It runs everything as `agent` and logs to `/home/agent/actions.jsonl`:

```bash
sudo -u agent -H /opt/agent/venv/bin/python /opt/agent/agent.py
```

## Updating from GitHub

The server deploys itself from the `main` branch of this repository. A systemd timer on the laptop runs `deploy/agent-update.sh` every 5 minutes. Nothing on GitHub connects to the laptop; the laptop pulls, so no inbound access or deploy secrets are involved.

When `main` has a commit that isn't deployed yet, the script:

1. Postpones the deploy if the agent is in the middle of a task, and tries again on the next run.
2. Checks the Python files for syntax errors and refuses to deploy if any fail.
3. Backs up the live files to `/opt/agent-backups` (the 10 newest are kept).
4. Runs `pip install` if `requirements.txt` changed.
5. Copies the files into `/opt/agent` and restarts the panel.
6. Waits up to 20 seconds for the panel to answer. If it doesn't, the script restores the backup, restarts again, and marks the commit as bad so it isn't retried. The next commit to `main` is tried normally.

Only `agent.py`, `server.py`, `toolrunner.py`, `jobs.py` and `requirements.txt` are deployed automatically. Files under `deploy/` change root-level configuration (sudo rules, systemd units, firewall), so the script only reports that they changed. Apply those by hand after reviewing them.

Anyone who can push to `main` can change the code this machine runs, so the GitHub account needs two-factor authentication.

One-time setup on the server:

```bash
sudo git clone https://github.com/SaisatwikBiku/personal-llm-server.git /opt/agent-src
sudo install -m 755 /opt/agent-src/deploy/agent-update.sh /usr/local/sbin/agent-update
sudo cp /opt/agent-src/deploy/agent-update.service /opt/agent-src/deploy/agent-update.timer /etc/systemd/system/
sudo chown -R root:root /opt/agent
sudo systemctl daemon-reload
sudo systemctl enable --now agent-update.timer
sudo systemctl start agent-update
journalctl -u agent-update -n 20 --no-pager
```

After this, `/opt/agent` belongs to root and changes go through Git. To deploy right away instead of waiting for the timer, run `sudo systemctl start agent-update`. Deploy history is in `journalctl -u agent-update`.

To roll back by hand, copy a folder from `/opt/agent-backups` into `/opt/agent` and restart `agent-web`, or push a revert commit.

## Configuration

All settings are environment variables, set in the systemd unit or a drop-in file.

| Variable | Default | Purpose |
|---|---|---|
| `AGENT_MODEL` | `agent-4b` | Ollama model name |
| `AGENT_OWNER` | `Sai` | Name the model uses for you in its prompt |
| `AGENT_WORKSPACE` | `/home/agent/workspace` | Working directory for tools |
| `AGENT_LOG` | `/home/agent/actions.jsonl` | Action log |
| `AGENT_SEARXNG` | `http://127.0.0.1:8888` | SearXNG base URL |
| `AGENT_USE_TOOLRUNNER` | unset | `1` runs tools as the agent user through sudo |
| `AGENT_VAPID_KEY` | `/home/agent/vapid_private.pem` | Push signing key, created on first start |
| `AGENT_PUSH_SUBS` | `/home/agent/push_subscriptions.json` | Saved push subscriptions |
| `AGENT_PUSH_SUB` | `mailto:agent@example.com` | Contact address sent to push services |
| `AGENT_ALLOWED_LOGIN` | unset | If set, only this Tailscale login may use the panel |
| `AGENT_JOBS_DIR` | `/home/agentd/jobs` | Job search config, profile, resume and results |
| `AGENT_CHAT_MODEL` | `qwen3:8b` | Model for the chat tab's "8B, better" setting |
| `AGENT_CHATS` | `/home/agentd/chats.json` | Saved chat conversations |
| `AGENT_MEMORY` | `/home/agentd/memory.md` | Facts added to every chat and task |

Tools that run without approval are listed in `AUTO_APPROVE` in `agent.py`.

## Job search setup

The job search stays off until its folder has a config and a resume. As `sai` on the server, with `profile.json` and `resume.txt` copied into the home directory first:

```bash
sudo install -d -o agentd -g agentd -m 700 /home/agentd/jobs
sudo install -o agentd -g agentd -m 600 /opt/agent-src/deploy/jobs-config.example.json /home/agentd/jobs/config.json
sudo install -o agentd -g agentd -m 600 ~/profile.json ~/resume.txt /home/agentd/jobs/
```

Then fill in the Profile tab in the panel: every field application forms ask for, grouped as Name, Contact, Links, Work authorization, Experience, Education, Job preferences and optional self-identification, built from the questions on real application forms. Below the fields it lists the questions it still can't answer, most common first; answer one once and every form that asks it gets that answer. An empty field makes the matching form question show up as one to answer yourself. Legal consents are never answered for you. Edit `config.json` to change roles, companies (board names as they appear in the job board URLs; for Workday, `<tenant>.wdN/<site>` from a URL like `https://mtb.wd5.myworkdayjobs.com/MTB/job/...`, and for SmartRecruiters the company name from `https://jobs.smartrecruiters.com/<company>/...`), the run and digest times, and the score thresholds; the next run picks up the change without a restart. The first run happens at the next `run_at` time, or right away from Run now in the Jobs view.

Each run can score up to 300 postings (`max_scored_per_run`), about 100 minutes of model time at 20 seconds each, and the rest wait for the next night. It then drafts answers for up to 60 strong matches (`max_drafts_per_run`), about 2 minutes each with the 8B, so a full run ends around 5 a.m. Other jobs can be drafted on demand with Prepare answers on their card. Panel tasks take priority: the job search waits whenever a task is running.

### Autofill

Copy the resume PDF into the jobs folder as `resume.pdf` (owned by `agentd`, mode 600, like the other files). Then install the userscript from the Jobs view (Autofill script):

- iPhone: install the free Userscripts app from the App Store, turn it on in Settings > Apps > Safari > Extensions and allow it on greenhouse.io, lever.co and ashbyhq.com, then open the Autofill script link in Safari and install it.
- Mac: install Tampermonkey (Chrome) or Userscripts (Safari), then open the Autofill script link.

To give the agent its own inbox, make a new Yahoo or Gmail address and create an app password for it (Yahoo: Account Info, Account Security, Generate app password; Gmail: turn on 2-step verification, then Google Account, Security, App passwords), and in the Jobs view choose Emails and connect it. It becomes the email on your applications. Every 5 minutes the panel reads new mail without marking it read: an "application received" email marks the job Applied, interview requests and rejections move the job to Interviewing or Rejected, and verification codes appear in the list and as an alert. Email text is shown as plain text, never given to the model, and its links are never opened.

On a Greenhouse, Lever or Ashby application page, tap Fill from agent. Workday and SmartRecruiters forms aren't supported; for those, copy answers from the job's card in the Jobs view. It fills text fields, dropdowns and yes/no questions from the answers prepared for that job (or from `profile.json` for any other job on those sites), attaches the resume, and outlines each field: green filled, blue a draft to read, orange for you. Consents, legal and demographic questions, and anything it has no answer for stay empty. Check everything, solve the CAPTCHA, submit, and tap Mark applied. It needs Tailscale on, since it asks the panel for the answers.

## Running a laptop as a server

A few things this particular laptop needed, which may save time on similar hardware:

- Closing the lid suspends the machine by default. A logind drop-in with `HandleLidSwitch=ignore` and masking the sleep targets keeps it running.
- Lenovo's airplane-mode setting survived the switch from Windows and left the Wi-Fi radio soft-blocked (`ideapad_wlan` in `/sys/class/rfkill`). Writing `0` to the `soft` files cleared it.
- Intel Wi-Fi power saving made SSH sessions sluggish. `deploy/wifi-powersave-off.service` turns it off at boot.
- Lenovo's battery conservation mode, at `/sys/bus/platform/drivers/ideapad_acpi/VPC2004:00/conservation_mode`, caps charging to protect a battery that stays plugged in.
- With `OLLAMA_KEEP_ALIVE=-1`, every model loaded during benchmarking stayed in memory until 11 GB of 16 were in use. `OLLAMA_MAX_LOADED_MODELS=1` prevents that.
- Unattended upgrades with an automatic 4 a.m. reboot keep kernel security fixes applied.

## Limitations

A 4B model makes confident mistakes. During testing it suggested `sort -hr | tail -5` to find the five largest directories, which returns the five smallest, and it once ranked 16K above 32K. The system prompt now tells it to let commands do sorting and arithmetic, which fixed those cases, but the approval step is the real safeguard, and reading each command before approving it is part of using this.

Each task starts with an empty context apart from the memory list, so the agent remembers nothing else between tasks. Chat keeps its conversation, but only the most recent 12,000 characters are sent to the model. Only one task runs at a time. Stopping a task takes effect after the current model call returns, which can take up to half a minute.

The CPU does all the work, so a task that needs many steps or reads long output takes minutes. The 8B model gives better answers at half the speed, and switching to it is a one-line change to `AGENT_MODEL`.
