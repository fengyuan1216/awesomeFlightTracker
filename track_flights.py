#!/usr/bin/env python3
"""
Flight price tracker — supports two backends:
  • fast-flights  (default) — scrapes Google Flights, no API key needed
  • serpapi        — Google Flights via SerpApi (accurate live prices, API key required)

SerpApi optimisation: multiple departure airports are combined into ONE API call
per date pair (comma-separated departure_id), minimising total API calls.

Install:
  pip install fast-flights requests   # requests needed for serpapi backend

Configure (in .env or environment):
  FLIGHT_API=serpapi          # or fast-flights (default)
  SERPAPI_KEY=your_key_here

Usage examples:
  python track_flights.py --from YVR --to AKL --depart 2026-11-20 --return 2026-12-01
  python track_flights.py --from SEA YVR --to ZQN --depart 2026-11-22 --return 2026-12-05
  python track_flights.py --from SEA --to AKL --depart 2026-11-20 --one-way
  python track_flights.py --from SEA YVR PAE --to AKL --depart 2026-11-20 --return 2026-12-01 --flex-days 3
  python track_flights.py --api serpapi --from SEA YVR --to AKL --depart 2026-11-21 --trip-days 9 --flex-days 3
  python track_flights.py --history --to AKL
"""

from __future__ import annotations

import argparse
import csv
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config — override via environment / .env
# ---------------------------------------------------------------------------

ORIGIN_AIRPORTS = ["SEA", "YVR", "PAE"]

DEFAULT_ROUTES = [
    {"to": "AKL", "depart": "2026-11-20", "return": "2026-12-01"},
    {"to": "ZQN", "depart": "2026-11-20", "return": "2026-12-01"},
]

RESULTS_DIR = Path(__file__).parent / "results"
RESULTS_DIR.mkdir(exist_ok=True)

# How many departure_token (return-leg) calls to make per initial SerpApi search.
# Each token call costs 1 API credit and fills the "↩ Return" column in the report.
# The initial call already gives accurate prices + full outbound details without any token calls.
#   0 = no return details (saves all token credits — recommended for price tracking)
#   3 = return details for the 3 cheapest outbound flights (covers the pinned "Top 3" section)
SERPAPI_MAX_RETURN_CALLS = 3

# ---------------------------------------------------------------------------
# fast-flights backend
# ---------------------------------------------------------------------------

def _fetch_fastflights(flight_data, trip, adults, *, max_attempts=5, backoff=3):
    """Retry wrapper for fast-flights with quality checks (common→fallback alternation)."""
    import time
    from fast_flights import Passengers, get_flights

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
                return result
        attempt += 1
        if attempt < max_attempts:
            time.sleep(backoff * attempt)

    return last_result


