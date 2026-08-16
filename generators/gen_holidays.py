# Writes out the real US holiday calendar for the series date range, sourced from the
# `holidays` package and extended with the retail-only dates it does not carry.
# Exists so the agent has a legitimate external explanation to test a revenue swing
# against — many of the largest moves in the data are calendar effects, not incidents.

import pandas as pd

from config import END_DATE, RAW_DIR, START_DATE
from curves import HOLIDAY_CALENDAR

# Qualitative retail weight, not the multiplier the simulation used. The agent gets a
# reasonable prior about which dates move revenue; it does not get the answer key.
RETAIL_SIGNIFICANCE = {
    "Black Friday": "high",
    "Cyber Monday": "high",
    "Christmas Day": "high",
    "Christmas Eve": "high",
    "Thanksgiving Day": "medium",
    "Boxing Day": "medium",
    "New Year's Day": "medium",
    "Independence Day": "medium",
    "Memorial Day": "medium",
    "Labor Day": "medium",
}


def build_holiday_table():
    """Flattens the shared calendar into a loadable table, tagging each date as a federal
    holiday, a retail event, or both, with the weekday it fell on."""
    records = []
    for day, (name, is_federal, is_retail_event) in HOLIDAY_CALENDAR.items():
        clean_name = name.replace(" (observed)", "").strip()
        records.append(
            {
                "holiday_date": day,
                "holiday_name": name,
                "country_code": "US",
                "is_federal_holiday": is_federal,
                "is_retail_event": is_retail_event,
                "retail_significance": RETAIL_SIGNIFICANCE.get(clean_name, "low"),
                "day_of_week": day.strftime("%A"),
            }
        )
    return pd.DataFrame(records).sort_values("holiday_date").reset_index(drop=True)


def main():
    calendar = build_holiday_table()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    calendar.to_csv(RAW_DIR / "holiday_calendar.csv", index=False)

    print(f"holiday_calendar.csv {len(calendar):>7,} rows "
          f"({START_DATE} .. {END_DATE})")
    print(f"  federal holidays: {int(calendar['is_federal_holiday'].sum())}  "
          f"retail-only events: {int(calendar['is_retail_event'].sum())}")
    print("  high-significance dates: "
          + ", ".join(
              f"{row.holiday_date} {row.holiday_name}"
              for row in calendar[calendar["retail_significance"] == "high"].itertuples()
          ))


if __name__ == "__main__":
    main()
