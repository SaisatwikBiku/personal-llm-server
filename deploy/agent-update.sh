#!/bin/bash
# Deploy the latest commit on the GitHub branch to /opt/agent.
# Runs as root from agent-update.service (triggered by agent-update.timer).
#
# Only the Python files and requirements.txt are deployed automatically.
# Files under deploy/ (systemd units, sudoers, logrotate) touch root-level
# configuration, so changes there are reported but never applied by this script.
set -euo pipefail

SRC=/opt/agent-src
DEST=/opt/agent
BRANCH=main
STATE=/var/lib/agent-update
BACKUPS=/opt/agent-backups
PANEL=http://127.0.0.1:8000/api/state
FILES=(agent.py server.py toolrunner.py requirements.txt)

mkdir -p "$STATE" "$BACKUPS"
cd "$SRC"

git fetch --quiet origin "$BRANCH"
TARGET=$(git rev-parse "origin/$BRANCH")
DEPLOYED=$(cat "$STATE/deployed" 2>/dev/null || true)
BAD=$(cat "$STATE/bad" 2>/dev/null || true)

if [ "$TARGET" = "$DEPLOYED" ]; then
    exit 0
fi
if [ "$TARGET" = "$BAD" ]; then
    echo "Commit ${TARGET:0:7} failed before; waiting for a new commit."
    exit 0
fi

# Commits that only touch the README or deploy/ don't need a restart.
if [ -n "$DEPLOYED" ] && git cat-file -e "$DEPLOYED" 2>/dev/null \
   && git diff --quiet "$DEPLOYED" "$TARGET" -- "${FILES[@]}"; then
    git reset --quiet --hard "$TARGET"
    echo "$TARGET" > "$STATE/deployed"
    echo "No code changes in ${TARGET:0:7}; nothing to restart."
    if ! git diff --quiet "$DEPLOYED" "$TARGET" -- deploy/; then
        echo "NOTE: files under deploy/ changed. Review and apply them by hand:"
        git diff --stat "$DEPLOYED" "$TARGET" -- deploy/
    fi
    exit 0
fi

# Don't restart the panel in the middle of a task; try again on the next run.
STATUS=$(curl -fsS -m 5 "$PANEL" 2>/dev/null \
    | python3 -c 'import sys, json; print(json.load(sys.stdin)["status"])' 2>/dev/null || echo unreachable)
if [ "$STATUS" != "idle" ] && [ "$STATUS" != "unreachable" ]; then
    echo "Agent is $STATUS; postponing deploy of ${TARGET:0:7}."
    exit 0
fi

git reset --quiet --hard "$TARGET"
echo "Deploying ${TARGET:0:7}: $(git log -1 --format=%s)"

# 1. Validate before touching the live copy.
if ! "$DEST/venv/bin/python" -m py_compile agent.py server.py toolrunner.py; then
    echo "Syntax check failed; not deploying ${TARGET:0:7}."
    echo "$TARGET" > "$STATE/bad"
    exit 1
fi

# 2. Back up the current live files.
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$BACKUPS/$STAMP"
for f in "${FILES[@]}"; do
    [ -f "$DEST/$f" ] && cp -p "$DEST/$f" "$BACKUPS/$STAMP/"
done
ls -1dt "$BACKUPS"/*/ | tail -n +11 | xargs -r rm -rf   # keep the 10 newest

# 3. Install dependencies if requirements.txt changed.
if ! cmp -s requirements.txt "$DEST/requirements.txt"; then
    echo "requirements.txt changed; installing packages."
    "$DEST/venv/bin/pip" install --quiet -r requirements.txt
fi

# 4. Swap in the new files and restart.
install -o root -g root -m 644 "${FILES[@]}" "$DEST/"
systemctl restart agent-web

# 5. Health check, with rollback.
for _ in $(seq 1 20); do
    if curl -fsS -m 3 "$PANEL" > /dev/null 2>&1; then
        echo "$TARGET" > "$STATE/deployed"
        rm -f "$STATE/bad"
        echo "Deployed ${TARGET:0:7}."
        if [ -n "$DEPLOYED" ] && git cat-file -e "$DEPLOYED" 2>/dev/null \
           && ! git diff --quiet "$DEPLOYED" "$TARGET" -- deploy/; then
            echo "NOTE: files under deploy/ changed. Review and apply them by hand:"
            git diff --stat "$DEPLOYED" "$TARGET" -- deploy/
        fi
        exit 0
    fi
    sleep 1
done

echo "Panel did not come back after deploying ${TARGET:0:7}; rolling back."
cp -p "$BACKUPS/$STAMP"/* "$DEST/"
systemctl restart agent-web
echo "$TARGET" > "$STATE/bad"
exit 1
