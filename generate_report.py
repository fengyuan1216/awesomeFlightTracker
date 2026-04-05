#!/usr/bin/env python3
"""
Generate an HTML flight price report from saved CSV results.

Usage:
  python generate_report.py                   # all CSVs in results/
  python generate_report.py --dest AKL
  python generate_report.py --dest AKL ZQN
  python generate_report.py --dest AKL --out report.html
"""

import argparse
import csv
import re
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

import base64
from fast_flights import FlightData, Passengers, create_filter

RESULTS_DIR = Path(__file__).parent / "results"
TOP_N = 3        # pinned rows per sub-section (cheapest / shortest)
MAX_FLIGHTS = 10  # max total rows shown per stop category (pinned + collapsible)

# ---------------------------------------------------------------------------
# Proto helpers – inject carrier codes and max_stops into a TFS binary
# ---------------------------------------------------------------------------

def _write_varint(v: int) -> bytes:
    r = b""
    while True:
        b = v & 0x7F; v >>= 7
        if v: b |= 0x80
        r += bytes([b])
        if not v: break
    return r

def _read_varint(data: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = data[pos]; pos += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80): break
        shift += 7
    return result, pos

def _inject_carriers(proto_bytes: bytes, codes: list[str]) -> bytes:
    """Append carrier-code strings (field 18) into each FlightData sub-message."""
    if not codes:
        return proto_bytes
    extra = b""
    for c in codes:
        cb = c.encode()
        extra += _write_varint((18 << 3) | 2) + _write_varint(len(cb)) + cb

    result = b""; pos = 0
    while pos < len(proto_bytes):
        tag, pos = _read_varint(proto_bytes, pos)
        fn, wt = tag >> 3, tag & 7
        if fn == 3 and wt == 2:          # field 3 = data (FlightData sub-message)
            ln, pos = _read_varint(proto_bytes, pos)
            sub = proto_bytes[pos:pos + ln]; pos += ln
            new_sub = sub + extra
            result += _write_varint(tag) + _write_varint(len(new_sub)) + new_sub
        elif wt == 0:
            v, pos = _read_varint(proto_bytes, pos)
            result += _write_varint(tag) + _write_varint(v)
        elif wt == 2:
            ln, pos = _read_varint(proto_bytes, pos)
            result += _write_varint(tag) + _write_varint(ln) + proto_bytes[pos:pos + ln]; pos += ln
        elif wt == 5:
            result += _write_varint(tag) + proto_bytes[pos:pos + 4]; pos += 4
        elif wt == 1:
            result += _write_varint(tag) + proto_bytes[pos:pos + 8]; pos += 8
    return result

# ---------------------------------------------------------------------------
# Airline name → IATA code mapping  (for logo lookup)
# ---------------------------------------------------------------------------

AIRLINE_CODES: dict[str, str] = {
    "air new zealand":      "NZ",
    "alaska":               "AS",
    "alaska airlines":      "AS",
    "american":             "AA",
    "american airlines":    "AA",
    "ana":                  "NH",
    "air canada":           "AC",
    "air china":            "CA",
    "air france":           "AF",
    "air tahiti nui":       "TN",
    "british airways":      "BA",
    "cathay pacific":       "CX",
    "china airlines":       "CI",
    "china eastern":        "MU",
    "china southern":       "CZ",
    "delta":                "DL",
    "emirates":             "EK",
    "eva air":              "BR",
    "fiji airways":         "FJ",
    "finnair":              "AY",
    "hawaiian":             "HA",
    "hawaiian airlines":    "HA",
    "japan airlines":       "JL",
    "jal":                  "JL",
    "jetblue":              "B6",
    "jetstar":              "JQ",
    "korean air":           "KE",
    "lufthansa":            "LH",
    "philippine airlines":  "PR",
    "qantas":               "QF",
    "qatar airways":        "QR",
    "singapore airlines":   "SQ",
    "southwest":            "WN",
    "spirit":               "NK",
    "starlux":              "JX",
    "thai airways":         "TG",
    "turkish airlines":     "TK",
    "united":               "UA",
    "united airlines":      "UA",
    "vietnam airlines":     "VN",
    "westjet":              "WS",
    "xiamen air":           "MF",
}

LOGO_BASE = "https://www.gstatic.com/flights/airline_logos/70px"


def airline_logo_url(iata: str) -> str:
    return f"{LOGO_BASE}/{iata}.png"


