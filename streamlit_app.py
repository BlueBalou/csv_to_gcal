#!/usr/bin/env python3
"""
Streamlit web UI for csv_to_gcal.py
Allows users to upload CSV files and generate calendar exports through a web interface.
"""

import streamlit as st
import csv
import uuid
import calendar as cal_lib
from datetime import date, timedelta
from pathlib import Path
from typing import Optional
import io


# ---------------------------------------------------------------------------
# Event type color mapping for ICS calendar
# ---------------------------------------------------------------------------

EVENT_COLORS = {
    "WOCHENENDDIENST": None,
    "NACHTDIENST": None,
    "HINTERGRUND": None,
    "SPÄTDIENST": None,
    "FREI": None,
    "FORTBILDUNG": None,
}

# Default labels for event types
DEFAULT_LABELS = {
    "WOCHENENDDIENST": "WOCHENENDDIENST",
    "NACHTDIENST": "NACHTDIENST",
    "HINTERGRUND": "HINTERGRUND",
    "SPÄTDIENST": "SPÄTDIENST",
    "FREI": "FREI",
    "FORTBILDUNG": "FORTBILDUNG",
}

# CSV Bezeichnung patterns for each event type (for documentation)
CSV_PATTERNS = {
    "WOCHENENDDIENST": ["Tagdienst Sa/So", "Pikett_24h_Sa/So", "Pikett_Vormittag_Sa/So"],
    "NACHTDIENST": ["Nachtdienst"],
    "HINTERGRUND": ["Pikett_Nacht_Mo-Fr"],
    "SPÄTDIENST": ["Spätdienst", "Spatdienst"],
    "FREI": ["Frei", "Frei-Wunsch", "Kompensation", "Kompensation fix", "Externer Arbeitseinsatz", "Ferien", "Flexitag", "Treueprämie Ferien"],
    "FORTBILDUNG": ["Fort- und Weiterbildung"],
}


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
        "Externer Arbeitseinsatz", "Ferien", "Flexitag", "Treueprämie Ferien",
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


def make_event(title: str, start: date, end_inclusive: date, event_type: str, color: Optional[str]) -> str:
    """Return a VEVENT block with optional color. end date in ICS is exclusive, so +1 day."""
    dtend = end_inclusive + timedelta(days=1)
    uid = str(uuid.uuid4())
    
    event_str = (
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{title}\r\n"
        f"DTSTART;VALUE=DATE:{ics_date(start)}\r\n"
        f"DTEND;VALUE=DATE:{ics_date(dtend)}\r\n"
    )
    
    # Only add COLOR if specified
    if color:
        event_str += f"COLOR:{color}\r\n"
    
    event_str += "END:VEVENT\r\n"
    
    return event_str


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


def build_events(
    person_rows: list[dict],
    merge_adjacent: bool = True,
    merge_frei_weekends: bool = True,
    date_start: Optional[date] = None,
    date_end: Optional[date] = None
) -> tuple[list[tuple], list[str]]:
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
        
        # Apply date range filter
        if date_start and d < date_start:
            continue
        if date_end and d > date_end:
            continue
        
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

            # Consecutive days - only merge if merge_adjacent is enabled
            # For FREI: also check we're not crossing weekend boundary when merge_frei_weekends is disabled
            if gap_days == 1 and merge_adjacent:
                if event_type == "FREI" and not merge_frei_weekends:
                    # Don't merge if crossing weekend/weekday boundary
                    if is_weekend(cur_end) != is_weekend(d):
                        # Flush current block and start new one
                        type_blocks.append((event_type, cur_start, cur_end))
                        cur_start = d
                        cur_end = d
                        continue
                cur_end = d
                continue
            
            # For FREI: bridge across weekends (2-3 day gaps that are only weekends)
            # Only bridge if the weekend days don't have other non-FREI events
            if event_type == "FREI" and merge_frei_weekends and 1 < gap_days <= 3:
                all_weekend = True
                weekend_has_other_events = False
                check_date = cur_end + timedelta(days=1)
                while check_date < d:
                    if not is_weekend(check_date):
                        all_weekend = False
                        break
                    # Check if this weekend day has other non-FREI events
                    if check_date in day_entries:
                        other_events = [e for e in day_entries[check_date] if e != "FREI"]
                        if other_events:
                            weekend_has_other_events = True
                            break
                    check_date += timedelta(days=1)
                
                if all_weekend and not weekend_has_other_events:
                    cur_end = d
                    continue

            # Gap too large - flush current block
            type_blocks.append((event_type, cur_start, cur_end))
            cur_start = d
            cur_end = d

        # Flush final block
        type_blocks.append((event_type, cur_start, cur_end))

        # For FREI blocks: always filter out standalone weekend blocks
        # Weekends should only appear if they're adjacent to or bridge weekday FREI
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


