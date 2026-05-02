# Wochenplan to Google Calendar - Technical Documentation

## Overview
This project converts shift schedule CSV files into Google Calendar-compatible ICS files. It consists of two components:
1. **csv_to_gcal.py** - Command-line script
2. **streamlit_app.py** - Web UI with interactive features

---

## Core Concepts

### Event Classification
The `classify()` function maps CSV "Bezeichnung" values to standardized event types:

- **WOCHENENDDIENST** - Weekend shifts (Sa/So)
- **NACHTDIENST** - Night shifts
- **HINTERGRUND** - On-call weekday shifts (Pikett_Nacht_Mo-Fr)
- **SPÄTDIENST** - Late shifts
- **FREI** - Time off (Frei, Ferien, Kompensation, etc.)
- **FORTBILDUNG** - Training/education

Returns `None` for regular weekday shifts (Tagdienst Mo-Fr) - these are skipped.

---

## Key Logic Flow

### 1. CSV Loading and Parsing

```python
def load_csv(path: str) -> list[dict]:
```

**Purpose:** Load CSV with encoding fallback (UTF-8 → ISO-8859-1)

**Key points:**
- Uses `;` as delimiter
- Returns list of dictionaries (one per row)
- Each row has keys: Suchname, Bezeichnung, Datum, etc.

---

### 2. Data Collection Phase

```python
for row in person_rows:
    bezeichnung = row["Bezeichnung"].strip()
    d = parse_date(row["Datum"])
    
    # Apply date range filter
    if date_start and d < date_start:
        continue
    if date_end and d > date_end:
        continue
    
    title = classify(bezeichnung)
    
    # Allow multiple entries per day
    if d in day_entries:
        if title not in day_entries[d]:
            day_entries[d].append(title)
    else:
        day_entries[d] = [title]
```

**Data structure:** `day_entries: dict[date, list[str]]`
- Key: Date object
- Value: List of event types on that day

**Example:**
```python
{
    date(2026, 2, 12): ["FREI", "HINTERGRUND"],  # Multiple events same day
    date(2026, 2, 13): ["FREI"],
    date(2026, 2, 14): ["FREI"]
}
```

**Key behavior:**
- Allows multiple event types per day (e.g., FREI + HINTERGRUND)
- Skips duplicates (same event type twice on same day)

---

### 3. Event Block Building Phase

This is the core logic that merges individual days into multi-day blocks.

```python
# Process each event type separately
for event_type in sorted(all_titles):
    # Get all dates for this event type
    dates_for_type = sorted([d for d, titles in day_entries.items() if event_type in titles])
```

**Why process separately?** Each event type (FREI, NACHTDIENST, etc.) forms its own blocks independently. A day can be in multiple blocks if it has multiple event types.

---

#### 3a. Consecutive Day Merging

```python
for d in dates_for_type[1:]:
    gap_days = (d - cur_end).days
    
    # Consecutive days - only merge if merge_adjacent is enabled
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
```

**Logic:**
1. If days are consecutive (gap = 1) AND `merge_adjacent` is enabled → merge them
2. **Special case for FREI:** If `merge_frei_weekends` is disabled, don't merge when crossing weekend/weekday boundary
   - Example: Fri→Sat or Sun→Mon won't merge if weekends disabled
   - This prevents weekend FREI from attaching to weekday FREI blocks

**Example with merge_adjacent=True, merge_frei_weekends=False:**
```python
# Input FREI dates: Jan 31 (Sat), Feb 1 (Sun), Feb 2 (Mon), Feb 3 (Tue)
# Output blocks:
#   - Jan 31-Feb 1 (Sat-Sun) → one block
#   - Feb 2-3 (Mon-Tue) → separate block
# They don't merge because Sun→Mon crosses weekend boundary
```

---

#### 3b. Weekend Bridging (FREI only)

```python
# For FREI: bridge across weekends (2-3 day gaps that are only weekends)
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
```

**Purpose:** Extend FREI blocks across weekends when they sandwich the weekend.

