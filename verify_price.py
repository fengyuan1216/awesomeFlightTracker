#!/usr/bin/env python3
"""
Verify a flight price in real time using SerpApi (Google Flights).

Usage:
  python verify_price.py --from YVR --to AKL --depart 2026-11-21
  python verify_price.py --from YVR SEA --to AKL --depart 2026-11-21 --return 2026-11-30
  python verify_price.py --from YVR --to AKL --depart 2026-11-21 --trip-days 9
  python verify_price.py --from YVR --to AKL --depart 2026-11-21 --return 2026-11-30 --max-stops 1
  python verify_price.py --from YVR --to AKL --depart 2026-11-21 --return 2026-11-30 --compare 1248
"""
from __future__ import annotations

import argparse
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_env():
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _minutes_to_hm(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h}h {m:02d}m"


def _stops_label(n: int) -> str:
    return {0: "Nonstop", 1: "1 stop"}.get(n, f"{n} stops")


def _fmt_leg(legs: list[dict], layovers: list[dict], total_minutes: int) -> str:
    if not legs:
        return "—"
    dep = legs[0]["departure_airport"]
    arr = legs[-1]["arrival_airport"]
    airlines = []
    seen: set[str] = set()
    for leg in legs:
        name = leg.get("airline", "")
        if name and name not in seen:
            airlines.append(name)
            seen.add(name)
    stops = _stops_label(len(layovers))
    return (f"{dep['id']} {dep['time'][11:16]} → {arr['id']} {arr['time'][11:16]}"
            f"  {_minutes_to_hm(total_minutes)}  {stops}  [{', '.join(airlines)}]")


# ---------------------------------------------------------------------------
# Core fetch
# ---------------------------------------------------------------------------

def fetch_flights(
    origins: list[str],
    destination: str,
    depart_date: str,
    return_date: str | None,
    adults: int,
    api_key: str,
    max_stops: int | None,
) -> list[dict]:
    """Fetch real-time Google Flights results via SerpApi. Returns raw flight dicts."""
    import requests

    params = {
        "engine":         "google_flights",
        "departure_id":   ",".join(origins),
        "arrival_id":     destination,
        "outbound_date":  depart_date,
        "adults":         str(adults),
        "currency":       "USD",
        "hl":             "en",
        "no_cache":       "true",
        "api_key":        api_key,
        "type":           "2" if not return_date else "1",
    }
    if return_date:
        params["return_date"] = return_date
    if max_stops is not None:
        params["stops"] = str(max_stops + 1)   # SerpApi: 1=nonstop, 2=≤1 stop, 3=≤2 stops

    resp = requests.get("https://serpapi.com/search", params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(data["error"])

    return data.get("best_flights", []) + data.get("other_flights", [])


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def print_results(
    flights: list[dict],
    origins: list[str],
    destination: str,
    depart_date: str,
    return_date: str | None,
    max_stops: int | None,
    compare_price: float | None,
):
    trip = f"{'/'.join(origins)} → {destination}  {depart_date}"
    if return_date:
        trip += f" ↩ {return_date}"
    stops_filter = "" if max_stops is None else f"  (≤{max_stops} stop{'s' if max_stops != 1 else ''})"
    print(f"\nReal-time SerpApi results — {trip}{stops_filter}")
    print(f"Fetched: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print("─" * 72)

    if not flights:
        print("  No flights found.")
        return

    flights_sorted = sorted(flights, key=lambda f: f.get("price", 999_999))

    for i, f in enumerate(flights_sorted, 1):
        price = f.get("price", 0)
        legs  = f.get("flights", [])
        layovers = f.get("layovers", [])
        duration = f.get("total_duration", 0)

        leg_str = _fmt_leg(legs, layovers, duration)

        # Compare badge
        compare_str = ""
        if compare_price is not None:
            delta = price - compare_price
            sign = "+" if delta >= 0 else ""
            compare_str = f"  (vs ${compare_price:,.0f}: {sign}${delta:,.0f})"

        marker = "▶" if i == 1 else " "
        print(f"  {marker} #{i:>2}  ${price:>7,.0f}{compare_str}")
        print(f"         {leg_str}")

    cheapest = flights_sorted[0].get("price", 0)
    print("─" * 72)
    print(f"  Cheapest: ${cheapest:,.0f}")

    if compare_price is not None:
        gap = abs(cheapest - compare_price)
        direction = "cheaper" if cheapest < compare_price else "more expensive"
        verified = "✓ VERIFIED (gap ≤ $50)" if gap <= 50 else f"⚠  GAP: ${gap:,.0f} {direction} than compared price"
        print(f"  Compare:  ${compare_price:,.0f}  →  {verified}")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    _load_env()

    parser = argparse.ArgumentParser(
        description="Verify a flight price in real time via SerpApi.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--from", dest="origins", nargs="+", required=True, metavar="IATA",
                        help="Departure airport(s), e.g. YVR SEA")
    parser.add_argument("--to", required=True, metavar="IATA",
                        help="Destination airport, e.g. AKL")
    parser.add_argument("--depart", required=True, metavar="YYYY-MM-DD",
                        help="Outbound date")
    parser.add_argument("--return", dest="return_date", metavar="YYYY-MM-DD",
                        help="Return date (omit for one-way)")
    parser.add_argument("--trip-days", type=int, metavar="N",
                        help="Return = depart + N days (alternative to --return)")
    parser.add_argument("--adults", type=int, default=1,
                        help="Number of adult passengers (default: 1)")
    parser.add_argument("--max-stops", type=int, default=None, metavar="N",
                        help="Filter: only show flights with ≤N stops (0=nonstop, 1=1-stop, …)")
    parser.add_argument("--compare", type=float, default=None, metavar="USD",
                        help="Price to compare against (e.g. from fast-flights); shows gap + verified badge")
    args = parser.parse_args()

    api_key = os.environ.get("SERPAPI_KEY", "")
    if not api_key:
        parser.error("SERPAPI_KEY not found. Set it in .env or export SERPAPI_KEY=…")

    return_date = args.return_date
    if args.trip_days is not None:
        base = datetime.strptime(args.depart, "%Y-%m-%d")
        return_date = (base + timedelta(days=args.trip_days)).strftime("%Y-%m-%d")

    origins = [o.upper() for o in args.origins]
    destination = args.to.upper()

    print(f"Querying SerpApi (no_cache) …", end=" ", flush=True)
    try:
        flights = fetch_flights(
            origins, destination, args.depart, return_date,
            args.adults, api_key, args.max_stops,
        )
        print(f"{len(flights)} flights found")
    except Exception as e:
        print(f"\nError: {e}")
        raise SystemExit(1)

    print_results(flights, origins, destination, args.depart, return_date,
                  args.max_stops, args.compare)


if __name__ == "__main__":
    main()
