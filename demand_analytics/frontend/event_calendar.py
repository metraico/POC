import datetime
import pandas as pd


def _nth_weekday(year, month, weekday, n):
    """nth occurrence of weekday (Mon=0 … Sun=6) in given month."""
    first = datetime.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + datetime.timedelta(days=offset + (n - 1) * 7)


def _last_weekday(year, month, weekday):
    """Last occurrence of weekday in given month."""
    nxt = datetime.date(year + 1, 1, 1) if month == 12 else datetime.date(year, month + 1, 1)
    last = nxt - datetime.timedelta(1)
    return last - datetime.timedelta((last.weekday() - weekday) % 7)


def _iso_week(d, year):
    """ISO week number for date d if it belongs to `year`, else None."""
    iso_y, w, _ = d.isocalendar()
    return w if iso_y == year else None


def _week_dates(year, iso_week):
    """Return (monday, sunday) for ISO week iso_week of year."""
    jan4 = datetime.date(year, 1, 4)
    w1_mon = jan4 - datetime.timedelta(days=jan4.weekday())
    monday = w1_mon + datetime.timedelta(weeks=iso_week - 1)
    return monday, monday + datetime.timedelta(6)


# ── Variable festival dates (shift year to year) ──────────────────────────────

_VARIABLE = [
    ("Easter",              {2023:(4,9),   2024:(3,31), 2025:(4,20), 2026:(4,5)}),
    ("Holi",                {2023:(3,8),   2024:(3,25), 2025:(3,14), 2026:(3,3)}),
    ("Ramadan Begins",      {2023:(3,23),  2024:(3,11), 2025:(3,1),  2026:(2,18)}),
    ("Eid al-Fitr",         {2023:(4,21),  2024:(4,10), 2025:(3,30), 2026:(3,20)}),
    ("Eid al-Adha",         {2023:(6,28),  2024:(6,17), 2025:(6,7),  2026:(5,27)}),
    ("Onam (Thiruvonam)",   {2023:(8,29),  2024:(9,5),  2025:(8,27), 2026:(9,15)}),
    ("Ganesh Chaturthi",    {2023:(9,19),  2024:(9,7),  2025:(8,27), 2026:(9,15)}),
    ("Navratri Begins",     {2023:(10,15), 2024:(10,3), 2025:(9,22), 2026:(10,11)}),
    ("Dussehra",            {2023:(10,24), 2024:(10,12),2025:(10,2), 2026:(10,20)}),
    ("Diwali",              {2023:(11,12), 2024:(11,1), 2025:(10,20),2026:(11,8)}),
]

# ── Fixed holidays (same date every year) ────────────────────────────────────

_FIXED = [
    (1,  1,  "New Year's Day"),
    (1,  14, "Pongal / Makar Sankranti"),
    (2,  14, "Valentine's Day"),
    (3,  17, "St. Patrick's Day"),
    (4,  13, "Baisakhi"),
    (7,  4,  "Independence Day (US)"),
    (10, 31, "Halloween"),
    (11, 11, "Veterans Day"),
    (12, 24, "Christmas Eve"),
    (12, 25, "Christmas Day"),
    (12, 26, "Boxing Day"),
    (12, 31, "New Year's Eve"),
]

# ── Promotional spans (ISO week ranges, same every year) ─────────────────────

_PROMO_SPANS = [
    (range(6,  9),  "Super Bowl Season"),
    (range(13, 17), "Spring Sale"),
    (range(22, 28), "Summer Sale"),
    (range(31, 36), "Back to School"),
    (range(44, 53), "Holiday Shopping Season"),
]


def build_calendar_df(years):
    rows = []
    for year in sorted(int(y) for y in years):
        event_map = {}

        def add(week, name):
            if 1 <= week <= 52:
                event_map.setdefault(week, []).append(name)

        def add_date(d, name):
            w = _iso_week(d, year)
            if w is not None:
                add(w, name)

        # Fixed holidays
        for m, d, name in _FIXED:
            try:
                add_date(datetime.date(year, m, d), name)
            except ValueError:
                pass

        # Calculated US / global holidays
        add_date(_nth_weekday(year, 1, 0, 3),   "MLK Day")
        add_date(_nth_weekday(year, 2, 0, 3),   "Presidents' Day")
        add_date(_nth_weekday(year, 2, 6, 2),   "Super Bowl Sunday")
        add_date(_nth_weekday(year, 5, 6, 2),   "Mother's Day")
        add_date(_last_weekday(year, 5, 0),     "Memorial Day")
        add_date(_nth_weekday(year, 6, 6, 3),   "Father's Day")
        add_date(_nth_weekday(year, 9, 0, 1),   "Labor Day")
        add_date(_nth_weekday(year, 10, 0, 2),  "Columbus Day")
        thx = _nth_weekday(year, 11, 3, 4)
        add_date(thx,                            "Thanksgiving (US)")
        add_date(thx + datetime.timedelta(1),    "Black Friday")
        add_date(thx + datetime.timedelta(4),    "Cyber Monday")

        # Variable festivals
        for name, year_map in _VARIABLE:
            if year in year_map:
                m, d = year_map[year]
                add_date(datetime.date(year, m, d), name)

        # Promotional spans
        for week_range, label in _PROMO_SPANS:
            for w in week_range:
                add(w, label)

        # Build one row per week
        for wn in range(1, 53):
            start, end = _week_dates(year, wn)
            events = event_map.get(wn, [])
            rows.append({
                "Year":                year,
                "Week":                f"W{wn:02d}",
                "Date Range":          f"{start.strftime('%b %d')} – {end.strftime('%b %d')}",
                "Events / Promotions": ", ".join(events) if events else "—",
            })

    return pd.DataFrame(rows)
