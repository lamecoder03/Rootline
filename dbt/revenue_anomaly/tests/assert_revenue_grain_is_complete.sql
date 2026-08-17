-- Fails if the revenue series has a hole: every dimension cell must appear on every day,
-- and the date sequence must have no gap.
-- Exists because a missing day reads as a revenue collapse to the Day 5 z-score detector —
-- it would manufacture an anomaly that never happened. Absence is tested, not assumed.

with grain as (

    select
        (select count(distinct order_date) from {{ ref('stg_daily_revenue') }})    as day_count,
        (select count(*) from (
            select distinct category, channel, region from {{ ref('stg_daily_revenue') }}
        ) as cells)                                                               as cell_count,
        (select count(*) from {{ ref('stg_daily_revenue') }})                     as actual_row_count

),

cartesian_is_complete as (

    select
        'expected ' || (day_count * cell_count)
        || ' rows (' || day_count || ' days x ' || cell_count
        || ' cells) but found ' || actual_row_count as failure
    from grain
    where day_count * cell_count <> actual_row_count

),

distinct_days as (

    select distinct order_date from {{ ref('stg_daily_revenue') }}

),

date_sequence_has_no_gap as (

    select
        'gap in the date series before ' || order_date as failure
    from (
        select
            order_date,
            lag(order_date) over (order by order_date) as previous_date
        from distinct_days
    ) as sequenced
    where previous_date is not null
      and order_date - previous_date > 1

)

select failure from cartesian_is_complete
union all
select failure from date_sequence_has_no_gap