def parse_airlines(airline_str: str) -> list[tuple[str, str]]:
    """'Alaska, Fiji Airways' → [('Alaska', 'AS'), ('Fiji Airways', 'FJ')]"""
    results = []
    for name in re.split(r"\s*[,/]\s*", airline_str):
        name = name.strip()
        code = AIRLINE_CODES.get(name.lower(), "")
        results.append((name, code))
    return results


def airline_logos_html(airline_str: str) -> str:
    """Return stacked logo+name chips for an airline string."""
    if not airline_str or airline_str == "—":
        return '<span class="no-airline">—</span>'
    chips = []
    for name, code in parse_airlines(airline_str):
        if code:
            logo = f'<img src="{airline_logo_url(code)}" alt="{name}" class="al-logo" onerror="this.style.display=\'none\'">'
        else:
            logo = '<span class="al-logo al-placeholder"></span>'
        chips.append(
            f'<span class="al-chip">{logo}<span class="al-name">{name}</span></span>'
        )
    return '<div class="al-group">' + "".join(chips) + "</div>"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def duration_to_minutes(duration: str) -> int | None:
    if not duration:
        return None
    hours = re.search(r"(\d+)\s*hr", duration)
    mins  = re.search(r"(\d+)\s*min", duration)
    total = 0
    if hours:
        total += int(hours.group(1)) * 60
    if mins:
        total += int(mins.group(1))
    return total if total else None


def load_csv(path: Path) -> list[dict]:
    with open(path, newline="") as f:
        rows = list(csv.DictReader(f))
    # Require a price; accept Unknown stops (partial data) rather than dropping entirely
    rows = [r for r in rows if r.get("price_numeric") and float(r["price_numeric"]) > 0]
    for r in rows:
        r["price_numeric"] = float(r["price_numeric"])
        s = r.get("stops", "")
        r["stops_int"]    = int(s) if s.isdigit() else 99   # 99 = Unknown/partial
        r["duration_min"] = duration_to_minutes(r.get("duration", ""))

    # Deduplicate by: origin, destination, departure time-of-day, arrival time-of-day, duration.
    # Time-of-day only (strips the date) so the same daily flight schedule found across
    # multiple flex-date searches is collapsed into one entry (cheapest price kept).
    def _time_of_day(dt_str: str) -> str:
        """'6:35 PM on Mon, Nov 23' → '6:35 PM'"""
        return dt_str.split(" on ")[0].strip() if " on " in dt_str else dt_str.strip()

    buckets: dict[tuple, dict] = {}
    for r in rows:
        key = (
            r["origin"],
            r["destination"],
            _time_of_day(r.get("departure", "")),
            _time_of_day(r.get("arrival", "")),
            r.get("duration", ""),
        )
        if key not in buckets:
            buckets[key] = r
        else:
            existing = buckets[key]
            # Prefer whichever has more filled fields; break ties by lower price
            filled = lambda x: sum(bool(x.get(f)) for f in ("airline", "departure", "arrival", "duration", "stops"))
            if filled(r) > filled(existing) or (
                filled(r) == filled(existing) and r["price_numeric"] < existing["price_numeric"]
            ):
                buckets[key] = r
    return list(buckets.values())


def stop_label(n: int) -> str:
    return {0: "Nonstop", 1: "1 Stop", 99: "Unknown Stops (partial data)"}.get(n, f"{n} Stops")


def make_google_flights_url(origin: str, dest: str, depart_date: str,
                             return_date: str = "", adults: int = 1,
                             max_stops: int | None = None,
                             airline_codes: list[str] | None = None) -> str:
    fd = [FlightData(date=depart_date, from_airport=origin, to_airport=dest)]
    trip = "one-way"
    if return_date:
        fd.append(FlightData(date=return_date, from_airport=dest, to_airport=origin))
        trip = "round-trip"
    filter_obj = create_filter(
        flight_data=fd, trip=trip, seat="economy",
        passengers=Passengers(adults=adults),
        max_stops=max_stops,
    )
    proto_bytes = filter_obj.to_string()
    if airline_codes:
        proto_bytes = _inject_carriers(proto_bytes, airline_codes)
    tfs = urllib.parse.quote(base64.b64encode(proto_bytes).decode())
    return f"https://www.google.com/travel/flights?tfs={tfs}&hl=en&curr=USD"