**Conditions (all must be true):**
1. Event type is FREI
2. `merge_frei_weekends` is enabled
3. Gap is 2-3 days (typical weekend)
4. All days in gap are weekends
5. Weekend days have NO other non-FREI events

**Example:**
```python
# Thu-Fri FREI, Sat-Sun empty, Mon-Tue FREI
# Gap between Fri and Mon = 3 days (Sat, Sun, Mon counted as gap)
# All gap days are weekend → bridge them
# Result: Thu-Tue as one block (includes the weekend)

# But if Sat has WOCHENENDDIENST:
# weekend_has_other_events = True
# Don't bridge → Thu-Fri separate, Mon-Tue separate
```

---

#### 3c. Standalone Weekend Filtering (FREI only)

```python
# For FREI blocks: filter out standalone weekend blocks when merge is DISABLED
if event_type == "FREI" and not merge_frei_weekends:
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
```

**Purpose:** Remove FREI blocks that are ONLY weekend days (Sat-Sun) when weekend merging is disabled.

**Logic:**
1. Only applies when `merge_frei_weekends = False`
2. Check each FREI block
3. If block contains at least one weekday → keep it
4. If block is only weekends (Sat-Sun) → discard it

**Example:**
```python
# Input FREI blocks:
#   - Sat-Sun (standalone weekend)
#   - Mon-Tue (weekdays)
#   - Thu-Sun (has weekdays Thu-Fri)

# Output (with merge_frei_weekends=False):
#   - Sat-Sun → FILTERED OUT (no weekdays)
#   - Mon-Tue → kept
#   - Thu-Sun → kept (has Thu and Fri)
```

**Why?** Standalone weekend FREI without adjacent weekday FREI is probably a data entry error or should be excluded from calendar.

---

### 4. ICS File Generation

```python
def build_ics(events: list[tuple[str, date, date]], labels: dict, colors: dict) -> str:
    for event_type, start, end in events:
        title = labels.get(event_type, event_type)
        color = colors.get(event_type, None)
        lines.append(make_event(title, start, end, event_type, color))
```

**Key points:**
- Uses custom labels (e.g., "Weekend Shift" instead of "WOCHENENDDIENST")
- Adds COLOR property if specified (Google Calendar supports this)
- Date end is EXCLUSIVE in ICS format → adds +1 day

```python
def make_event(title, start, end_inclusive, event_type, color):
    dtend = end_inclusive + timedelta(days=1)  # ICS uses exclusive end
```

---

## Streamlit-Specific Features

### Session State Management

```python
if 'event_labels' not in st.session_state:
    st.session_state.event_labels = DEFAULT_LABELS.copy()
if 'event_enabled' not in st.session_state:
    st.session_state.event_enabled = {k: True for k in DEFAULT_LABELS.keys()}
if 'event_colors' not in st.session_state:
    st.session_state.event_colors = EVENT_COLORS.copy()
if 'filter_option' not in st.session_state:
    st.session_state.filter_option = "All events"
```

**Purpose:** Persist user settings across Streamlit reruns
- `event_labels` - Custom names for each event type
- `event_enabled` - Which events to export (when using "Benutzerdefiniert" filter)
- `event_colors` - ICS color for each event type
- `filter_option` - Currently selected export filter

---

### Filter Application

```python
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
```

**Filter modes:**
1. **All events** - No filtering
2. **Dienste only** - Only shift types (work events)
3. **Absenzen only** - Only absence types (time off)
4. **Benutzerdefiniert** - Uses individual checkboxes from settings

---

### Calendar Preview

```python
def render_calendar_preview(events, labels):
    # Build date -> event mapping with block info
    date_to_events = {}
    for event_type, start, end in events:
        label = labels.get(event_type, event_type)
        d = start
        while d <= end:
            if d not in date_to_events:
                date_to_events[d] = []
            date_to_events[d].append((event_type, start, end, label))
            d += timedelta(days=1)
```

**Purpose:** Show month calendar with events marked

**Visual distinction:**
- **Single-day events:** Normal border (1px), shows "FREI"
- **Multi-day blocks:** Thicker border (2px blue), shows "FREI (12.02-15.02)"

