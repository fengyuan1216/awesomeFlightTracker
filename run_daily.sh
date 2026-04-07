#!/bin/bash
# Daily flight price tracker + email report.
# Scheduled via cron — runs every day at 4 AM PST.
#
# Tracked routes:
#   1. AKL  — depart 2026-11-21  trip 9d ±1  flex ±3 days
#   2. PVG  — depart 2026-06-15  trip 18d ±4  flex ±10 days

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$SCRIPT_DIR/results/cron.log"
PYTHON="/Users/yuanfeng/miniconda3/bin/python"

# ── Backend & departure airports ─────────────────────────────────────────────
# Tracker uses fast-flights by default (read from .env).
# SERPAPI_KEY (from .env) is used automatically for price verification below.

ORIGINS_NZ="SEA YVR"   # AKL search
ORIGINS_PVG="SEA YVR"  # PVG search

# ── Per-route targets ────────────────────────────────────────────────────────
# MIN_PRICE_*  : alert threshold — report flags ✓/✗, email subject says "Below target!"
# MAX_STOPS_*  : SerpApi only verifies the cheapest result with ≤N stops
#                0 = nonstop only  |  1 = nonstop + 1-stop  |  2 = up to 2 stops
MIN_PRICE_AKL=1100  ;  MAX_STOPS_AKL=0   # AKL: flag if < $1100, verify nonstop only
MIN_PRICE_PVG=1000  ;  MAX_STOPS_PVG=1   # PVG: flag if < $1000, verify ≤1-stop

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
"$PYTHON" "$SCRIPT_DIR/generate_report.py" \
  --dest AKL --email \
  --min-price $MIN_PRICE_AKL --verify-stops $MAX_STOPS_AKL \
  >> "$LOG" 2>&1

# ── 2. PVG ──────────────────────────────────────────────────────────────────
log "PVG search starting"
"$PYTHON" "$SCRIPT_DIR/track_flights.py" \
  --from $ORIGINS_PVG --to PVG --depart 2026-06-15 --trip-days 18 --trip-flex 4 --flex-days 10 \
  >> "$LOG" 2>&1

log "PVG report + email"
"$PYTHON" "$SCRIPT_DIR/generate_report.py" \
  --dest PVG --email \
  --min-price $MIN_PRICE_PVG --verify-stops $MAX_STOPS_PVG \
  >> "$LOG" 2>&1

log "━━━━ RUN COMPLETE ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ─── Crontab entry (runs every day at 4 AM PST): ────────────────────────────
#   0 12 * * * /Users/yuanfeng/Project/flights/run_daily.sh
# ────────────────────────────────────────────────────────────────────────────
