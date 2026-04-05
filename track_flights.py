#!/usr/bin/env python3
"""
Flight price tracker using Google Flights (via fast-flights, no API key needed).
Tracks prices over time and saves results to CSV.

Install:
  pip install fast-flights

Usage examples:
  # Track pre-configured routes (Nov 20 departure, Dec 1 return)
  python track_flights.py

  # Custom destination and dates
  python track_flights.py --from YVR --to AKL --depart 2026-11-20 --return 2026-12-01
  python track_flights.py --from SEA YVR --to ZQN --depart 2026-11-22 --return 2026-12-05

  # One-way
  python track_flights.py --from SEA --to AKL --depart 2026-11-20 --one-way

  # Search ±3 days around each date (finds cheaper dates nearby)
  python track_flights.py --from SEA YVR PAE --to AKL --depart 2026-11-20 --return 2026-12-01 --flex-days 3

  # Show saved price history
  python track_flights.py --history
  python track_flights.py --history --to AKL
"""

import argparse
import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fast_flights import FlightData, Passengers, get_flights

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ORIGIN_AIRPORTS = ["SEA", "YVR", "PAE"]

DEFAULT_ROUTES = [
    {"to": "AKL", "depart": "2026-11-20", "return": "2026-12-01"},
    {"to": "ZQN", "depart": "2026-11-20", "return": "2026-12-01"},
]

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _fetch(flight_data, trip, adults, *, max_attempts=5, backoff=3):
    """Fetch flights with retries and quality checks.

    Attempt order per round:
      1. fetch_mode='common'  (direct scrape, fast)
      2. fetch_mode='fallback' (playwright, slower but more reliable)

    A result is considered 'good' when at least one flight has a parsed
    stop count (not 'Unknown').  We keep retrying until we get a good
    result or exhaust all attempts.
    """
    import time

    modes = ["common", "fallback"]
    attempt = 0
    last_result = None

    while attempt < max_attempts:
        mode = modes[attempt % len(modes)]
        try:
            result = get_flights(
                flight_data=flight_data,
                trip=trip,
                seat="economy",
                passengers=Passengers(adults=adults),
                fetch_mode=mode,
            )
        except Exception:
            result = None

        if result and result.flights:
            last_result = result
            good = sum(1 for f in result.flights
                       if str(getattr(f, "stops", "Unknown")) != "Unknown")
            if good > 0:
                return result          # quality data — done
        attempt += 1
        if attempt < max_attempts:
            time.sleep(backoff * attempt)   # 3 s, 6 s, 9 s, 12 s …

    return last_result   # best we got (may still be all-Unknown)


