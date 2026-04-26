python3 csv_to_gcal.py --csv KW18.csv --name "familyname firstname"
python3 csv_to_gcal.py --csv KW18.csv --name "familyname "        # partial match works too
python3 csv_to_gcal.py --csv KW18.csv --name "familyname " --out mein_kalender.ics


A few things worth noting: name matching is case-insensitive partial match, so you don't need the full name or correct accents. If an unknown Bezeichnung appears it prints a warning with the exact string and tells you to add it to classify(). The classify() function at the top is the single place to add or change any Bezeichnung mapping.