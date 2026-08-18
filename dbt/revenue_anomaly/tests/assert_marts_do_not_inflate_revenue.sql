-- Fails if any mart join multiplied revenue instead of enriching it.
-- Exists because fct_daily_revenue joins a SKU-grain product dimension to a category-grain
-- fact: get that wrong and one revenue row fans out into 24, inflating revenue 24x while every
-- row still looks individually plausible. Row counts and money totals are checked against staging.

with revenue_totals as (

    select
        (select count(*)          from {{ ref('stg_daily_revenue') }}) as staging_rows,
        (select sum(gross_revenue) from {{ ref('stg_daily_revenue') }}) as staging_revenue,
        (select count(*)          from {{ ref('fct_daily_revenue') }}) as mart_rows,
        (select sum(gross_revenue) from {{ ref('fct_daily_revenue') }}) as mart_revenue

),

revenue_fact_inflated as (

    select
        'fct_daily_revenue has ' || mart_rows || ' rows / ' || mart_revenue
        || ' revenue vs staging ' || staging_rows || ' rows / ' || staging_revenue as failure
    from revenue_totals
    where mart_rows <> staging_rows
       or mart_revenue <> staging_revenue

),

margin_totals as (

    select
        (select count(*)           from {{ ref('fct_daily_revenue') }}) as revenue_rows,
        (select sum(gross_revenue) from {{ ref('fct_daily_revenue') }}) as revenue_total,
        (select count(*)           from {{ ref('fct_daily_margin') }})  as margin_rows,
        (select sum(gross_revenue) from {{ ref('fct_daily_margin') }})  as margin_total

),

margin_fact_inflated as (

    select
        'fct_daily_margin has ' || margin_rows || ' rows / ' || margin_total
        || ' revenue vs fact ' || revenue_rows || ' rows / ' || revenue_total as failure
    from margin_totals
    where margin_rows <> revenue_rows
       or margin_total <> revenue_total

),

stockout_grain as (

    select
        'fct_daily_stockout has ' || count(*) || ' rows but '
        || count(distinct stockout_key) || ' distinct keys' as failure
    from {{ ref('fct_daily_stockout') }}
    having count(*) <> count(distinct stockout_key)

)

select failure from revenue_fact_inflated
union all
select failure from margin_fact_inflated
union all
select failure from stockout_grain
