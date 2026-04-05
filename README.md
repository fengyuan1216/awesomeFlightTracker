# Flight Price Tracker

Tracks flight prices from Google Flights and sends a daily HTML email report.
Uses `fast-flights` as the primary data source and optionally verifies prices against live SerpApi data.

## Features

- Searches multiple origin airports per route in a single query
- Flexible date ranges: fixed return or trip-length mode with ±flex days
- HTML report with stop categories, airline logos, and Google Flights deep links
- Per-route target prices — report flags ✓/✗ and email subject says "Below target!"
- SerpApi price verification: spot-checks the cheapest result and shows a "✓ Verified" badge (or mismatch warning) in the report
- Deduplicates results across search runs and flex dates
- Daily email via Gmail SMTP, cron-scheduled

## Scripts

| File | Purpose |
|------|---------|
| `track_flights.py` | Searches Google Flights, saves results to `results/<DEST>.csv` |
| `generate_report.py` | Reads CSV, generates HTML report, optionally emails it |
| `verify_price.py` | One-off real-time price check via SerpApi |
| `run_daily.sh` | Orchestrates all routes: search → report → email |
| `.env` | Credentials — copy from `.env.example` (not committed) |
| `results/` | CSV data and HTML reports (not committed) |

## Setup

### 1. Install dependencies

```bash
pip install fast-flights requests
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env — set Gmail sender/receiver/password and optionally SERPAPI_KEY
```

- **Gmail App Password**: <https://myaccount.google.com/apppasswords>
- **SerpApi key** (optional, for price verification): <https://serpapi.com>

### 3. Configure routes and targets

Edit `run_daily.sh`. Key variables at the top:

```bash
ORIGINS_NZ="SEA YVR"     # departure airports for NZ routes

MIN_PRICE_AKL=1100  ;  MAX_STOPS_AKL=1   # alert if < $1100, verify ≤1-stop flights
MIN_PRICE_BOI=100   ;  MAX_STOPS_BOI=0   # alert if < $100,  verify nonstop only
MIN_PRICE_PVG=1000  ;  MAX_STOPS_PVG=1   # alert if < $1000, verify ≤1-stop flights
```

- `MIN_PRICE_*` — best-price threshold shown in the report header and email subject
- `MAX_STOPS_*` — SerpApi verifies the cheapest result with this many stops or fewer (`0` = nonstop only, `1` = ≤1 stop)

### 4. Schedule (macOS cron)

```bash
crontab -e
# Add (runs daily at 4 AM PST = 12:00 UTC):
0 12 * * * /path/to/flights/run_daily.sh
```

## Usage

### Track prices

```bash
# Basic round trip
python track_flights.py --from YVR --to AKL --depart 2026-11-21 --return 2026-11-30

# Trip-length mode with flex dates
python track_flights.py --from SEA YVR --to AKL --depart 2026-11-21 --trip-days 9 --trip-flex 1 --flex-days 3

# One-way
python track_flights.py --from SEA --to AKL --depart 2026-11-21 --one-way

# Use SerpApi instead of fast-flights
python track_flights.py --api serpapi --from YVR --to AKL --depart 2026-11-21 --trip-days 9
```

### Generate report

```bash
# HTML report for one destination
python generate_report.py --dest AKL

# With target price and SerpApi verification (≤1-stop flights)
python generate_report.py --dest AKL --min-price 1100 --verify-stops 1

# Generate and email
python generate_report.py --dest AKL --email --min-price 1100 --verify-stops 1

# All destinations
python generate_report.py
```

### Verify a price on demand

```bash
# Check live prices — compare against a fast-flights result
python verify_price.py --from YVR --to AKL --depart 2026-11-21 --trip-days 9 --max-stops 1 --compare 1248

# Nonstop only
python verify_price.py --from YVR SEA --to AKL --depart 2026-11-21 --return 2026-11-30 --max-stops 0

# One-way, no filter
python verify_price.py --from YVR --to AKL --depart 2026-11-21
```

### View price history

```bash
python track_flights.py --history --to AKL
python track_flights.py --history        # all destinations
```

### Run all routes (same as cron)

```bash
bash run_daily.sh
```

## Backends

| Backend | Key required | Notes |
|---------|-------------|-------|
| `fast-flights` (default) | No | Scrapes Google Flights; may lag real-time by a few minutes |
| `serpapi` | Yes (`SERPAPI_KEY`) | Live Google Flights data; used for on-demand verification |

Switch backend for a single run:
```bash
python track_flights.py --api serpapi --from YVR --to AKL --depart 2026-11-21 --trip-days 9
```