# ---------------------------------------------------------------------------
# Row rendering
# ---------------------------------------------------------------------------

BADGE_CHEAP   = '<span class="badge badge-cheap">Cheapest</span>'
BADGE_SHORT   = '<span class="badge badge-short">Shortest</span>'
BADGE_BEST    = '<span class="badge badge-best">Best</span>'


def split_time_date(dt_str: str) -> tuple[str, str]:
    """'4:20 PM on Fri, Nov 20' → ('4:20 PM', 'Fri, Nov 20')"""
    if " on " in dt_str:
        time_part, date_part = dt_str.split(" on ", 1)
        return time_part.strip(), date_part.strip()
    return dt_str, ""


def fmt_datetime(dt_str: str, variant: str, ahead: str = "") -> str:
    """Render a styled departure/arrival pill.
    variant: 'out-dep' | 'out-arr' | 'ret-dep' | 'ret-arr'
    Falls back to a plain date string when no time is available.
    """
    empty = not dt_str or dt_str == "—"
    if empty:
        return f'<span class="dt-wrap {variant} empty">—</span>'
    time_part, date_part = split_time_date(dt_str)
    ahead_html = f'<span class="ahead">+{ahead.lstrip("+")}</span>' if ahead else ""
    date_html  = f'<div class="dt-date">{date_part}</div>' if date_part else ""
    return (
        f'<span class="dt-wrap {variant}">'
        f'  <span class="dt-time">{time_part}{ahead_html}</span>'
        f'  {date_html}'
        f'</span>'
    )


def fmt_date_only(date_str: str, variant: str) -> str:
    """Render a date-only pill (when we only have the return date, no times)."""
    return (
        f'<span class="dt-wrap {variant}">'
        f'  <span class="dt-time">{date_str}</span>'
        f'</span>'
    )


def render_leg(airline: str, departure: str, arrival: str,
               arrival_time_ahead: str, duration: str, stops,
               is_return: bool = False) -> str:
    s = str(stops)
    if s == "0":
        stops_str = '<span class="stops-nonstop">Nonstop</span>'
    elif s == "1":
        stops_str = '<span class="stops-one">1 stop</span>'
    elif s.isdigit():
        stops_str = f'<span class="stops-many">{s} stops</span>'
    else:
        stops_str = f'<span class="stops-unknown">{s}</span>'

    prefix = "ret" if is_return else "out"
    dep_html = fmt_datetime(departure or "—", f"{prefix}-dep")
    arr_html = fmt_datetime(arrival   or "—", f"{prefix}-arr", arrival_time_ahead)

    return (
        f'<div class="leg-airline">{airline_logos_html(airline)}</div>'
        f'<div class="leg-times">'
        f'  {dep_html}'
        f'  <span class="leg-arrow">→</span>'
        f'  {arr_html}'
        f'</div>'
        f'<div class="leg-meta">{duration or "—"} &nbsp;·&nbsp; {stops_str}</div>'
    )


def render_row(r: dict, rank: int, badges: str, gf_url: str) -> str:
    has_return = bool(r.get("return_date"))

    outbound_html = render_leg(
        r.get("airline", ""), r.get("departure", ""), r.get("arrival", ""),
        r.get("arrival_time_ahead", ""), r.get("duration", ""), r.get("stops", ""),
        is_return=False,
    )
    return_html = ""
    if has_return:
        if r.get("ret_airline") or r.get("ret_departure"):
            return_html = render_leg(
                r.get("ret_airline", ""), r.get("ret_departure", ""), r.get("ret_arrival", ""),
                r.get("ret_arrival_time_ahead", ""), r.get("ret_duration", ""), r.get("ret_stops", ""),
                is_return=True,
            )
        else:
            # Only return date is known — show styled date pills as placeholders
            return_html = (
                f'<div class="leg-airline muted" style="font-size:11px;margin-bottom:4px">Return date</div>'
                f'<div class="leg-times">'
                f'  {fmt_date_only(r["return_date"], "ret-dep")}'
                f'  <span class="leg-arrow">→</span>'
                f'  {fmt_date_only("?", "ret-arr")}'
                f'</div>'
                f'<div class="leg-meta muted" style="font-size:10px">Re-run tracker for full details</div>'
            )

    return_cell = f'<div class="leg-block">{return_html}</div>' if has_return else '<div class="muted">—</div>'

    row_class = "row-cheap" if "Cheapest" in badges else ("row-short" if "Shortest" in badges else "")
    if "Cheapest" in badges and "Shortest" in badges:
        row_class = "row-best"

    checked_at = r.get("checked_at", "")

    return f"""
      <tr class="{row_class}">
        <td class="td-rank">{rank}</td>
        <td class="td-origin"><strong>{r['origin']}</strong></td>
        <td class="td-price">
          <span class="price-val">{r['price']}</span>
          <div class="badges">{badges}</div>
          <div class="price-ts">as of {checked_at}</div>
        </td>
        <td class="td-leg"><div class="leg-block">{outbound_html}</div></td>
        <td class="td-leg">{return_cell}</td>
        <td class="td-link">
          <a href="{gf_url}" target="_blank" class="gf-link">Live price ↗</a>
          <div class="gf-note">Opens Google Flights<br>with current pricing</div>
        </td>
      </tr>"""


