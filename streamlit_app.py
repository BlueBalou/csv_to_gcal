#!/usr/bin/env python3
"""
Streamlit web UI for csv_to_gcal.py
Allows users to upload CSV files and generate calendar exports through a web interface.
"""

import streamlit as st
import csv
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
import io


# ---------------------------------------------------------------------------
# Classification logic (same as csv_to_gcal.py)
# ---------------------------------------------------------------------------

def classify(bezeichnung: str) -> Optional[str]:
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

    # Hintergrund (on-call weekdays)
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

    # Unknown
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

def load_csv_from_upload(uploaded_file) -> list[dict]:
    """Load CSV from Streamlit uploaded file."""
    rows = []
    for encoding in ("utf-8", "iso-8859-1"):
        try:
            uploaded_file.seek(0)
            content = uploaded_file.read().decode(encoding)
            reader = csv.DictReader(io.StringIO(content), delimiter=";")
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
# Streamlit UI
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Wochenplan to Google Calendar", page_icon="📅")
    
    st.title("📅 Wochenplan to Google Calendar")
    st.write("Convert your shift schedule CSV to a Google Calendar ICS file")
    
    # File upload
    uploaded_file = st.file_uploader("Upload Wochenplan CSV", type=["csv"])
    
    if uploaded_file is not None:
        # Load CSV
        rows = load_csv_from_upload(uploaded_file)
        
        if not rows:
            st.error("Could not read CSV file. Please check the file format.")
            return
        
        # Get available names
        available_names = sorted({r["Suchname"].strip() for r in rows})
        
        # Name selection
        selected_name = st.selectbox(
            "Select person",
            options=available_names,
            help="Choose the person whose shifts you want to export"
        )
        
        # Filter selection
        filter_option = st.radio(
            "Export filter",
            options=["All events", "Dienste only", "Absenzen only"],
            help="Dienste: WOCHENENDDIENST, NACHTDIENST, HINTERGRUND, SPÄTDIENST | Absenzen: FREI, FORTBILDUNG"
        )
        
        # Process button
        if st.button("Generate Calendar", type="primary"):
            person_rows = find_person_rows(rows, selected_name)
            
            if not person_rows:
                st.error(f"No entries found for '{selected_name}'")
                return
            
            st.success(f"Found {len(person_rows)} entries for: {selected_name}")
            
            # Build events
            events, warnings = build_events(person_rows)
            
            # Apply filter
            if filter_option == "Dienste only":
                dienste_types = {"WOCHENENDDIENST", "NACHTDIENST", "HINTERGRUND", "SPÄTDIENST"}
                events = [(title, start, end) for title, start, end in events if title in dienste_types]
            elif filter_option == "Absenzen only":
                absenzen_types = {"FREI", "FORTBILDUNG"}
                events = [(title, start, end) for title, start, end in events if title in absenzen_types]
            
            # Show warnings
            if warnings:
                with st.expander(f"⚠️ {len(warnings)} unknown Bezeichnung(en) were skipped"):
                    for w in warnings:
                        st.write(f"- {w}")
                    st.info("These entries were not recognized and will not appear in the calendar.")
            
            if not events:
                st.warning("No calendar entries to export (all shifts are regular weekday Tagdienst or filtered out).")
                return
            
            # Show event summary
            st.subheader(f"Events to export ({len(events)})")

            # Build compact list as single markdown string
            event_lines = []
            for title, start, end in events:
                if start == end:
                    event_lines.append(f"📌 {start.strftime('%d.%m.%Y')} — **{title}**")
                else:
                    event_lines.append(f"📌 {start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')} — **{title}**")

            st.markdown("<small>" + "<br>".join(event_lines) + "</small>", unsafe_allow_html=True)

            # Generate ICS
            ics_content = build_ics(events)
            
            # Download button
            filename = f"{selected_name.replace(' ', '_')}.ics"
            st.download_button(
                label="⬇️ Download ICS file",
                data=ics_content,
                file_name=filename,
                mime="text/calendar",
                type="primary"
            )
            
            st.success("Calendar file ready! Click the button above to download.")
            st.info("💡 Import the .ics file into Google Calendar, Apple Calendar, or Outlook.")


if __name__ == "__main__":
    main()