def search_google_flights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 1,
) -> list[dict]:
    flight_data = [FlightData(date=depart_date, from_airport=origin, to_airport=destination)]
    trip = "one-way"

    if return_date:
        flight_data.append(FlightData(date=return_date, from_airport=destination, to_airport=origin))
        trip = "round-trip"

    result = _fetch(flight_data, trip, adults)
    if not result or not result.flights:
        return []

    # If round-trip search returned poor detail (all Unknown stops/airline),
    # fetch a separate one-way outbound search to fill in the missing fields.
    # Match by price-rank: cheapest RT ↔ cheapest OW, 2nd ↔ 2nd, etc.
    ow_by_rank: dict[int, object] = {}
    rt_flights = result.flights
    all_unknown = all(str(getattr(f, "stops", "Unknown")) == "Unknown" for f in rt_flights)
    if all_unknown and return_date:
        ow = _fetch(
            [FlightData(date=depart_date, from_airport=origin, to_airport=destination)],
            "one-way", adults,
        )
        if ow and ow.flights:
            rt_sorted = sorted(rt_flights, key=lambda f: _parse_price(f.price))
            ow_sorted = sorted(ow.flights,  key=lambda f: _parse_price(f.price))
            ow_by_rank = {i: f for i, f in enumerate(ow_sorted)}
            rt_flights = rt_sorted

    # Secondary one-way search for return leg details, grouped by stops
    ret_by_stops: dict[str, list] = {}
    ret_fallback: list = []
    if return_date:
        ret_result = _fetch(
            [FlightData(date=return_date, from_airport=destination, to_airport=origin)],
            "one-way", adults,
        )
        if ret_result and ret_result.flights:
            ret_fallback = ret_result.flights
            for rf in ret_result.flights:
                ret_by_stops.setdefault(str(getattr(rf, "stops", "?")), []).append(rf)

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    rt_price_sorted = sorted(rt_flights, key=lambda f: _parse_price(f.price))
    for rank, flight in enumerate(rt_price_sorted):
        # Use one-way outbound details when round-trip had Unknown stops
        ow_detail = ow_by_rank.get(rank)
        detail = ow_detail if (ow_detail and str(getattr(flight, "stops", "Unknown")) == "Unknown") else flight

        out_stops = str(getattr(detail, "stops", ""))
        # Match return flight with same stop count; fall back to any cheapest
        candidates = ret_by_stops.get(out_stops) or ret_fallback
        best_ret = min(candidates, key=lambda f: _parse_price(f.price)) if candidates else None

        row = {
            "checked_at": checked_at,
            "origin": origin,
            "destination": destination,
            "depart_date": depart_date,
            "return_date": return_date or "",
            "price": flight.price,            # always from round-trip (combined fare)
            "price_numeric": _parse_price(flight.price),
            # Outbound leg – from one-way search if RT was Unknown
            "duration": getattr(detail, "duration", ""),
            "stops": getattr(detail, "stops", ""),
            "airline": getattr(detail, "name", ""),
            "departure": getattr(detail, "departure", ""),
            "arrival": getattr(detail, "arrival", ""),
            "arrival_time_ahead": getattr(detail, "arrival_time_ahead", ""),
            # Return leg
            "ret_duration": getattr(best_ret, "duration", "") if best_ret else "",
            "ret_stops": getattr(best_ret, "stops", "") if best_ret else "",
            "ret_airline": getattr(best_ret, "name", "") if best_ret else "",
            "ret_departure": getattr(best_ret, "departure", "") if best_ret else "",
            "ret_arrival": getattr(best_ret, "arrival", "") if best_ret else "",
            "ret_arrival_time_ahead": getattr(best_ret, "arrival_time_ahead", "") if best_ret else "",
        }
        rows.append(row)

    return rows


def _parse_price(price_str: str) -> float:
    """Extract numeric value from price strings like 'CA$1,234' or '$2,345'."""
    if not price_str:
        return 0.0
    cleaned = ""
    for ch in price_str:
        if ch.isdigit() or ch == ".":
            cleaned += ch
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------------------
# Date flexibility helper
# ---------------------------------------------------------------------------

def date_variants(date_str: str, flex_days: int) -> list[str]:
    base = datetime.strptime(date_str, "%Y-%m-%d")
    return [
        (base + timedelta(days=d)).strftime("%Y-%m-%d")
        for d in range(-flex_days, flex_days + 1)
    ]


# ---------------------------------------------------------------------------
# CSV persistence
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "checked_at", "origin", "destination", "depart_date", "return_date",
    "price", "price_numeric",
    # Outbound leg
    "duration", "stops", "airline", "departure", "arrival", "arrival_time_ahead",
    # Return leg
    "ret_duration", "ret_stops", "ret_airline", "ret_departure", "ret_arrival", "ret_arrival_time_ahead",
]


def csv_path(destination: str) -> Path:
    return RESULTS_DIR / f"{destination.upper()}.csv"


