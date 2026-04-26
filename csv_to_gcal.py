#!/usr/bin/env python3
"""
csv_to_gcal.py — Extract shifts for one person from a Wochenplan CSV
and export them as a Google Calendar-importable .ics file.

Usage:
    python3 csv_to_gcal.py --csv KW18.csv --name "Name"
    python3 csv_to_gcal.py --csv KW18.csv --name "Name" --out kalender.ics
"""

import argparse
import csv
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path


# ---------------------------------------------------------------------------
# Bezeichnung → calendar event title
# None = skip (no entry created)
# ---------------------------------------------------------------------------

def classify(bezeichnung: str) -> str | None:
    b = bezeichnung.strip()

    # Skip regular weekday shifts
    if "Tagdienst Mo-Fr" in b:
        return None
    if "Tagdienst UA/SpAz" in b:
        return None

    # Skip EIR (external/virtual)
    if "EIR" in b:
        return None

    # Weekend work
    if "Tagdienst Sa/So" in b:
        return "WOCHENENDDIENST"
    if "Pikett_24h_Sa/So" in b:
        return "WOCHENENDDIENST"
    if "Pikett_Vormittag_Sa/So" in b:
        return "WOCHENENDDIENST"

    # Night shifts
    if "Nachtdienst" in b:
        return "NACHTDIENST"
    if "Pikett_Nacht" in b:
        return "NACHTDIENST"

    # Late shift
    if "Spätdienst" in b or "Spatdienst" in b:
        return "SPÄTDIENST"

    # Education / training
    if b.startswith("Fort- und Weiterbildung"):
        return "FORTBILDUNG"

    # All other absences → FREI
    ABSENZ_TYPES = {
        "Frei", "Frei-Wunsch", "Kompensation", "Kompensation fix",
        "Externer Arbeitseinsatz", "Ferien", "Flexitag",
    }
    if b in ABSENZ_TYPES:
        return "FREI"

    # Unknown — caller will warn
    return "UNKNOWN:" + b


# ---------------------------------------------------------------------------
# ICS helpers
# ---------------------------------------------------------------------------

def ics_date(d: date) -> str:
    return d.strftime("%Y%m%d")


def make_event(title: str, start: date, end_inclusive: date) -> str:
    """Return a VEVENT block. end date in ICS is exclusive, so +1 day."""
    dtend = end_inclusive + timedelta(days=1)
    uid = str(uuid.uuid4())
    return (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{title}\r\n"
        f"DTSTART;VALUE=DATE:{ics_date(start)}\r\n"
        f"DTEND;VALUE=DATE:{ics_date(dtend)}\r\n"
        "END:VEVENT\r\n"
    )


# ---------------------------------------------------------------------------
# Core logic
# ---------------------------------------------------------------------------

def load_csv(path: str) -> list[dict]:
    rows = []
    for encoding in ("utf-8", "iso-8859-1"):
        try:
            with open(path, encoding=encoding, newline="") as f:
                reader = csv.DictReader(f, delimiter=";")
                rows = list(reader)
            break
        except UnicodeDecodeError:
            continue
    return rows


def parse_date(s: str) -> date:
    """Parse DD.MM.YYYY."""
    d, m, y = s.strip().split(".")
    return date(int(y), int(m), int(d))


def find_person_rows(rows: list[dict], name: str) -> list[dict]:
    """Case-insensitive partial match on Suchname."""
    name_lower = name.lower()
    return [r for r in rows if name_lower in r["Suchname"].strip().lower()]