# ---------------------------------------------------------------------------
# Section builder
# ---------------------------------------------------------------------------

def build_section(stop_n: int, rows: list[dict]) -> str:
    by_price    = sorted(rows, key=lambda r: r["price_numeric"])
    rows_w_dur  = [r for r in rows if r["duration_min"]]
    by_duration = sorted(rows_w_dur, key=lambda r: r["duration_min"])

    top_cheap   = by_price[:TOP_N]
    top_short   = by_duration[:TOP_N]

    # IDs of rows to show in the pinned sections
    cheap_ids = {id(r) for r in top_cheap}
    short_ids = {id(r) for r in top_short}
    pinned_ids = cheap_ids | short_ids

    # Remaining rows in price order, capped so total displayed ≤ MAX_FLIGHTS
    rest_all = [r for r in by_price if id(r) not in pinned_ids]
    pinned_count = len(cheap_ids | short_ids)
    rest_limit = max(0, MAX_FLIGHTS - pinned_count)
    rest = rest_all[:rest_limit]
    rest_hidden = len(rest_all) - len(rest)  # rows trimmed beyond MAX_FLIGHTS

    label = stop_label(stop_n)

    # Summary bar
    cheapest = by_price[0]
    shortest = by_duration[0] if by_duration else None

    def summary_item(lbl, val, sub, color=""):
        return (f'<div class="s-item">'
                f'<span class="s-label">{lbl}</span>'
                f'<span class="s-value" style="color:{color}">{val}</span>'
                f'<span class="s-sub">{sub}</span>'
                f'</div>')

    summary_items = [
        summary_item("Cheapest", cheapest["price"], f"{cheapest['origin']} · {cheapest.get('airline','')}", "#16a34a"),
    ]
    if shortest:
        summary_items.append(
            summary_item("Shortest", shortest.get("duration","—"),
                         f"{shortest['origin']} · {shortest['price']}", "#2563eb")
        )
    summary_items.append(
        summary_item("Total", str(len(rows)), f"from {len(set(r['origin'] for r in rows))} airport(s)")
    )

    # Table header
    thead = """
      <table>
        <colgroup>
          <col style="width:32px">
          <col style="width:52px">
          <col style="width:115px">
          <col>
          <col>
          <col style="width:130px">
        </colgroup>
        <thead>
          <tr>
            <th>#</th><th>From</th><th>Tracked Price</th>
            <th>✈&nbsp;Outbound</th><th>↩&nbsp;Return</th>
            <th>Live Price</th>
          </tr>
        </thead>
        <tbody>"""

    def render_subsection(title: str, sub_rows: list[dict],
                          highlight_cheap: set, highlight_short: set) -> str:
        if not sub_rows:
            return ""
        html = f'<tr class="sub-header"><td colspan="7">{title}</td></tr>'
        for i, r in enumerate(sub_rows, 1):
            stops_int = r.get("stops_int")
            max_stops = stops_int if isinstance(stops_int, int) and stops_int < 99 else None
            codes = [code for _, code in parse_airlines(r.get("airline", "")) if code]
            gf_url = make_google_flights_url(
                r["origin"], r["destination"], r["depart_date"], r.get("return_date", ""),
                max_stops=max_stops, airline_codes=codes or None,
            )
            both  = id(r) in highlight_cheap and id(r) in highlight_short
            cheap = id(r) in highlight_cheap
            short = id(r) in highlight_short
            if both:
                badges = BADGE_BEST
            elif cheap:
                badges = BADGE_CHEAP
            elif short:
                badges = BADGE_SHORT
            else:
                badges = ""
            html += render_row(r, i, badges, gf_url)
        return html

    # Build visible rows
    visible_html  = render_subsection("Top 3 Cheapest", top_cheap,  cheap_ids, short_ids)
    # Remove from top_short any already shown in top_cheap
    extra_short = [r for r in top_short if id(r) not in cheap_ids]
    if extra_short:
        visible_html += render_subsection("Top 3 Shortest", extra_short, cheap_ids, short_ids)

    rest_html = ""
    if rest:
        rest_rows_html = render_subsection(f"All other options ({len(rest)})", rest, set(), set())
        truncation_note = ""
        if rest_hidden:
            truncation_note = (f'<tr><td colspan="7" style="text-align:center;padding:6px 8px;'
                               f'font-size:11px;color:#64748b">'
                               f'+{rest_hidden} more flight{"s" if rest_hidden!=1 else ""} not shown '
                               f'— run tracker to refresh</td></tr>')
        rest_html = f"""
        <tr class="collapse-row">
          <td colspan="7" style="padding:0">
            <details>
              <summary>Show {len(rest)} more flight{'s' if len(rest)!=1 else ''}</summary>
              <table style="width:100%; border-collapse:collapse">
                <colgroup>
                  <col style="width:32px"><col style="width:52px"><col style="width:115px">
                  <col><col><col style="width:130px">
                </colgroup>
                <tbody>{rest_rows_html}{truncation_note}</tbody>
              </table>
            </details>
          </td>
        </tr>"""

    return f"""
  <section class="stop-section">
    <div class="section-header">
      <h2>{label}</h2>
      <div class="summary-bar">{"".join(summary_items)}</div>
    </div>
    {thead}{visible_html}{rest_html}
        </tbody>
      </table>
  </section>"""


