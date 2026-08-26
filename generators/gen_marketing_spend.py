# Generates daily marketing spend at the date x channel x category grain — one grain
# coarser than revenue, as real budgets are, so the dbt staging layer has to aggregate to join.
# Spend is sized against the same seasonal signal revenue uses, then blurred with smoothed
# noise and monthly budget steps, so the two correlate realistically rather than perfectly.

import numpy as np
import pandas as pd

from config import (
    CATEGORIES,
    CATEGORY_BASE_REVENUE,
    CHANNEL_SHARE,
    CHANNEL_SPEND_RATIO,
    CHANNELS,
    RAW_DIR,
    REGION_SHARE,
    REGIONS,
    SEED,
    SPEND_NOISE_SIGMA,
    SPEND_OVERRIDES,
)
from curves import baseline_signal, date_range

RNG = np.random.default_rng(SEED + 1)

# Media efficiency per channel. Mobile App buys attention at a premium but converts
# clicks harder, which is why cutting its budget shows up in revenue at all.
CHANNEL_CPM = {"Web": 8.50, "Mobile App": 12.00, "Marketplace": 6.00}
CHANNEL_CTR = {"Web": 0.011, "Mobile App": 0.018, "Marketplace": 0.009}
BUDGET_AUTOCORRELATION = 0.75
MONTHLY_BUDGET_SIGMA = 0.10


def smoothed_noise(day_count):
    """AR(1) noise in log space. Marketing budgets are planned and drift; independent
    daily draws would make spend look like white noise and give the agent a false signal."""
    shocks = RNG.normal(0.0, SPEND_NOISE_SIGMA, day_count)
    series = np.empty(day_count)
    series[0] = shocks[0]
    for position in range(1, day_count):
        series[position] = (
            BUDGET_AUTOCORRELATION * series[position - 1] + shocks[position]
        )
    return np.exp(series - 0.5 * SPEND_NOISE_SIGMA**2)


def monthly_budget_steps(days):
    """One budget multiplier per calendar month, held flat within the month. Spend steps
    at month boundaries in a way revenue does not, which keeps the correlation imperfect."""
    months = sorted({(day.year, day.month) for day in days})
    draws = {month: RNG.lognormal(0.0, MONTHLY_BUDGET_SIGMA) for month in months}
    return np.array([draws[(day.year, day.month)] for day in days])


def override_multipliers(days, channel, category):
    """Per-day spend multiplier from SPEND_OVERRIDES — the media footprint of the injected
    events. ANOM-02 has no override on purpose, so spend cannot explain the stockout dip."""
    dimensions = {"channel": channel, "category": category}
    multipliers = np.ones(len(days))

    for override in SPEND_OVERRIDES:
        if any(dimensions[key] != value for key, value in override["filters"].items()):
            continue
        mask = np.array([override["start"] <= day <= override["end"] for day in days])
        multipliers[mask] = override["multiplier"]

    return multipliers


def build_spend():
    """Builds spend as (revenue the channel-category supports) x (its spend ratio), then
    applies noise, monthly steps, and the injected overrides, and derives media metrics."""
    days = date_range()
    day_count = len(days)
    frames = []

    for channel in CHANNELS:
        for category in CATEGORIES:
            supported_revenue = np.array(
                [
                    CATEGORY_BASE_REVENUE[category]
                    * CHANNEL_SHARE[channel]
                    * sum(
                        REGION_SHARE[region]
                        * baseline_signal(day, category, channel, region)
                        for region in REGIONS
                    )
                    for day in days
                ]
            )
            spend = (
                supported_revenue
                * CHANNEL_SPEND_RATIO[channel]
                * smoothed_noise(day_count)
                * monthly_budget_steps(days)
                * override_multipliers(days, channel, category)
            )

            cpm = CHANNEL_CPM[channel] * RNG.lognormal(-0.002, 0.06, day_count)
            ctr = CHANNEL_CTR[channel] * RNG.lognormal(-0.008, 0.12, day_count)
            impressions = np.round(spend / cpm * 1000).astype(int)
            clicks = np.round(impressions * ctr).astype(int)

            frames.append(
                pd.DataFrame(
                    {
                        "spend_date": days,
                        "channel": channel,
                        "category": category,
                        "spend_usd": np.round(spend, 2),
                        "impressions": impressions,
                        "clicks": clicks,
                    }
                )
            )

    return pd.concat(frames, ignore_index=True)


def main():
    spend = build_spend()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    spend.to_csv(RAW_DIR / "marketing_spend.csv", index=False)

    print(f"marketing_spend.csv {len(spend):>7,} rows")
    for override in SPEND_OVERRIDES:
        window = spend[
            (spend["spend_date"] >= override["start"])
            & (spend["spend_date"] <= override["end"])
        ]
        for key, value in override["filters"].items():
            window = window[window[key] == value]
        print(
            f"  {override['anomaly_id']} spend {override['start']}..{override['end']} "
            f"x{override['multiplier']} -> {window['spend_usd'].sum():,.2f} USD "
            f"over {len(window)} rows"
        )


if __name__ == "__main__":
    main()
