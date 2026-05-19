#!/usr/bin/env bash
# Auto-update script for Chanlun Trading System.
# Fetches data, regenerates mobile dashboard, and pushes to git.
# Schedule: every 5 min during A-share trading hours + post-close, Mon-Fri.
#
# Usage:
#   ./scripts/auto_update.sh           # Run once
#   ./scripts/auto_update.sh --install # Install cron schedule
#   ./scripts/auto_update.sh --remove  # Remove cron schedule

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOG_DIR}/auto_update_$(date +%Y%m%d).log"
LOCK_FILE="/tmp/chanlun_auto_update.lock"

mkdir -p "$LOG_DIR"

CRON_TAG="chanlun-auto-update"

# ─── Install / Remove cron ─────────────────────────────────────────
if [[ "${1:-}" == "--install" ]]; then
    SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"

    # A-share trading hours only (Mon-Fri):
    #   Morning  09:31-11:31  (every 5 min)
    #   Afternoon 13:01-14:56 (every 5 min)
    #   Post-close 15:06      (final closing data)
    CRON_LINES=$(cat <<CRON
1-56/5 9-14 * * 1-5 ${SCRIPT_PATH} >> ${LOG_DIR}/cron.log 2>&1 # ${CRON_TAG}
6 15 * * 1-5 ${SCRIPT_PATH} >> ${LOG_DIR}/cron.log 2>&1 # ${CRON_TAG}
CRON
)
    # Remove old entries then append
    ( crontab -l 2>/dev/null | grep -v "${CRON_TAG}" || true; echo "$CRON_LINES" ) | crontab -
    echo "Cron jobs installed. Current schedule:"
    crontab -l | grep "${CRON_TAG}"
    exit 0
fi

if [[ "${1:-}" == "--remove" ]]; then
    ( crontab -l 2>/dev/null | grep -v "${CRON_TAG}" || true ) | crontab -
    echo "Cron jobs removed."
    exit 0
fi

TIMEOUT_SEC=240  # 4 minutes max (cron interval is 5 min)

# ─── Lock to prevent concurrent runs ───────────────────────────────
if [[ -f "$LOCK_FILE" ]]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCK_FILE") ))
        if (( lock_age > TIMEOUT_SEC )); then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Stale lock (pid=$pid, age=${lock_age}s > ${TIMEOUT_SEC}s), killing." >> "$LOG_FILE"
            kill "$pid" 2>/dev/null; sleep 2
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            rm -f "$LOCK_FILE"
        else
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] Another instance running (pid=$pid, age=${lock_age}s), skipping." >> "$LOG_FILE"
            exit 0
        fi
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# ─── Main pipeline ─────────────────────────────────────────────────
cd "$PROJECT_DIR"

{
    echo "============================================================"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Auto-update started"
    echo "============================================================"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 1/2: Fetch + analyze + dashboard..."
    run_exit=0
    timeout "${TIMEOUT_SEC}s" python3 main.py run 2>&1 || run_exit=$?
    if (( run_exit == 124 )); then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: Pipeline timed out after ${TIMEOUT_SEC}s"
    elif (( run_exit != 0 )); then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] WARNING: Pipeline exited with code $run_exit"
    fi
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Pipeline complete."

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 2/3: Git push..."
    git add -A
    git diff --cached --quiet && {
        echo "No changes to commit."
    } || {
        git commit -m "bohan: auto-update $(date '+%Y-%m-%d %H:%M')"
        git push
        echo "Pushed to remote."
    }

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 3/3: Deploy to Cloudflare Pages..."
    python3 scripts/deploy_cloudflare.py 2>&1 || echo "WARNING: Cloudflare deploy failed (non-fatal)"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Auto-update complete."
    echo ""
} >> "$LOG_FILE" 2>&1