# ---------------------------------------------------------------------------
# Full HTML page
# ---------------------------------------------------------------------------

def generate_html(destination: str, rows: list[dict]) -> str:
    by_stops: dict[int, list[dict]] = {}
    for r in rows:
        by_stops.setdefault(r["stops_int"], []).append(r)

    sections_html = "".join(build_section(n, by_stops[n]) for n in sorted(by_stops))

    overall_best    = min(rows, key=lambda r: r["price_numeric"])
    origins         = sorted(set(r["origin"] for r in rows))
    depart_dates    = sorted(set(r["depart_date"] for r in rows))
    return_dates    = sorted(set(r["return_date"] for r in rows if r.get("return_date")))
    generated_at    = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    is_roundtrip    = bool(return_dates)
    trip_type       = "Round Trip" if is_roundtrip else "One Way"

    # Staleness: find the most recent checked_at across all rows
    timestamps = [r["checked_at"] for r in rows if r.get("checked_at")]
    latest_check = max(timestamps) if timestamps else ""
    try:
        latest_dt = datetime.strptime(latest_check, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        age_hours = (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600
    except ValueError:
        age_hours = 0

    if age_hours >= 24:
        age_str = f"{int(age_hours // 24)}d {int(age_hours % 24)}h ago"
        stale_color = "#7f1d1d"; stale_bg = "#fef2f2"; stale_border = "#fca5a5"
    elif age_hours >= 6:
        age_str = f"{int(age_hours)}h ago"
        stale_color = "#78350f"; stale_bg = "#fffbeb"; stale_border = "#fcd34d"
    else:
        age_str = ""

    staleness_banner = ""
    if age_str:
        staleness_banner = f"""
    <div class="stale-banner" style="background:{stale_bg};border-color:{stale_border};color:{stale_color}">
      ⚠ Prices last captured <strong>{age_str}</strong> ({latest_check}).
      Flight prices change frequently — click <strong>Live Price ↗</strong> on any row to see the current fare on Google Flights,
      or re-run <code>python track_flights.py</code> to refresh.
    </div>"""

    return_meta = (
        f'<div class="meta-item"><span class="label">Return</span>'
        f'<span class="value">{", ".join(return_dates)}</span></div>'
        if return_dates else ""
    )

    best_stops = overall_best.get("stops_int")
    best_max_stops = best_stops if isinstance(best_stops, int) and best_stops < 99 else None
    best_codes = [c for _, c in parse_airlines(overall_best.get("airline", "")) if c]
    best_gf_url = make_google_flights_url(
        overall_best["origin"], overall_best["destination"],
        overall_best["depart_date"], overall_best.get("return_date", ""),
        max_stops=best_max_stops, airline_codes=best_codes or None,
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Flight Report — {destination}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=DM+Mono:wght@400;500&family=DM+Sans:wght@500;600&display=swap" rel="stylesheet">
<style>
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f1f5f9; color: #1e293b; padding: 24px; font-size: 13px;
}}

/* ── Page header ── */
.page-header {{
  background: linear-gradient(135deg, #0f172a 0%, #1e3a5f 100%);
  color: white; border-radius: 12px; padding: 28px 32px; margin-bottom: 20px;
}}
.page-header h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 14px; }}
.meta {{ display: flex; flex-wrap: wrap; gap: 24px; }}
.meta-item {{ display: flex; flex-direction: column; gap: 3px; }}
.meta-item .label {{ font-size: 10px; opacity:.6; text-transform: uppercase; letter-spacing:.06em; }}
.meta-item .value {{ font-size: 14px; font-weight: 600; }}