def save_results(rows: list[dict], destination: str):
    path = csv_path(destination)

    if path.exists():
        # Check whether the on-disk header matches current FIELDNAMES
        with open(path, newline="") as f:
            existing_fields = csv.DictReader(f).fieldnames or []
        if existing_fields != FIELDNAMES:
            # Schema changed — rewrite file with new header, preserving old rows
            with open(path, newline="") as f:
                old_rows = list(csv.DictReader(f))
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(old_rows)   # missing new cols written as ""
            print(f"  Migrated {path.name} schema ({len(existing_fields)} → {len(FIELDNAMES)} columns)")

    with open(path, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
        if path.stat().st_size == 0:
            writer.writeheader()
        writer.writerows(rows)
    print(f"  Saved {len(rows)} result(s) → {path}")


def show_history(destination: str):
    path = csv_path(destination)
    if not path.exists():
        print(f"No history found for {destination} (expected {path})")
        return

    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        print("History file is empty.")
        return

    print(f"\n{'='*70}")
    print(f"Price history for {destination.upper()} ({len(rows)} records)")
    print(f"{'='*70}")

    # Group rows by check session (same minute = same run)
    sessions: dict[str, list[dict]] = {}
    for r in rows:
        key = r["checked_at"][:16]
        sessions.setdefault(key, []).append(r)

    for session_time, session_rows in sorted(sessions.items()):
        valid = [r for r in session_rows if r["price_numeric"]]
        if not valid:
            continue
        cheapest = min(valid, key=lambda r: float(r["price_numeric"]))
        print(
            f"  {session_time}  cheapest: {cheapest['price']:>12}  "
            f"{cheapest['origin']}→{cheapest['destination']}  "
            f"depart {cheapest['depart_date']}  "
            f"{cheapest['stops']}  {cheapest['airline']}"
        )

    valid_rows = [r for r in rows if r.get("price_numeric")]
    if valid_rows:
        prices = [float(r["price_numeric"]) for r in valid_rows]
        print(f"\n  All-time low: {min(prices):.0f}  |  Latest: {float(valid_rows[-1]['price_numeric']):.0f}")


# ---------------------------------------------------------------------------
# Main search flow
# ---------------------------------------------------------------------------

def compute_date_pairs(
    depart_date: str,
    return_date: str | None,
    flex_days: int,
    trip_days: int | None,
    trip_flex: int,
) -> list[tuple[str, str | None]]:
    """Return all (depart, return) pairs to search.

    Two modes:
    • Fixed return date  – vary both dates independently by ±flex_days.
    • Trip-length mode   – vary departure by ±flex_days; return = depart +
                           trip_days, then also vary that by ±trip_flex.
    """
    depart_dates = date_variants(depart_date, flex_days)

    if trip_days is not None:
        pairs: list[tuple[str, str | None]] = []
        for dd in depart_dates:
            base_ret = datetime.strptime(dd, "%Y-%m-%d") + timedelta(days=trip_days)
            for rd in date_variants(base_ret.strftime("%Y-%m-%d"), trip_flex):
                pairs.append((dd, rd))
        return pairs

    if return_date:
        return [(dd, rd)
                for dd in depart_dates
                for rd in date_variants(return_date, flex_days)]

    return [(dd, None) for dd in depart_dates]


def run_search(
    destination: str,
    depart_date: str,
    return_date: str | None,
    flex_days: int,
    trip_days: int | None,
    trip_flex: int,
    origins: list[str],
    adults: int,
):
    pairs = compute_date_pairs(depart_date, return_date, flex_days, trip_days, trip_flex)
    print(f"  Date pairs : {len(pairs)} combination(s) × {len(origins)} origin(s) "
          f"= {len(pairs) * len(origins)} search(es)")

    all_rows: list[dict] = []

    for origin in origins:
        for dd, rd in pairs:
                label = f"{origin}→{destination}  {dd}" + (f" / return {rd} ({(datetime.strptime(rd,'%Y-%m-%d')-datetime.strptime(dd,'%Y-%m-%d')).days}d)" if rd else " (one-way)")
                print(f"  Searching {label} …", end=" ", flush=True)
                try:
                    rows = search_google_flights(origin, destination, dd, rd, adults)
                    if not rows:
                        print("no results")
                        continue
                    valid = [r for r in rows if r["price_numeric"]]
                    good  = [r for r in rows if str(r.get("stops","")) not in ("Unknown","")]
                    if valid:
                        cheapest = min(valid, key=lambda r: r["price_numeric"])
                        quality  = "" if good else "  ⚠ stops/times missing (partial page load)"
                        print(
                            f"{len(rows)} flights, cheapest: {cheapest['price']}  "
                            f"({cheapest['stops']})  {cheapest['airline']}{quality}"
                        )
                    else:
                        print(f"{len(rows)} flights (prices unparsed)")
                    all_rows.extend(rows)
                except Exception as e:
                    # Truncate noisy HTML responses (e.g. no-route airports)
                    msg = str(e).splitlines()[0][:120]
                    print(f"no routes ({msg})")

    if all_rows:
        save_results(all_rows, destination)
        valid = [r for r in all_rows if r["price_numeric"]]
        if valid:
            best = min(valid, key=lambda r: r["price_numeric"])
            print(
                f"\n  *** Best for {destination}: {best['price']}  "
                f"{best['origin']} on {best['depart_date']}  "
                f"({best['stops']})  {best['airline']} ***\n"
            )
    else:
        print(f"  No results found for {destination}.\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Track Google Flights prices from SEA/YVR/PAE to any destination.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--to", metavar="IATA", help="Destination airport code (e.g. AKL, ZQN)")
    p.add_argument("--depart", metavar="YYYY-MM-DD", help="Outbound departure date")
    p.add_argument("--return", dest="return_date", metavar="YYYY-MM-DD", help="Return date")
    p.add_argument("--one-way", action="store_true", help="Search one-way only (ignore --return)")
    p.add_argument("--flex-days", type=int, default=0, metavar="N",
                   help="Search ±N days around the departure date (default: 0)")
    p.add_argument("--trip-days", type=int, default=None, metavar="N",
                   help="Trip length in days; return = depart + N (overrides --return)")
    p.add_argument("--trip-flex", type=int, default=0, metavar="N",
                   help="Allow trip length to vary ±N days (use with --trip-days, default: 0)")
    p.add_argument("--from", dest="origins", nargs="+", default=ORIGIN_AIRPORTS, metavar="IATA",
                   help=f"Departure airport(s) (default: {' '.join(ORIGIN_AIRPORTS)})")
    p.add_argument("--origins", dest="origins", nargs="+", metavar="IATA",
                   help=argparse.SUPPRESS)  # legacy alias for --from
    p.add_argument("--adults", type=int, default=1, help="Number of adult passengers (default: 1)")
    p.add_argument("--history", action="store_true",
                   help="Show saved price history (use --to to filter by destination)")
    return p


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.history:
        if args.to:
            show_history(args.to.upper())
        else:
            csv_files = sorted(RESULTS_DIR.glob("*.csv"))
            if not csv_files:
                print("No history found. Run a search first.")
            for f in csv_files:
                show_history(f.stem)
        return

    # Determine routes
    if args.to:
        if not args.depart:
            parser.error("--depart is required when --to is specified")
        routes = [{
            "to": args.to.upper(),
            "depart": args.depart,
            "return": None if (args.one_way or args.trip_days is not None) else args.return_date,
        }]
    else:
        print("No --to specified, using default routes.\n")
        routes = [
            {
                "to": r["to"],
                "depart": r["depart"],
                "return": None if args.one_way else r.get("return"),
            }
            for r in DEFAULT_ROUTES
        ]

    print(f"Search run : {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    print(f"Origins    : {', '.join(args.origins)}")
    if args.trip_days is not None:
        print(f"Trip length: {args.trip_days} days ±{args.trip_flex}  |  Depart flex: ±{args.flex_days} days")
    else:
        print(f"Flex days  : ±{args.flex_days}")
    print()

    for route in routes:
        ret_label = route["return"] or ("one-way" if args.one_way else "—")
        if args.trip_days is not None:
            ret_label = f"depart + {args.trip_days}d ±{args.trip_flex}"
        print(f"{'─'*60}")
        print(f"  {route['to']}  |  depart: {route['depart']}  |  return: {ret_label}")
        print(f"{'─'*60}")
        run_search(
            destination=route["to"],
            depart_date=route["depart"],
            return_date=route["return"],
            flex_days=args.flex_days,
            trip_days=args.trip_days,
            trip_flex=args.trip_flex,
            origins=args.origins,
            adults=args.adults,
        )


if __name__ == "__main__":
    main()
