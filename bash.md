all arguments:
--csv (required) - Path to the Wochenplan CSV file
--name (required) - Person name to filter (partial match on Suchname)
--out (optional) - Output .ics file path (default: <name>.ics)
--filter (optional) - Filter events by type:

dienste - exports only: WOCHENENDDIENST, NACHTDIENST, HINTERGRUND, SPÄTDIENST
absenzen - exports only: FREI, FORTBILDUNG
(no filter) - exports everything

name matching is case-insensitive partial match, so you don't need the full name or correct accents. If an unknown Bezeichnung appears it prints a warning with the exact string and tells you to add it to classify(). The classify() function at the top is the single place to add or change any Bezeichnung mapping.


Examples:
python3 csv_to_gcal.py --csv KW18.csv --name "familyname firstname"
python3 csv_to_gcal.py --csv KW18.csv --name "familyname "        # partial match works too
python3 csv_to_gcal.py --csv KW18.csv --name "familyname " --out mein_kalender.ics

CAUTION at least python 3.10 necessary. For homebrewed python on macos:
python3.12 csv_to_gcal.py --csv KW18.csv --name "familyname firstname" --out mein_kalender.ics

