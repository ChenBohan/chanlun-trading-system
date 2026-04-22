#!/usr/bin/env bash
# Auto-update script for Chanlun Trading System.
# Fetches data, regenerates dashboard, and pushes to git.
# Designed to be called by cron during A-share trading hours.
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

    # Trading-hour schedule (Mon-Fri, China Standard Time):
    #   Every 5 min during morning session  (9:30-11:30)
    #   Every 5 min during afternoon session (13:00-15:00)
    #   Final update at 15:05 after market close
    CRON_LINES=$(cat <<CRON
30-59/5 9 * * 1-5 ${SCRIPT_PATH} >> ${LOG_DIR}/cron.log 2>&1 # ${CRON_TAG}
*/5 10-11 * * 1-5 ${SCRIPT_PATH} >> ${LOG_DIR}/cron.log 2>&1 # ${CRON_TAG}
*/5 13-14 * * 1-5 ${SCRIPT_PATH} >> ${LOG_DIR}/cron.log 2>&1 # ${CRON_TAG}
0,5 15 * * 1-5 ${SCRIPT_PATH} >> ${LOG_DIR}/cron.log 2>&1 # ${CRON_TAG}
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

# ─── Lock to prevent concurrent runs ───────────────────────────────
if [[ -f "$LOCK_FILE" ]]; then
    pid=$(cat "$LOCK_FILE" 2>/dev/null || echo "")
    if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Another instance running (pid=$pid), skipping." >> "$LOG_FILE"
        exit 0
    fi
fi
echo $$ > "$LOCK_FILE"
trap 'rm -f "$LOCK_FILE"' EXIT

# ─── Skip weekends ─────────────────────────────────────────────────
DOW=$(date +%u)
if [[ "$DOW" -gt 5 ]]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Weekend, skipping." >> "$LOG_FILE"
    exit 0
fi

# ─── Main pipeline ─────────────────────────────────────────────────
cd "$PROJECT_DIR"

{
    echo "============================================================"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Auto-update started"
    echo "============================================================"

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 1/3: Fetching data..."
    python3 main.py fetch 2>&1
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Fetch complete."

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 2/3: Running analysis + dashboard..."
    python3 main.py run 2>&1
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dashboard generated."

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 3/3: Git push..."
    git add -A
    git diff --cached --quiet && {
        echo "No changes to commit."
    } || {
        git commit -m "bohan: auto-update $(date '+%Y-%m-%d %H:%M')"
        git push
        echo "Pushed to remote."
    }

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Auto-update complete."
    echo ""
} >> "$LOG_FILE" 2>&1
