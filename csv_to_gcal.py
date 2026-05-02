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
    
    if "Pikett_Nacht_Mo-Fr" in b:
        return "HINTERGRUND"

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
    # Collect (date, list of titles) pairs, skip None
    day_entries: dict[date, list[str]] = {}
    warnings = []

    def is_weekend(d: date) -> bool:
        return d.weekday() >= 5  # 5=Sat, 6=Sun

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

        # Allow multiple entries per day
        if d in day_entries:
            if title not in day_entries[d]:  # Avoid duplicates
                day_entries[d].append(title)
        else:
            day_entries[d] = [title]

    if not day_entries:
        return [], warnings

    # ---------------------------------------------------------------------------
    # Process each event type separately to build blocks
    # FREI gets special treatment: remove standalone weekend blocks not adjacent to weekday FREI
    # ---------------------------------------------------------------------------

    # Collect all unique event types
    all_titles = set()
    for titles_list in day_entries.values():
        all_titles.update(titles_list)

    blocks: list[tuple[str, date, date]] = []

    # Process each event type separately
    for event_type in sorted(all_titles):
        # Get all dates for this event type
        dates_for_type = sorted([d for d, titles in day_entries.items() if event_type in titles])
        
        if not dates_for_type:
            continue
        
        # Build blocks for this event type
        cur_start = dates_for_type[0]
        cur_end = dates_for_type[0]
        type_blocks = []

        for d in dates_for_type[1:]:
            gap_days = (d - cur_end).days

            # Consecutive days or weekend gap that should be bridged
            if gap_days == 1:
                cur_end = d
                continue
            
            # For FREI: bridge across weekends (2-3 day gaps that are only weekends)
            if event_type == "FREI" and 1 < gap_days <= 3:
                all_weekend = True
                check_date = cur_end + timedelta(days=1)
                while check_date < d:
                    if not is_weekend(check_date):
                        all_weekend = False
                        break
                    check_date += timedelta(days=1)
                
                if all_weekend:
                    cur_end = d
                    continue

            # Gap too large - flush current block
            type_blocks.append((event_type, cur_start, cur_end))
            cur_start = d
            cur_end = d

        # Flush final block
        type_blocks.append((event_type, cur_start, cur_end))

        # For FREI blocks: filter out standalone weekend blocks
        if event_type == "FREI":
            for block_start, block_end in [(b[1], b[2]) for b in type_blocks]:
                # Check if block contains any weekday
                has_weekday = False
                check_date = block_start
                while check_date <= block_end:
                    if not is_weekend(check_date):
                        has_weekday = True
                        break
                    check_date += timedelta(days=1)
                
                # Only keep blocks that have at least one weekday
                if has_weekday:
                    blocks.append((event_type, block_start, block_end))
        else:
            # Non-FREI blocks: keep all
            blocks.extend(type_blocks)

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
