#!/bin/bash
# Daily flight price tracker + email report.
# Scheduled via cron — runs every day at 4 AM PST.
#
# Tracked routes:
#   1. AKL  — depart 2026-11-21  trip 9d ±1  flex ±3 days
#   2. ZQN  — depart 2026-11-21  trip 9d ±1  flex ±3 days
#   3. BOI  — depart 2026-05-25  return 2026-05-27  no flex
#   4. PVG  — depart 2026-06-15  trip 18d ±4  flex ±10 days

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/results/cron.log"
PYTHON="$(which python3)"

# ── Departure airports per route (space-separated IATA codes) ────────────────
ORIGINS_NZ="SEA YVR"   # AKL + ZQN searches
ORIGINS_BOI="SEA PAE"           # BOI search
ORIGINS_PVG="SEA YVR"       # PVG search

# Load .env credentials into environment
if [[ -f "$SCRIPT_DIR/.env" ]]; then
  set -a; source "$SCRIPT_DIR/.env"; set +a
fi

RUN_ID="$(date '+%Y%m%d-%H%M%S')-$$"
log() { echo "[$RUN_ID $(date '+%H:%M:%S %Z')] $*" | tee -a "$LOG"; }

log "━━━━ RUN START ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
# ── 1. AKL ──────────────────────────────────────────────────────────────────
log "AKL search starting"
"$PYTHON" "$SCRIPT_DIR/track_flights.py" \
  --from $ORIGINS_NZ --to AKL --depart 2026-11-21 --trip-days 9 --trip-flex 1 --flex-days 3 \
  >> "$LOG" 2>&1

log "AKL report + email"
"$PYTHON" "$SCRIPT_DIR/generate_report.py" --dest AKL --email >> "$LOG" 2>&1

# ── 2. ZQN ──────────────────────────────────────────────────────────────────
log "ZQN search starting"
"$PYTHON" "$SCRIPT_DIR/track_flights.py" \
  --from $ORIGINS_NZ --to ZQN --depart 2026-11-21 --trip-days 9 --trip-flex 1 --flex-days 3 \
  >> "$LOG" 2>&1

log "ZQN report + email"
"$PYTHON" "$SCRIPT_DIR/generate_report.py" --dest ZQN --email >> "$LOG" 2>&1

# ── 3. BOI ──────────────────────────────────────────────────────────────────
log "BOI search starting"
"$PYTHON" "$SCRIPT_DIR/track_flights.py" \
  --from $ORIGINS_BOI --to BOI --depart 2026-05-25 --return 2026-05-27 \
  >> "$LOG" 2>&1

log "BOI report + email"
"$PYTHON" "$SCRIPT_DIR/generate_report.py" --dest BOI --email >> "$LOG" 2>&1

# ── 4. PVG ──────────────────────────────────────────────────────────────────
log "PVG search starting"
"$PYTHON" "$SCRIPT_DIR/track_flights.py" \
  --from $ORIGINS_PVG --to PVG --depart 2026-06-15 --trip-days 18 --trip-flex 4 --flex-days 10 \
  >> "$LOG" 2>&1

log "PVG report + email"
"$PYTHON" "$SCRIPT_DIR/generate_report.py" --dest PVG --email >> "$LOG" 2>&1

log "━━━━ RUN COMPLETE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Crontab entry (runs every day at 4 AM PST): ────────────────────────────
#   0 12 * * * /Users/yuanfeng/Project/flights/run_daily.sh
# ────────────────────────────────────────────────────────────────────────────