**How it detects multi-day:**
```python
is_multi_day = (end - start).days > 0
if is_multi_day:
    range_text = f"{start.strftime('%d.%m')}-{end.strftime('%d.%m')}"
    event_texts.append(f"<small>{label} ({range_text})</small>")
```

---

## Common Scenarios

### Scenario 1: Weekend between weekday FREI blocks

**CSV data:**
- Thu-Fri: Frei
- Sat-Sun: (no entries)
- Mon: Frei

**With merge_frei_weekends=True:**
- Result: One block Thu-Mon (includes weekend)

**With merge_frei_weekends=False:**
- Result: Two blocks (Thu-Fri, Mon only)
- Weekend gap not bridged

---

### Scenario 2: Standalone weekend FREI

**CSV data:**
- Sat-Sun: Frei
- (no adjacent weekday FREI)

**With merge_frei_weekends=True:**
- Result: One block Sat-Sun (kept)

**With merge_frei_weekends=False:**
- Result: FILTERED OUT (no weekdays in block)

---

### Scenario 3: Multiple events same day

**CSV data:**
- Thu: Frei + Pikett_Nacht_Mo-Fr

**Result:**
- Two separate calendar entries for Thu
- FREI block: might span multiple days
- HINTERGRUND block: might span multiple days
- Both appear on Thu (overlapping)

---

### Scenario 4: Weekend with other events

**CSV data:**
- Fri: Frei
- Sat-Sun: Frei + Pikett_24h_Sa/So (WOCHENENDDIENST)
- Mon: Frei

**With merge_frei_weekends=True:**
- FREI: Two blocks (Fri only, Mon only)
  - Weekend NOT bridged because it has WOCHENENDDIENST
- WOCHENENDDIENST: One block Sat-Sun

**Result:** 3 calendar entries total
- Fri: FREI
- Sat-Sun: FREI (overlapping with WOCHENENDDIENST)
- Sat-Sun: WOCHENENDDIENST

**Edge case note:** Having both FREI and WOCHENENDDIENST on weekends is typically a planning error. The system keeps both to alert the user.

---

## Configuration Constants

### Event Type Mappings

**In classify():**
```python
# Weekend work
if "Tagdienst Sa/So" in b:
    return "WOCHENENDDIENST"

# Night shifts  
if "Nachtdienst" in b:
    return "NACHTDIENST"

# Hintergrund (on-call weekdays)
if "Pikett_Nacht_Mo-Fr" in b:
    return "HINTERGRUND"

# Absences
ABSENZ_TYPES = {
    "Frei", "Frei-Wunsch", "Kompensation", "Ferien", 
    "Flexitag", "Treueprämie Ferien", ...
}
```

**To add new mappings:** Edit the `classify()` function and add pattern matching logic.

---

### Color Configuration (Streamlit)

```python
EVENT_COLORS = {
    "WOCHENENDDIENST": None,  # No color by default
    "NACHTDIENST": None,
    # ... user can override in UI
}
```

**Available ICS colors:**
- RED, ORANGE, YELLOW, GREEN, BLUE, PURPLE, PINK, BROWN, GRAY
- None (no color property in ICS)

---

## File Paths

### Command-line Script
- Input: CSV file path via `--csv` argument
- Output: ICS file (default: `{name}.ics`, or via `--out` argument)

### Streamlit App
- Input: File uploaded via web UI
- Output: In-memory ICS file → download button

---

## Error Handling

### Unknown Bezeichnung
```python
if title.startswith("UNKNOWN:"):
    unknown = title[8:]
    if unknown not in warnings:
        warnings.append(unknown)
```

**Behavior:**
- Skips the entry (not included in calendar)
- Adds to warnings list
- Displayed to user at end

**To fix:** Add new pattern to `classify()` function

---

## Performance Considerations

### Time Complexity
- CSV parsing: O(n) where n = number of rows
- Event building: O(m × d) where m = event types, d = unique dates per type
- Calendar rendering: O(months × days_per_month)

**Typical values:**
- n = 1000-5000 rows (one month for 20-30 people)
- m = 6 event types
- Performance is excellent for typical use cases

