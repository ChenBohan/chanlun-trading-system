#!/usr/bin/env bash
# Auto-update script for Chanlun Trading System.
# Fetches data, regenerates mobile dashboard, and pushes to git.
# Schedule: every 5 min during A-share trading hours + post-close, Mon-Fri.
#
# Usage:
#   ./scripts/auto_update.sh                  # Run once
#   ./scripts/auto_update.sh --install        # Install schedule (cron, or systemd user timer)
#   ./scripts/auto_update.sh --install-systemd # Install systemd user timer only
#   ./scripts/auto_update.sh --remove         # Remove cron + systemd timer

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="${PROJECT_DIR}/logs"
LOG_FILE="${LOG_DIR}/auto_update_$(date +%Y%m%d).log"
LOCK_FILE="/tmp/chanlun_auto_update.lock"

mkdir -p "$LOG_DIR"

CRON_TAG="chanlun-auto-update"
SYSTEMD_UNIT="chanlun-auto-update.timer"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"

_install_systemd_timer() {
    mkdir -p "$SYSTEMD_USER_DIR" "$LOG_DIR"
    local svc="${SYSTEMD_USER_DIR}/chanlun-auto-update.service"
    local tmr="${SYSTEMD_USER_DIR}/chanlun-auto-update.timer"
    if [[ ! -f "$svc" || ! -f "$tmr" ]]; then
        echo "ERROR: Missing ${svc} or ${tmr}. Run from repo with systemd unit files present." >&2
        return 1
    fi
    systemctl --user daemon-reload
    systemctl --user enable --now "$SYSTEMD_UNIT"
    echo "Systemd user timer enabled:"
    systemctl --user list-timers --no-pager | grep -F "$SYSTEMD_UNIT" || true
}

_remove_systemd_timer() {
    systemctl --user disable --now "$SYSTEMD_UNIT" 2>/dev/null || true
    echo "Systemd user timer removed (unit files kept under ${SYSTEMD_USER_DIR})."
}

# ─── Install / Remove schedule ─────────────────────────────────────
if [[ "${1:-}" == "--install-systemd" ]]; then
    _install_systemd_timer
    exit 0
fi

if [[ "${1:-}" == "--install" ]]; then
    SCRIPT_PATH="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
    mkdir -p "$LOG_DIR"

    # A-share trading hours only (Mon-Fri):
    #   09:01-14:56 every 5 min + post-close 15:06
    CRON_LINES=$(cat <<CRON
1-56/5 9-14 * * 1-5 ${SCRIPT_PATH} >> ${LOG_DIR}/cron.log 2>&1 # ${CRON_TAG}
6 15 * * 1-5 ${SCRIPT_PATH} >> ${LOG_DIR}/cron.log 2>&1 # ${CRON_TAG}
CRON
)
    if ( crontab -l 2>/dev/null | grep -v "${CRON_TAG}" || true; echo "$CRON_LINES" ) | crontab - 2>/dev/null; then
        echo "Cron jobs installed. Current schedule:"
        crontab -l | grep "${CRON_TAG}"
        exit 0
    fi
    echo "WARN: crontab not writable; falling back to systemd user timer." >&2
    _install_systemd_timer
    exit 0
fi

if [[ "${1:-}" == "--remove" ]]; then
    ( crontab -l 2>/dev/null | grep -v "${CRON_TAG}" || true ) | crontab - 2>/dev/null || true
    _remove_systemd_timer
    echo "Cron (if any) and systemd timer removed."
    exit 0
fi

TIMEOUT_SEC=360  # 6 minutes max (to accommodate full deployment)

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

    # Deploy strategy:
    #   Full deploy (all 724 files): first run of day (09:15) and post-close (15:06)
    #   Delta deploy (HTML + live.js only): every other run (~few KB, <5s)
    MINUTE=$(date +%M)
    HOUR=$(date +%H)
    BASELINE_FILE="reports/data/.baseline.json"

    if [[ ! -f "$BASELINE_FILE" ]] || (( HOUR == 15 && MINUTE >= 6 && MINUTE < 11 )); then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 3/3: FULL deploy to Cloudflare Pages..."
        timeout 300s python3 scripts/deploy_cloudflare.py --save-baseline 2>&1 \
            || echo "WARNING: Full deploy failed/timed out (non-fatal)"
    else
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Step 3/3: Delta deploy (live.js only)..."
        timeout 30s python3 scripts/deploy_cloudflare.py --delta 2>&1 \
            || echo "WARNING: Delta deploy failed (non-fatal)"
    fi

    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Auto-update complete."
    echo ""
} >> "$LOG_FILE" 2>&1
