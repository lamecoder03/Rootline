-- Fails if the revenue series has a hole: every established cell must appear on every day,
-- and the date sequence must have no gap.
-- Exists because a missing day reads as a revenue collapse to the z-score detector — it would
-- manufacture an anomaly that never happened. Absence is tested, not assumed.
-- A cell whose history STARTS late is a launch, not a hole: it is checked for internal
-- contiguity from its own first day instead, so a newly launched category cannot hide a gap.

with cell_spans as (

    select
        category, channel, region,
        min(order_date) as first_seen,
        max(order_date) as last_seen,
        count(*)        as observed_days
    from {{ ref('stg_daily_revenue') }}
    group by category, channel, region

),

series_end as (

    select max(order_date) as last_date from {{ ref('stg_daily_revenue') }}

),

-- Every cell must be contiguous from its own first day to the end of the series. This catches a
-- true mid-series hole (and a cell that stops reporting) without demanding that a category
-- launched three weeks ago have two years of history it never had.
cell_is_contiguous as (

    select
        'cell ' || category || ' | ' || channel || ' | ' || region
        || ' has ' || observed_days || ' rows but spans '
        || ((select last_date from series_end) - first_seen + 1) || ' days' as failure
    from cell_spans
    where last_seen = (select last_date from series_end)
      and observed_days <> (select last_date from series_end) - first_seen + 1

),

grain as (

    select
        (select count(distinct order_date) from {{ ref('stg_daily_revenue') }})    as day_count,
        (select count(*) from cell_spans
         where first_seen = (select min(order_date) from {{ ref('stg_daily_revenue') }}))
                                                                                  as cell_count,
        (select count(*) from {{ ref('stg_daily_revenue') }}
         where (category, channel, region) in (
            select category, channel, region from cell_spans
            where first_seen = (select min(order_date) from {{ ref('stg_daily_revenue') }})
         ))                                                                       as actual_row_count

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
union all
select failure from cell_is_contiguous