def build_ics(events: list[tuple[str, date, date]], labels: dict[str, str], colors: dict[str, Optional[str]]) -> str:
    lines = [
        "BEGIN:VCALENDAR\r\n",
        "VERSION:2.0\r\n",
        "PRODID:-//csv_to_gcal//KSBL Radiologie//DE\r\n",
        "CALSCALE:GREGORIAN\r\n",
        "METHOD:PUBLISH\r\n",
    ]
    for event_type, start, end in events:
        # Use custom label if provided, otherwise use event_type
        title = labels.get(event_type, event_type)
        color = colors.get(event_type, None)
        lines.append(make_event(title, start, end, event_type, color))
    lines.append("END:VCALENDAR\r\n")
    return "".join(lines)


# ---------------------------------------------------------------------------
# Calendar preview helper
# ---------------------------------------------------------------------------

def render_calendar_preview(events: list[tuple[str, date, date]], labels: dict[str, str]):
    """Render a simple month-by-month calendar view of events with visual distinction for multi-day blocks."""
    if not events:
        return
    
    # Build a map of date -> list of (event_type, start_date, end_date, label)
    # This allows us to know if a day is part of a multi-day block
    date_to_events = {}
    for event_type, start, end in events:
        label = labels.get(event_type, event_type)
        d = start
        while d <= end:
            if d not in date_to_events:
                date_to_events[d] = []
            date_to_events[d].append((event_type, start, end, label))
            d += timedelta(days=1)
    
    # Group events by month
    events_by_month = {}
    for d, event_list in date_to_events.items():
        month_key = (d.year, d.month)
        if month_key not in events_by_month:
            events_by_month[month_key] = {}
        events_by_month[month_key][d] = event_list
    
    # Render each month
    for (year, month) in sorted(events_by_month.keys()):
        month_name = cal_lib.month_name[month]
        st.markdown(f"**{month_name} {year}**")
        
        # Create calendar
        cal = cal_lib.monthcalendar(year, month)
        
        # Build HTML table
        days = ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"]
        html = "<table style='width:100%; border-collapse: collapse; font-size: 0.85em;'>"
        html += "<tr>" + "".join([f"<th style='padding:4px; text-align:center; background:rgba(128, 128, 128, 0.15);'>{d}</th>" for d in days]) + "</tr>"
        
        for week in cal:
            html += "<tr>"
            for day in week:
                if day == 0:
                    html += "<td style='padding:4px;'></td>"
                else:
                    d = date(year, month, day)
                    if d in events_by_month[(year, month)]:
                        event_list = events_by_month[(year, month)][d]
                        
                        # Build event text with block ranges
                        event_texts = []
                        for event_type, start, end, label in event_list:
                            is_multi_day = (end - start).days > 0
                            if is_multi_day:
                                # Multi-day block: show date range
                                range_text = f"{start.strftime('%d.%m')}-{end.strftime('%d.%m')}"
                                event_texts.append(f"<small>{label} ({range_text})</small>")
                            else:
                                # Single day
                                event_texts.append(f"<small>{label}</small>")
                        
                        event_text = "<br>".join(event_texts)
                        
                        # Check if any event on this day is part of a multi-day block
                        has_multi_day = any((end - start).days > 0 for _, start, end, _ in event_list)
                        
                        if has_multi_day:
                            # Multi-day block: thicker border, slightly darker background
                            html += f"<td style='padding:4px; border:2px solid #4a90e2; background:rgba(100, 149, 237, 0.25); vertical-align:top;'><strong>{day}</strong><br>{event_text}</td>"
                        else:
                            # Single day: normal styling
                            html += f"<td style='padding:4px; border:1px solid #555; background:rgba(100, 149, 237, 0.2); vertical-align:top;'><strong>{day}</strong><br>{event_text}</td>"
                    else:
                        html += f"<td style='padding:4px; border:1px solid #555; vertical-align:top;'>{day}</td>"
            html += "</tr>"
        html += "</table>"
        
        st.markdown(html, unsafe_allow_html=True)
        st.markdown("<br>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Streamlit UI
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(page_title="Wochenplan to Google Calendar", page_icon="📅", layout="wide")
    
    st.title("📅 Wochenplan to Google Calendar")
    st.write("Convert your shift schedule CSV to a Google Calendar ICS file")
    
    # Initialize session state for settings
    if 'event_labels' not in st.session_state:
        st.session_state.event_labels = DEFAULT_LABELS.copy()
    if 'event_enabled' not in st.session_state:
        st.session_state.event_enabled = {k: True for k in DEFAULT_LABELS.keys()}
    if 'event_colors' not in st.session_state:
        st.session_state.event_colors = EVENT_COLORS.copy()
    if 'filter_option' not in st.session_state:
        st.session_state.filter_option = "All events"
    
    # Sidebar for settings
    with st.sidebar:
        st.header("⚙️ Settings")
        
        st.subheader("Export Filter")
        filter_option = st.radio(
            "Mode",
            options=["All events", "Dienste only", "Absenzen only", "Benutzerdefiniert"],
            index=["All events", "Dienste only", "Absenzen only", "Benutzerdefiniert"].index(st.session_state.filter_option),
            key="filter_radio_sidebar",
            help="Dienste: WOCHENENDDIENST, NACHTDIENST, HINTERGRUND, SPÄTDIENST | Absenzen: FREI, FORTBILDUNG"
        )
        st.session_state.filter_option = filter_option
        
        st.subheader("Merge Options")
        merge_adjacent = st.checkbox("Merge adjacent events", value=True, 
                                     help="Combine consecutive days of the same event type into blocks")
        merge_frei_weekends = st.checkbox("Include weekends in FREI blocks", value=True,
                                          help="Extend FREI blocks to include adjacent/sandwiched weekends")
        
        st.subheader("Event Configuration")
        st.info("💡 Individual checkboxes only apply when 'Benutzerdefiniert' is selected above.")
        
        # Color options for ICS
        color_options = ["None", "RED", "ORANGE", "YELLOW", "GREEN", "BLUE", "PURPLE", "PINK", "BROWN", "GRAY"]
        
        for event_type in sorted(DEFAULT_LABELS.keys()):
            st.markdown(f"**{event_type}**")
            
            # Show CSV patterns
            patterns = CSV_PATTERNS.get(event_type, [])
            pattern_text = ", ".join(patterns[:3])  # Show first 3
            if len(patterns) > 3:
                pattern_text += f", ... (+{len(patterns)-3} more)"
            st.caption(f"📋 CSV: {pattern_text}")
            
            col1, col2, col3 = st.columns([1, 4, 3])
            with col1:
                # Disable checkbox unless "Benutzerdefiniert" is selected
                is_custom_mode = st.session_state.filter_option == "Benutzerdefiniert"
                st.session_state.event_enabled[event_type] = st.checkbox(
                    "Enable", 
                    value=st.session_state.event_enabled.get(event_type, True),
                    key=f"enable_{event_type}",
                    label_visibility="collapsed",
                    disabled=not is_custom_mode
                )
            with col2:
                st.session_state.event_labels[event_type] = st.text_input(
                    "Label",
                    value=st.session_state.event_labels.get(event_type, event_type),
                    key=f"label_{event_type}",
                    label_visibility="collapsed",
                    placeholder="Event label"
                )
            with col3:
                current_color = st.session_state.event_colors.get(event_type, None)
                color_index = 0 if current_color is None else (color_options.index(current_color) if current_color in color_options else 0)
                selected_color = st.selectbox(
                    "Color",
                    options=color_options,
                    index=color_index,
                    key=f"color_{event_type}",
                    label_visibility="collapsed"
                )
                st.session_state.event_colors[event_type] = None if selected_color == "None" else selected_color
            
            st.markdown("---")
    
    # File upload
    uploaded_file = st.file_uploader("Upload Wochenplan CSV", type=["csv"])
    
    if uploaded_file is not None:
        # Load CSV
        rows = load_csv_from_upload(uploaded_file)
        
        if not rows:
            st.error("Could not read CSV file. Please check the file format.")
            return
        
        # Get available names and date range
        available_names = sorted({r["Suchname"].strip() for r in rows})
        all_dates = [parse_date(r["Datum"]) for r in rows]
        min_date = min(all_dates)
        max_date = max(all_dates)
        
        st.success(f"CSV loaded successfully! Date range: {min_date.strftime('%d.%m.%Y')} - {max_date.strftime('%d.%m.%Y')}")
        
        # Name selection
        selected_name = st.selectbox(
            "Select person",
            options=available_names,
            help="Choose the person whose shifts you want to export"
        )
        
        # Date range filter
        col1, col2 = st.columns(2)
        with col1:
            date_start = st.date_input(
                "Start date",
                value=min_date,
                min_value=min_date,
                max_value=max_date,
                help="First date to include in export"
            )
        with col2:
            date_end = st.date_input(
                "End date",
                value=max_date,
                min_value=min_date,
                max_value=max_date,
                help="Last date to include in export"
            )
        
        # Quick date range presets
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            if st.button("Next month"):
                date_start = date.today()
                date_end = date.today() + timedelta(days=30)
        with col2:
            if st.button("Next 3 months"):
                date_start = date.today()
                date_end = date.today() + timedelta(days=90)
        with col3:
            if st.button("Next 6 months"):
                date_start = date.today()
                date_end = date.today() + timedelta(days=180)
        with col4:
            if st.button("All dates"):
                date_start = min_date
                date_end = max_date
        
        # Process button
        if st.button("Generate Calendar", type="primary"):
            person_rows = find_person_rows(rows, selected_name)
            
            if not person_rows:
                st.error(f"No entries found for '{selected_name}'")
                return
            
            st.success(f"Found {len(person_rows)} entries for: {selected_name}")
            
            # Build events with settings
            events, warnings = build_events(
                person_rows,
                merge_adjacent=merge_adjacent,
                merge_frei_weekends=merge_frei_weekends,
                date_start=date_start,
                date_end=date_end
            )
            
            # Apply filter
            if st.session_state.filter_option == "Dienste only":
                dienste_types = {"WOCHENENDDIENST", "NACHTDIENST", "HINTERGRUND", "SPÄTDIENST"}
                events = [(title, start, end) for title, start, end in events if title in dienste_types]
            elif st.session_state.filter_option == "Absenzen only":
                absenzen_types = {"FREI", "FORTBILDUNG"}
                events = [(title, start, end) for title, start, end in events if title in absenzen_types]
            elif st.session_state.filter_option == "Benutzerdefiniert":
                events = [(title, start, end) for title, start, end in events 
                         if st.session_state.event_enabled.get(title, True)]
            
            # Show warnings
            if warnings:
                with st.expander(f"⚠️ {len(warnings)} unknown Bezeichnung(en) were skipped"):
                    for w in warnings:
                        st.write(f"- {w}")
                    st.info("These entries were not recognized and will not appear in the calendar.")
            
            if not events:
                st.warning("No calendar entries to export (all shifts are regular weekday Tagdienst or filtered out).")
                return
            
            # Show event summary with compact formatting
            st.subheader(f"Events to export ({len(events)})")
            
            event_lines = []
            for event_type, start, end in events:
                label = st.session_state.event_labels.get(event_type, event_type)
                if start == end:
                    event_lines.append(f"📌 {start.strftime('%d.%m.%Y')} — **{label}**")
                else:
                    event_lines.append(f"📌 {start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')} — **{label}**")
            
            st.markdown("<small>" + "<br>".join(event_lines) + "</small>", unsafe_allow_html=True)
            
            # Calendar preview
            with st.expander("📅 Calendar Preview", expanded=False):
                render_calendar_preview(events, st.session_state.event_labels)
            
            # Generate ICS
            ics_content = build_ics(events, st.session_state.event_labels, st.session_state.event_colors)
            
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
