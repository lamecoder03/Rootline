# The deterministic signal underneath the data: trend, weekly shape, yearly shape, and
# holiday effects. Revenue and marketing spend both build on these, which is why the two
# tables correlate structurally instead of being glued together after the fact.
# Everything here is a pure function of date and dimension, so it is noise-free and testable.

import math
from datetime import date, timedelta

import holidays as holidays_pkg

from config import (
    CATEGORY_ANNUAL_GROWTH,
    CATEGORY_HOLIDAY_SENSITIVITY,
    CHANNEL_WEEKLY_AMPLITUDE,
    END_DATE,
    REGION_GROWTH_ADJUSTMENT,
    START_DATE,
    WEEKDAY_FACTOR,
)

YEARS = tuple(range(START_DATE.year, END_DATE.year + 1))

# Retail calendar effects as multipliers on a normal day. Negative days (Christmas, New
# Year) matter as much as the peaks: they are the seasonal dips a naive detector would
# misread as incidents.
_HOLIDAY_BASE_EFFECT = {
    "Thanksgiving Day": 1.35,
    "Black Friday": 2.60,
    "Cyber Monday": 2.30,
    "Christmas Eve": 0.55,
    "Christmas Day": 0.35,
    "Boxing Day": 1.25,
    "New Year's Day": 0.70,
    "Independence Day": 0.80,
    "Memorial Day": 1.15,
    "Labor Day": 1.15,
    "Washington's Birthday": 1.08,
    "Veterans Day": 1.05,
    "Juneteenth National Independence Day": 0.95,
    "Martin Luther King Jr. Day": 1.02,
    "Columbus Day": 1.06,
}
_DEFAULT_HOLIDAY_EFFECT = 0.97


def _thanksgiving(year):
    """Fourth Thursday in November, needed because Black Friday and Cyber Monday are
    defined relative to it and are the two largest revenue days in the whole series."""
    november = date(year, 11, 1)
    first_thursday = november + timedelta(days=(3 - november.weekday()) % 7)
    return first_thursday + timedelta(days=21)


def build_holiday_calendar():
    """Real US federal holidays from the `holidays` package, plus the retail-only dates it
    does not carry (Black Friday, Cyber Monday, Christmas Eve, Boxing Day).
    Returns {date: (name, is_federal, is_retail_event)} across the full series range."""
    federal = holidays_pkg.US(years=YEARS)
    calendar = {}

    for day, name in federal.items():
        if START_DATE <= day <= END_DATE:
            calendar[day] = (name, True, False)

    for year in YEARS:
        thanksgiving = _thanksgiving(year)
        extras = {
            thanksgiving + timedelta(days=1): "Black Friday",
            thanksgiving + timedelta(days=4): "Cyber Monday",
            date(year, 12, 24): "Christmas Eve",
            date(year, 12, 26): "Boxing Day",
        }
        for day, name in extras.items():
            if START_DATE <= day <= END_DATE:
                calendar[day] = (name, False, True)

    return dict(sorted(calendar.items()))


HOLIDAY_CALENDAR = build_holiday_calendar()


def trend_factor(day, category, region):
    """Compound growth from the series start, varied by category and nudged per region so
    slices diverge slowly over the two years instead of scaling as one block."""
    annual = CATEGORY_ANNUAL_GROWTH[category] * REGION_GROWTH_ADJUSTMENT[region]
    elapsed_years = (day - START_DATE).days / 365.25
    return annual**elapsed_years


def weekly_factor(day, channel):
    """Day-of-week shape, with each channel amplifying or damping the same underlying
    curve. Deviation from 1.0 is scaled, so the weekly pattern stays in phase everywhere."""
    base = WEEKDAY_FACTOR[day.weekday()]
    return 1.0 + (base - 1.0) * CHANNEL_WEEKLY_AMPLITUDE[channel]


def yearly_factor(day):
    """Annual shape: a mild sine wave for the spring/summer cycle plus a Gaussian Q4 bump
    peaking in early December. This is the seasonality the detector's rolling baseline must absorb."""
    day_of_year = day.timetuple().tm_yday
    sine = 0.10 * math.sin(2 * math.pi * (day_of_year - 80) / 365.25)
    q4_peak = date(day.year, 12, 8).timetuple().tm_yday
    q4_bump = 0.45 * math.exp(-(((day_of_year - q4_peak) / 22.0) ** 2))
    return 1.0 + sine + q4_bump


def holiday_factor(day, category):
    """Calendar effect for a given day, scaled by how hard the category reacts —
    Electronics lives or dies on Black Friday, Home & Garden barely notices it."""
    entry = HOLIDAY_CALENDAR.get(day)
    if entry is None:
        return 1.0
    name = entry[0].replace(" (observed)", "").strip()
    base = _HOLIDAY_BASE_EFFECT.get(name, _DEFAULT_HOLIDAY_EFFECT)
    return 1.0 + (base - 1.0) * CATEGORY_HOLIDAY_SENSITIVITY[category]


def baseline_signal(day, category, channel, region):
    """The full noise-free expected revenue for one cell on one day. The revenue generator
    adds noise on top of this; the spend generator sizes budgets against it."""
    return (
        trend_factor(day, category, region)
        * weekly_factor(day, channel)
        * yearly_factor(day)
        * holiday_factor(day, category)
    )


def date_range():
    """Every day in the series, inclusive of both endpoints."""
    span = (END_DATE - START_DATE).days
    return [START_DATE + timedelta(days=offset) for offset in range(span + 1)]