---

## Common Modifications

### Add new event type

1. **Update classify():**
```python
if "New_Pattern" in b:
    return "NEW_EVENT_TYPE"
```

2. **Update DEFAULT_LABELS:**
```python
DEFAULT_LABELS = {
    # ... existing
    "NEW_EVENT_TYPE": "NEW_EVENT_TYPE",
}
```

3. **Update CSV_PATTERNS (Streamlit):**
```python
CSV_PATTERNS = {
    # ... existing
    "NEW_EVENT_TYPE": ["New_Pattern"],
}
```

4. **Update filter categories if needed:**
```python
# In main():
if st.session_state.filter_option == "Dienste only":
    dienste_types = {
        # ... existing
        "NEW_EVENT_TYPE"  # if it's a dienst
    }
```

---

### Change weekend filtering behavior

Edit the filtering logic at the end of `build_events()`:

```python
if event_type == "FREI" and not merge_frei_weekends:
    # Current: filter out weekend-only blocks
    # To keep all: change condition or skip this section
```

---

## Testing Scenarios

### Test Case 1: Basic merging
- Input: Mon, Tue, Wed as separate FREI entries
- merge_adjacent=True
- Expected: One block Mon-Wed

### Test Case 2: Weekend bridging
- Input: Fri FREI, Mon FREI (Sat-Sun empty)
- merge_frei_weekends=True
- Expected: One block Fri-Mon

### Test Case 3: Weekend with other events
- Input: Fri FREI, Sat-Sun FREI+WOCHENENDDIENST, Mon FREI
- merge_frei_weekends=True
- Expected: FREI blocks Fri and Mon separate (not bridged)

### Test Case 4: Multiple events same day
- Input: Thu FREI + HINTERGRUND
- Expected: Two overlapping calendar entries for Thu

---

## Known Edge Cases

1. **FREI + weekend dienst on same day**
   - System keeps both events (alerts user to planning conflict)
   - Typically a data entry error

2. **Cross-month blocks**
   - Works correctly (e.g., Jan 31 - Feb 2)
   - Calendar preview shows in both months

3. **Date range filtering mid-block**
   - If date range cuts a block (e.g., block is Feb 1-5, range is Feb 3-10)
   - Block gets truncated to date range (Feb 3-5 only)

---

## Troubleshooting

### Events not appearing
1. Check if CSV Bezeichnung matches patterns in `classify()`
2. Check if event type is enabled in filter settings
3. Check date range filter

### Unexpected merging
1. Verify merge_adjacent setting
2. For FREI: verify merge_frei_weekends setting
3. Check for weekend/weekday boundary crossing

### Standalone weekends appearing
1. Check merge_frei_weekends setting
2. Verify no adjacent weekday FREI exists
3. This is expected behavior when merge_frei_weekends=True

---

## Dependencies

### Command-line Script
- Python 3.9+ (uses `Optional` type hints)
- Standard library only: argparse, csv, datetime, pathlib, uuid

### Streamlit App
- Python 3.9+
- streamlit
- Standard library: csv, datetime, calendar, uuid, io

### Installation
```bash
pip install streamlit
```

---

## Future Enhancement Ideas

1. **Recurring event detection** - Identify patterns (e.g., every Saturday)
2. **Conflict detection** - Warn about overlapping dienst types
3. **Statistics dashboard** - Show total hours, dienst distribution
4. **Multi-person export** - Generate one ICS with multiple people's shifts
5. **Excel output** - Alternative to ICS format
6. **Auto-classification** - ML to learn new Bezeichnung patterns

---

## Version History

### Current Version
- Multiple events per day support
- Weekend bridging with conflict detection
- Calendar preview with block visualization
- Custom labels and colors
- Date range filtering
- Filter modes (All/Dienste/Absenzen/Custom)

### Key Design Decisions
- Process each event type separately (allows overlapping events)
- FREI-specific weekend logic (other types don't bridge weekends)
- Session state for Streamlit settings persistence
- Standalone weekend filtering (reduces noise in calendar)