def build_events(person_rows: list[dict]) -> tuple[list[tuple], list[str]]:
    """
    Returns (events, warnings).
    events = list of (title, start_date, end_date_inclusive)
    warnings = list of unknown Bezeichnung strings
    """
    # Collect (date, title) pairs, skip None
    day_entries: dict[date, str] = {}
    warnings = []

    for row in person_rows:
        bezeichnung = row["Bezeichnung"].strip()
        d = parse_date(row["Datum"])
        title = classify(bezeichnung)

        if title is None:
            continue
        if title.startswith("UNKNOWN:"):
            unknown = title[8:]
            if unknown not in warnings:
                warnings.append(unknown)
            continue

        # If multiple entries on same day, prefer non-FREI
        if d in day_entries:
            existing = day_entries[d]
            if existing == "FREI" and title != "FREI":
                day_entries[d] = title
        else:
            day_entries[d] = title

    if not day_entries:
        return [], warnings

    # Sort by date
    sorted_days = sorted(day_entries.items())

    # ---------------------------------------------------------------------------
    # Merge consecutive same-type entries, with FREI weekend-bridge rule:
    # If a FREI block is separated from another FREI block only by Sat/Sun
    # with no other entries on those days, merge them.
    # ---------------------------------------------------------------------------

    def is_weekend(d: date) -> bool:
        return d.weekday() >= 5  # 5=Sat, 6=Sun

    def weekend_between_has_no_other_entries(
        end1: date, start2: date, all_dates: set[date]
    ) -> bool:
        """Check that all dates between end1 and start2 are weekends not in entries."""
        d = end1 + timedelta(days=1)
        while d < start2:
            if not is_weekend(d):
                return False
            if d in all_dates:
                return False
            d += timedelta(days=1)
        return True

    all_entry_dates = {d for d, _ in sorted_days}

    # Build merged blocks
    blocks: list[tuple[str, date, date]] = []  # (title, start, end_inclusive)

    cur_title, cur_start, cur_end = sorted_days[0][1], sorted_days[0][0], sorted_days[0][0]

    for d, title in sorted_days[1:]:
        gap_days = (d - cur_end).days

        # Direct consecutive same type
        if title == cur_title and gap_days == 1:
            cur_end = d
            continue

        # FREI weekend bridge: gap is 2 or 3 days (Sat+Sun or Fri+Sat+Sun)
        if (
            title == "FREI"
            and cur_title == "FREI"
            and 1 < gap_days <= 3
            and weekend_between_has_no_other_entries(cur_end, d, all_entry_dates)
        ):
            cur_end = d
            continue

        # Flush current block
        blocks.append((cur_title, cur_start, cur_end))
        cur_title, cur_start, cur_end = title, d, d

    blocks.append((cur_title, cur_start, cur_end))

    return blocks, warnings


def build_ics(events: list[tuple[str, date, date]]) -> str:
    lines = [
        "BEGIN:VCALENDAR\r\n",
        "VERSION:2.0\r\n",
        "PRODID:-//csv_to_gcal//KSBL Radiologie//DE\r\n",
        "CALSCALE:GREGORIAN\r\n",
        "METHOD:PUBLISH\r\n",
    ]
    for title, start, end in events:
        lines.append(make_event(title, start, end))
    lines.append("END:VCALENDAR\r\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Export Wochenplan shifts to Google Calendar ICS.")
    parser.add_argument("--csv", required=True, help="Path to the Wochenplan CSV file")
    parser.add_argument("--name", required=True, help="Person name to filter (partial match on Suchname)")
    parser.add_argument("--out", default=None, help="Output .ics file path (default: <name>.ics)")
    args = parser.parse_args()

    rows = load_csv(args.csv)
    if not rows:
        print(f"ERROR: Could not read CSV file: {args.csv}", file=sys.stderr)
        sys.exit(1)

    person_rows = find_person_rows(rows, args.name)
    if not person_rows:
        print(f"ERROR: No entries found for '{args.name}'. Check spelling.", file=sys.stderr)
        # Show available names to help
        names = sorted({r["Suchname"].strip() for r in rows})
        print("Available names:", file=sys.stderr)
        for n in names:
            print(f"  {n}", file=sys.stderr)
        sys.exit(1)

    matched_name = person_rows[0]["Suchname"].strip()
    print(f"Found {len(person_rows)} entries for: {matched_name}")

    events, warnings = build_events(person_rows)

    if warnings:
        print(f"\nWARNING: {len(warnings)} unknown Bezeichnung(en) were skipped:")
        for w in warnings:
            print(f"  - {w}")
        print("Add them to the classify() function in csv_to_gcal.py to handle them.\n")

    if not events:
        print("No calendar entries to export (all shifts are regular weekday Tagdienst).")
        sys.exit(0)

    # Print summary
    print(f"\nEvents to export ({len(events)}):")
    for title, start, end in events:
        if start == end:
            print(f"  {start.strftime('%d.%m.%Y')}  {title}")
        else:
            print(f"  {start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}  {title}")

    # Write ICS
    out_path = args.out or f"{matched_name.replace(' ', '_')}.ics"
    ics_content = build_ics(events)
    Path(out_path).write_text(ics_content, encoding="utf-8")
    print(f"\nSaved: {out_path}")


if __name__ == "__main__":
    main()