def search_fastflights(
    origin: str,
    destination: str,
    depart_date: str,
    return_date: str | None = None,
    adults: int = 1,
) -> list[dict]:
    from fast_flights import FlightData

    flight_data = [FlightData(date=depart_date, from_airport=origin, to_airport=destination)]
    trip = "one-way"
    if return_date:
        flight_data.append(FlightData(date=return_date, from_airport=destination, to_airport=origin))
        trip = "round-trip"

    result = _fetch_fastflights(flight_data, trip, adults)
    if not result or not result.flights:
        return []

    # If round-trip returned all-Unknown stops, overlay with one-way outbound search
    ow_by_rank: dict[int, object] = {}
    rt_flights = result.flights
    all_unknown = all(str(getattr(f, "stops", "Unknown")) == "Unknown" for f in rt_flights)
    if all_unknown and return_date:
        from fast_flights import FlightData
        ow = _fetch_fastflights(
            [FlightData(date=depart_date, from_airport=origin, to_airport=destination)],
            "one-way", adults,
        )
        if ow and ow.flights:
            rt_sorted = sorted(rt_flights, key=lambda f: _parse_price(f.price))
            ow_sorted = sorted(ow.flights,  key=lambda f: _parse_price(f.price))
            ow_by_rank = {i: f for i, f in enumerate(ow_sorted)}
            rt_flights = rt_sorted

    # Secondary one-way search for return leg details
    ret_by_stops: dict[str, list] = {}
    ret_fallback: list = []
    if return_date:
        from fast_flights import FlightData
        ret_result = _fetch_fastflights(
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
        ow_detail = ow_by_rank.get(rank)
        detail = ow_detail if (ow_detail and str(getattr(flight, "stops", "Unknown")) == "Unknown") else flight

        out_stops = str(getattr(detail, "stops", ""))
        candidates = ret_by_stops.get(out_stops) or ret_fallback
        best_ret = min(candidates, key=lambda f: _parse_price(f.price)) if candidates else None

        rows.append({
            "checked_at": checked_at,
            "api_source": "fast-flights",
            "origin": origin,
            "destination": destination,
            "depart_date": depart_date,
            "return_date": return_date or "",
            "price": flight.price,
            "price_numeric": _parse_price(flight.price),
            "duration": getattr(detail, "duration", ""),
            "stops": getattr(detail, "stops", ""),
            "airline": getattr(detail, "name", ""),
            "departure": getattr(detail, "departure", ""),
            "arrival": getattr(detail, "arrival", ""),
            "arrival_time_ahead": getattr(detail, "arrival_time_ahead", ""),
            "ret_duration": getattr(best_ret, "duration", "") if best_ret else "",
            "ret_stops": getattr(best_ret, "stops", "") if best_ret else "",
            "ret_airline": getattr(best_ret, "name", "") if best_ret else "",
            "ret_departure": getattr(best_ret, "departure", "") if best_ret else "",
            "ret_arrival": getattr(best_ret, "arrival", "") if best_ret else "",
            "ret_arrival_time_ahead": getattr(best_ret, "arrival_time_ahead", "") if best_ret else "",
        })

    return rows


# ---------------------------------------------------------------------------
# SerpApi backend
# ---------------------------------------------------------------------------

def _minutes_to_duration(minutes: int) -> str:
    h, m = divmod(int(minutes), 60)
    return f"{h} hr {m} min" if m else f"{h} hr"


def _serpapi_fmt_time(dt_str: str, ref_date: str | None = None) -> tuple[str, str]:
    """Convert SerpApi time '2026-11-21 08:00' to display format.

    Returns:
        (display, ahead) e.g. ('8:00 AM on Sat, Nov 21', '+2')
        ahead is '' when arrival is same day as ref_date, '+N' when N days later.
    """
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M")
    # Use lstrip to avoid leading zeros on macOS/Linux
    hour = dt.strftime("%I").lstrip("0") or "12"
    minute = dt.strftime("%M")
    ampm = dt.strftime("%p")
    day_abbr = dt.strftime("%a")
    month_abbr = dt.strftime("%b")
    day_num = str(dt.day)
    display = f"{hour}:{minute} {ampm} on {day_abbr}, {month_abbr} {day_num}"

    ahead = ""
    if ref_date:
        ref = datetime.strptime(ref_date, "%Y-%m-%d").date()
        diff = dt.date() - ref
        if diff.days > 0:
            ahead = f"+{diff.days}"
    return display, ahead


def _serpapi_parse_leg(legs: list[dict], layovers: list[dict], total_duration: int,
                       ref_date: str | None = None) -> dict:
    """Extract display fields from a SerpApi flight's legs array."""
    if not legs:
        return {}
    first, last = legs[0], legs[-1]

    dep_display, _ = _serpapi_fmt_time(first["departure_airport"]["time"])
    arr_display, arr_ahead = _serpapi_fmt_time(last["arrival_airport"]["time"], ref_date)

    airlines: list[str] = []
    seen: set[str] = set()
    for leg in legs:
        name = leg.get("airline", "")
        if name and name not in seen:
            airlines.append(name)
            seen.add(name)

    return {
        "origin": first["departure_airport"]["id"],
        "departure": dep_display,
        "arrival": arr_display,
        "arrival_time_ahead": arr_ahead,
        "duration": _minutes_to_duration(total_duration),
        "stops": str(len(layovers)),
        "airline": ", ".join(airlines),
    }


def search_serpapi(
    origins: list[str],
    destination: str,
    depart_date: str,
    return_date: str | None,
    adults: int,
    api_key: str,
) -> list[dict]:
    """Search Google Flights via SerpApi.

    All origins are combined into ONE API call using comma-separated departure_id.
    Return-leg details are fetched via departure_token for the cheapest
    SERPAPI_MAX_RETURN_CALLS outbound flights.
    No retries — SerpApi returns authoritative data directly.
    """
    import requests

    base_params = {
        "engine": "google_flights",
        "departure_id": ",".join(origins),
        "arrival_id": destination,
        "outbound_date": depart_date,
        "adults": str(adults),
        "currency": "USD",
        "hl": "en",
        "no_cache": "true",
        "api_key": api_key,
        "type": "2" if not return_date else "1",
    }
    if return_date:
        base_params["return_date"] = return_date

    resp = requests.get("https://serpapi.com/search", params=base_params, timeout=30)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(data["error"])

    all_outbound = data.get("best_flights", []) + data.get("other_flights", [])
    if not all_outbound:
        return []

    # Fetch return-leg details for the cheapest N outbound flights
    return_details: dict[str, dict] = {}   # token → best return flight object
    if return_date:
        by_price = sorted(all_outbound, key=lambda f: f.get("price", 999999))
        tokens_seen: set[str] = set()
        for outbound in by_price:
            if len(tokens_seen) >= SERPAPI_MAX_RETURN_CALLS:
                break
            token = outbound.get("departure_token")
            if not token or token in tokens_seen:
                continue
            tokens_seen.add(token)
            try:
                ret_resp = requests.get(
                    "https://serpapi.com/search",
                    params={**base_params, "departure_token": token},
                    timeout=30,
                )
                ret_resp.raise_for_status()
                ret_data = ret_resp.json()
                ret_flights = ret_data.get("best_flights", []) + ret_data.get("other_flights", [])
                if ret_flights:
                    return_details[token] = min(ret_flights, key=lambda f: f.get("price", 999999))
            except Exception:
                pass

    checked_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []

    for outbound in all_outbound:
        legs = outbound.get("flights", [])
        if not legs:
            continue

        out = _serpapi_parse_leg(
            legs, outbound.get("layovers", []),
            outbound.get("total_duration", 0),
            ref_date=depart_date,
        )
        price = outbound.get("price", 0)

        # Return leg
        ret: dict = {}
        token = outbound.get("departure_token")
        if token and token in return_details:
            rf = return_details[token]
            ret = _serpapi_parse_leg(
                rf.get("flights", []),
                rf.get("layovers", []),
                rf.get("total_duration", 0),
                ref_date=return_date,
            )

        rows.append({
            "checked_at": checked_at,
            "api_source": "serpapi",
            "origin": out.get("origin", legs[0]["departure_airport"]["id"]),
            "destination": destination,
            "depart_date": depart_date,
            "return_date": return_date or "",
            "price": f"${price:,}",
            "price_numeric": float(price),
            "duration": out.get("duration", ""),
            "stops": out.get("stops", ""),
            "airline": out.get("airline", ""),
            "departure": out.get("departure", ""),
            "arrival": out.get("arrival", ""),
            "arrival_time_ahead": out.get("arrival_time_ahead", ""),
            "ret_duration": ret.get("duration", ""),
            "ret_stops": ret.get("stops", ""),
            "ret_airline": ret.get("airline", ""),
            "ret_departure": ret.get("departure", ""),
            "ret_arrival": ret.get("arrival", ""),
            "ret_arrival_time_ahead": ret.get("arrival_time_ahead", ""),
        })

    return rows


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_price(price_str: str) -> float:
    """Extract numeric value from '$1,234' or 'CA$1,234'."""
    if not price_str:
        return 0.0
    cleaned = "".join(ch for ch in str(price_str) if ch.isdigit() or ch == ".")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def date_variants(date_str: str, flex_days: int) -> list[str]:
    base = datetime.strptime(date_str, "%Y-%m-%d")
    return [(base + timedelta(days=d)).strftime("%Y-%m-%d")
            for d in range(-flex_days, flex_days + 1)]


# ---------------------------------------------------------------------------
# CSV persistence
# ---------------------------------------------------------------------------

FIELDNAMES = [
    "checked_at", "api_source", "origin", "destination", "depart_date", "return_date",
    "price", "price_numeric",
    "duration", "stops", "airline", "departure", "arrival", "arrival_time_ahead",
    "ret_duration", "ret_stops", "ret_airline", "ret_departure", "ret_arrival", "ret_arrival_time_ahead",
]


def csv_path(destination: str) -> Path:
    return RESULTS_DIR / f"{destination.upper()}.csv"


def save_results(rows: list[dict], destination: str):
    path = csv_path(destination)

    if path.exists():
        with open(path, newline="") as f:
            existing_fields = csv.DictReader(f).fieldnames or []
        if existing_fields != FIELDNAMES:
            with open(path, newline="") as f:
                old_rows = list(csv.DictReader(f))
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=FIELDNAMES, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(old_rows)
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

    sessions: dict[str, list[dict]] = {}
    for r in rows:
        sessions.setdefault(r["checked_at"][:16], []).append(r)

    for session_time, session_rows in sorted(sessions.items()):
        valid = [r for r in session_rows if r.get("price_numeric")]
        if not valid:
            continue
        cheapest = min(valid, key=lambda r: float(r["price_numeric"]))
        print(f"  {session_time}  cheapest: {cheapest['price']:>12}  "
              f"{cheapest['origin']}→{cheapest['destination']}  "
              f"depart {cheapest['depart_date']}  "
              f"{cheapest['stops']}  {cheapest['airline']}")

    valid_rows = [r for r in rows if r.get("price_numeric")]
    if valid_rows:
        prices = [float(r["price_numeric"]) for r in valid_rows]
        print(f"\n  All-time low: {min(prices):.0f}  |  Latest: {float(valid_rows[-1]['price_numeric']):.0f}")


# ---------------------------------------------------------------------------
# Date pair computation
# ---------------------------------------------------------------------------

def compute_date_pairs(
    depart_date: str,
    return_date: str | None,
    flex_days: int,
    trip_days: int | None,
    trip_flex: int,
) -> list[tuple[str, str | None]]:
    """Return all (depart, return) pairs to search."""
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


# ---------------------------------------------------------------------------
# Main search flow
# ---------------------------------------------------------------------------

def run_search(
    destination: str,
    depart_date: str,
    return_date: str | None,
    flex_days: int,
    trip_days: int | None,
    trip_flex: int,
    origins: list[str],
    adults: int,
    api_backend: str,
    serpapi_key: str | None = None,
):
    pairs = compute_date_pairs(depart_date, return_date, flex_days, trip_days, trip_flex)
    all_rows: list[dict] = []

    if api_backend == "serpapi":
        if not serpapi_key:
            raise RuntimeError("SERPAPI_KEY is not set. Add it to .env or export it.")
        # All origins combined into ONE call per date pair
        print(f"  API        : SerpApi  (origins combined — {len(pairs)} API call(s) + up to "
              f"{len(pairs) * SERPAPI_MAX_RETURN_CALLS} return-token call(s))")
        for dd, rd in pairs:
            label = (f"[{','.join(origins)}]→{destination}  {dd}" +
                     (f" / return {rd} ({(datetime.strptime(rd,'%Y-%m-%d')-datetime.strptime(dd,'%Y-%m-%d')).days}d)"
                      if rd else " (one-way)"))
            print(f"  Searching {label} …", end=" ", flush=True)
            try:
                rows = search_serpapi(origins, destination, dd, rd, adults, serpapi_key)
                _print_search_result(rows)
                all_rows.extend(rows)
            except Exception as e:
                print(f"error ({str(e).splitlines()[0][:120]})")

    else:
        # fast-flights: one call per origin × date pair
        print(f"  API        : fast-flights  "
              f"({len(pairs)} pair(s) × {len(origins)} origin(s) = {len(pairs)*len(origins)} call(s))")
        for origin in origins:
            for dd, rd in pairs:
                label = (f"{origin}→{destination}  {dd}" +
                         (f" / return {rd} ({(datetime.strptime(rd,'%Y-%m-%d')-datetime.strptime(dd,'%Y-%m-%d')).days}d)"
                          if rd else " (one-way)"))
                print(f"  Searching {label} …", end=" ", flush=True)
                try:
                    rows = search_fastflights(origin, destination, dd, rd, adults)
                    _print_search_result(rows)
                    all_rows.extend(rows)
                except Exception as e:
                    print(f"no routes ({str(e).splitlines()[0][:120]})")

    if all_rows:
        save_results(all_rows, destination)
        valid = [r for r in all_rows if r["price_numeric"]]
        if valid:
            best = min(valid, key=lambda r: r["price_numeric"])
            print(f"\n  *** Best for {destination}: {best['price']}  "
                  f"{best['origin']} on {best['depart_date']}  "
                  f"({best['stops']})  {best['airline']} ***\n")
    else:
        print(f"  No results found for {destination}.\n")


def _print_search_result(rows: list[dict]):
    if not rows:
        print("no results")
        return
    valid = [r for r in rows if r["price_numeric"]]
    good  = [r for r in rows if str(r.get("stops", "")) not in ("Unknown", "")]
    if valid:
        cheapest = min(valid, key=lambda r: r["price_numeric"])
        quality  = "" if good else "  ⚠ stops/times missing"
        print(f"{len(rows)} flights, cheapest: {cheapest['price']}  "
              f"({cheapest['stops']} stop{'s' if cheapest['stops'] != '1' else ''})  "
              f"{cheapest['airline']}{quality}")
    else:
        print(f"{len(rows)} flights (prices unparsed)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Track Google Flights prices to any destination.",
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
                   help=argparse.SUPPRESS)  # legacy alias
    p.add_argument("--adults", type=int, default=1, help="Number of adult passengers (default: 1)")
    p.add_argument("--api", dest="api_backend",
                   choices=["fast-flights", "serpapi"],
                   default=os.environ.get("FLIGHT_API", "fast-flights"),
                   help="Flight data backend (default: $FLIGHT_API or fast-flights)")
    p.add_argument("--history", action="store_true",
                   help="Show saved price history (use --to to filter by destination)")
    return p


def main():
    # Load .env if present
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, _, v = line.partition("=")
                    v = v.strip().strip('"').strip("'")
                    os.environ.setdefault(k.strip(), v)

    parser = build_parser()
    args = parser.parse_args()

    serpapi_key = os.environ.get("SERPAPI_KEY")

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
    print(f"Backend    : {args.api_backend}")
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
            api_backend=args.api_backend,
            serpapi_key=serpapi_key,
        )


if __name__ == "__main__":
    main()