/* ── Staleness banner ── */
.stale-banner {{
  border: 1px solid; border-radius: 8px; padding: 12px 18px;
  margin-bottom: 16px; font-size: 13px; line-height: 1.6;
}}
.stale-banner code {{ background: rgba(0,0,0,.07); padding: 1px 5px; border-radius: 4px; font-size: 12px; }}

/* ── Best banner ── */
.best-banner {{
  background: #dcfce7; border: 1px solid #86efac; border-radius: 8px;
  padding: 14px 20px; margin-bottom: 20px; display: flex;
  align-items: center; gap: 16px; flex-wrap: wrap;
}}
.best-banner .bp {{
  font-size: 26px; font-weight: 800; color: #16a34a;
  white-space: nowrap; flex-shrink: 0;
}}
.best-banner .bd {{ color: #166534; font-size: 13px; line-height: 1.7; min-width: 0; }}
.best-banner a {{ color: #15803d; font-weight: 600; }}
@media (max-width: 480px) {{
  .best-banner .bp {{ font-size: 22px; }}
}}

/* ── Price cell ── */
.price-ts {{ font-size: 10px; color: #94a3b8; margin-top: 3px; }}
.gf-note {{ font-size: 10px; color: #94a3b8; margin-top: 4px; line-height: 1.4; }}

/* ── Section ── */
.stop-section {{
  background: white; border-radius: 12px;
  box-shadow: 0 1px 4px rgba(0,0,0,.08); margin-bottom: 24px; overflow: hidden;
}}
.section-header {{
  padding: 16px 20px 14px; border-bottom: 1px solid #e2e8f0; background: #f8fafc;
}}
.section-header h2 {{ font-size: 16px; font-weight: 700; margin-bottom: 10px; }}
.summary-bar {{ display: flex; gap: 28px; flex-wrap: wrap; }}
.s-item {{ display: flex; flex-direction: column; gap: 2px; }}
.s-label {{ font-size: 10px; color: #64748b; text-transform: uppercase; letter-spacing:.04em; }}
.s-value {{ font-size: 17px; font-weight: 700; }}
.s-sub {{ font-size: 11px; color: #64748b; }}

/* ── Table ── */
table {{ width: 100%; border-collapse: collapse; }}
thead th {{
  background: #f1f5f9; padding: 9px 12px; text-align: left;
  font-size: 10px; text-transform: uppercase; letter-spacing:.05em;
  color: #64748b; border-bottom: 1px solid #e2e8f0; white-space: nowrap;
}}
tbody tr {{ border-bottom: 1px solid #f1f5f9; }}
tbody tr:last-child {{ border-bottom: none; }}
tbody tr:hover {{ background: #f8fafc; }}
td {{ padding: 10px 12px; vertical-align: top; }}

.sub-header td {{
  background: #f1f5f9; color: #475569; font-size: 11px; font-weight: 600;
  text-transform: uppercase; letter-spacing: .05em; padding: 6px 12px;
  border-top: 1px solid #e2e8f0; border-bottom: 1px solid #e2e8f0;
}}
.td-rank {{ color: #94a3b8; font-size: 12px; text-align: center; padding-top: 12px; }}
.td-origin {{ font-weight: 700; padding-top: 12px; }}
.td-price {{ white-space: nowrap; }}
.price-val {{ font-size: 15px; font-weight: 800; display: block; margin-bottom: 4px; }}
.badges {{ display: flex; flex-wrap: wrap; gap: 3px; }}
.badge {{
  font-size: 9px; font-weight: 700; color: white; padding: 2px 6px;
  border-radius: 99px; letter-spacing: .03em; white-space: nowrap;
}}
.badge-cheap {{ background: #16a34a; }}
.badge-short {{ background: #2563eb; }}
.badge-best  {{ background: #d97706; }}
.td-link {{ padding-top: 11px; }}
.gf-link {{
  display: inline-block; background: #1a73e8; color: white; font-size: 11px;
  font-weight: 600; padding: 5px 10px; border-radius: 6px; text-decoration: none;
  white-space: nowrap;
}}
.gf-link:hover {{ background: #1558b0; }}
.td-meta {{ font-size: 11px; color: #94a3b8; padding-top: 12px; white-space: nowrap; }}

/* ── Airline logos ── */
.al-group {{ display: flex; flex-direction: column; gap: 4px; margin-bottom: 4px; }}
.al-chip {{ display: flex; align-items: center; gap: 6px; }}
.al-logo {{
  width: 24px; height: 24px; border-radius: 4px;
  object-fit: contain; background: #fff;
  border: 1px solid #e2e8f0; flex-shrink: 0;
}}
.al-placeholder {{
  display: inline-block; width: 24px; height: 24px;
  border-radius: 4px; background: #e2e8f0; flex-shrink: 0;
}}
.al-name {{ font-size: 12px; font-weight: 600; color: #1e293b; }}
.no-airline {{ color: #94a3b8; }}

/* ── Leg details ── */
.leg-block {{ line-height: 1.4; }}
.leg-times {{ display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-top: 4px; }}
.leg-arrow {{ color: #cbd5e1; font-size: 16px; flex-shrink: 0; }}
/* shared pill */
.dt-wrap {{
  display: inline-flex; flex-direction: column;
  background: #f8fafc; border: 1px solid #e2e8f0;
  border-radius: 6px; padding: 4px 8px; min-width: 76px;
}}
.dt-wrap.empty {{ background: none; border: 1px dashed #e2e8f0; color: #94a3b8; }}
/* outbound: blue dep, green arr */
.dt-wrap.out-dep {{ border-left: 3px solid #2563eb; background: #eff6ff; }}
.dt-wrap.out-arr {{ border-left: 3px solid #16a34a; background: #f0fdf4; }}
/* return: amber dep, violet arr */
.dt-wrap.ret-dep {{ border-left: 3px solid #d97706; background: #fffbeb; }}
.dt-wrap.ret-arr {{ border-left: 3px solid #7c3aed; background: #f5f3ff; }}
.dt-time {{
  font-family: 'DM Mono', 'SF Mono', 'Fira Code', monospace;
  font-size: 13px; font-weight: 500; color: #0f172a;
  white-space: nowrap; letter-spacing: 0.01em;
  font-variant-numeric: tabular-nums;
}}
.dt-date {{
  font-family: 'DM Sans', -apple-system, sans-serif;
  font-size: 11px; font-weight: 600; color: #374151;
  white-space: nowrap; margin-top: 2px; letter-spacing: 0.01em;
}}
.ahead {{ font-size: 10px; color: #dc2626; font-weight: 700; margin-left: 3px; }}
.leg-meta {{ font-size: 11px; color: #64748b; margin-top: 5px; display: flex; align-items: center; gap: 6px; }}
.stops-nonstop {{ color: #16a34a; font-weight: 700; }}
.stops-one {{ color: #d97706; font-weight: 600; }}
.stops-many {{ color: #dc2626; font-weight: 600; }}
.stops-unknown {{ color: #94a3b8; }}

/* ── Row highlights ── */
.row-cheap {{ background: #f0fdf4 !important; }}
.row-short {{ background: #eff6ff !important; }}
.row-best  {{ background: #fefce8 !important; }}

/* ── Collapse ── */
.collapse-row td {{ padding: 0 !important; }}
details summary {{
  cursor: pointer; padding: 10px 16px; font-size: 12px; font-weight: 600;
  color: #2563eb; background: #f8fafc; border-top: 1px solid #e2e8f0;
  list-style: none; user-select: none;
}}
details summary::before {{ content: "▶  "; font-size: 9px; }}
details[open] summary::before {{ content: "▼  "; }}
details summary:hover {{ background: #eff6ff; }}
details[open] > table tbody tr:last-child {{ border-bottom: 1px solid #f1f5f9; }}

.muted {{ color: #94a3b8; }}
.footer {{ text-align: center; color: #94a3b8; font-size: 11px; margin-top: 16px; }}
</style>
</head>
<body>

<div class="page-header">
  <h1>✈ Flight Report — {destination} &nbsp;<small style="font-weight:400;font-size:14px;opacity:.7">{trip_type}</small></h1>
  <div class="meta">
    <div class="meta-item"><span class="label">Origins</span><span class="value">{" · ".join(origins)}</span></div>
    <div class="meta-item"><span class="label">Depart</span><span class="value">{", ".join(depart_dates)}</span></div>
    {return_meta}
    <div class="meta-item"><span class="label">Flights tracked</span><span class="value">{len(rows)}</span></div>
    <div class="meta-item"><span class="label">Generated</span><span class="value">{generated_at}</span></div>
  </div>
</div>

{staleness_banner}

<div class="best-banner">
  <div class="bp">{overall_best['price']}</div>
  <div class="bd">
    <strong>Overall best price</strong><br>
    {overall_best['origin']} &rarr; {destination} &nbsp;·&nbsp;
    Departs {overall_best.get('departure') or overall_best['depart_date']} &nbsp;·&nbsp;
    {stop_label(overall_best['stops_int'])} &nbsp;·&nbsp;
    {overall_best.get('airline','—')} &nbsp;·&nbsp;
    {overall_best.get('duration','—')}<br>
    <a href="{best_gf_url}" target="_blank">View on Google Flights ↗</a>
  </div>
</div>

{sections_html}

<div class="footer">Generated by flight tracker &nbsp;·&nbsp; {generated_at}</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _load_env() -> dict[str, str]:
    env: dict[str, str] = {}
    env_file = Path(__file__).parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip()
    # os.environ takes precedence
    import os
    for key in ("EMAIL_SENDER", "EMAIL_RECEIVER", "EMAIL_PASSWORD"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def send_email(subject: str, html_body: str):
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText as _MIMEText

    env = _load_env()
    sender   = env.get("EMAIL_SENDER", "")
    receiver = env.get("EMAIL_RECEIVER", "")
    password = env.get("EMAIL_PASSWORD", "")

    if not all([sender, receiver, password]):
        print("  Email skipped: EMAIL_SENDER / EMAIL_RECEIVER / EMAIL_PASSWORD not set in .env")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = sender
    msg["To"]      = receiver
    msg.attach(_MIMEText(html_body, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.send_message(msg)
    print(f"  Email sent → {receiver}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate HTML flight report from saved CSVs.")
    parser.add_argument("--dest", nargs="+", metavar="IATA",
                        help="Destination(s) (default: all CSVs in results/)")
    parser.add_argument("--out", metavar="PATH", help="Output file (default: results/<DEST>_report.html)")
    parser.add_argument("--email", action="store_true",
                        help="Send the report(s) by email after generating")
    args = parser.parse_args()

    csv_files: list[Path] = []
    if args.dest:
        for d in args.dest:
            p = RESULTS_DIR / f"{d.upper()}.csv"
            if not p.exists():
                print(f"No CSV found for {d.upper()} at {p}")
            else:
                csv_files.append(p)
    else:
        csv_files = sorted(RESULTS_DIR.glob("*.csv"))

    if not csv_files:
        print("No CSV files found. Run track_flights.py first.")
        return

    for csv_file in csv_files:
        dest = csv_file.stem.upper()
        rows = load_csv(csv_file)
        if not rows:
            print(f"No usable rows in {csv_file.name}.")
            continue

        html = generate_html(dest, rows)
        out_path = Path(args.out) if args.out else RESULTS_DIR / f"{dest}_report.html"
        out_path.write_text(html, encoding="utf-8")
        print(f"Report written → {out_path}")

        if args.email:
            best = min(rows, key=lambda r: r["price_numeric"])
            subject = (
                f"✈ Flight Alert {dest}: best {best['price']} "
                f"({best['origin']}→{dest}, {best['depart_date']})"
            )
            send_email(subject, html)


if __name__ == "__main__":
    main()
