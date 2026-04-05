# Flight Price Tracker

Tracks flight prices from Google Flights (via `fast-flights`) and sends a daily HTML email report.

## Features

- Searches multiple origin airports per route
- Flexible date ranges: fixed return or trip-length mode with ±flex days
- HTML report with stop categories, airline logos, and Google Flights deep links
- Deduplicates results across search runs and flex dates
- Daily email via Gmail SMTP
- Cron-scheduled

## Setup

### 1. Install dependencies

```bash
pip install fast-flights
```

### 2. Configure credentials

```bash
cp .env.example .env
# Edit .env with your Gmail address and App Password
```

Gmail App Password: <https://myaccount.google.com/apppasswords>

### 3. Configure routes

Edit `run_daily.sh` — set the `ORIGINS_*` variables and route parameters at the top.

### 4. Schedule (macOS cron)

```bash
crontab -e
# Add:
0 12 * * * /path/to/flights/run_daily.sh
```

(12:00 UTC = 4:00 AM PST)

## Usage

```bash
# Search and save results
python track_flights.py --from SEA YVR --to AKL --depart 2026-11-21 --trip-days 9 --trip-flex 1 --flex-days 3

# Generate HTML report
python generate_report.py --dest AKL

# Generate and email report
python generate_report.py --dest AKL --email

# Run all routes
bash run_daily.sh
```

## Files

| File | Purpose |
|------|---------|
| `track_flights.py` | Searches Google Flights, saves to `results/<DEST>.csv` |
| `generate_report.py` | Reads CSV, generates HTML report, sends email |
| `run_daily.sh` | Runs all routes and sends emails |
| `.env` | Gmail credentials (not committed) |
| `results/` | CSV data and HTML reports (not committed) |
