-- The core revenue fact at date x category x channel x region -- what Day 5's detector scans
-- and the Day 8 agent queries first when it is asked why revenue moved.
-- Exists so revenue, region-allocated marketing spend and calendar context sit on one grain,
-- letting a single row answer "what happened, and what was going on around it".
-- Every join is one-to-one or many-to-one: the product dimension is pre-aggregated to category
-- grain first, because joining it at SKU grain would fan one revenue row out into 24.

with revenue as (

    select * from {{ ref('stg_daily_revenue') }}

),

spend_allocated as (

    select * from {{ ref('int_marketing_spend_allocated') }}

),

category_cost_basis as (

    select * from {{ ref('int_category_cost_basis') }}

),

holidays as (

    select * from {{ ref('stg_holiday_calendar') }}

),

final as (

    select
        revenue.revenue_key,
        revenue.order_date,
        revenue.category,
        revenue.channel,
        revenue.region,

        revenue.orders,
        revenue.units,
        revenue.gross_revenue,

        coalesce(spend_allocated.spend_allocated_usd, 0)        as marketing_spend_usd,
        coalesce(spend_allocated.impressions_allocated, 0)      as impressions,
        coalesce(spend_allocated.clicks_allocated, 0)           as clicks,
        spend_allocated.region_share                            as spend_region_share,
        spend_allocated.allocation_basis                        as spend_allocation_basis,

        case
            when coalesce(spend_allocated.spend_allocated_usd, 0) > 0
                then cast(
                    revenue.gross_revenue / spend_allocated.spend_allocated_usd
                    as numeric(12, 4)
                )
        end                                                     as return_on_ad_spend,

        holidays.holiday_date is not null                       as is_holiday,
        holidays.holiday_name,
        coalesce(holidays.is_retail_event, false)               as is_retail_event,
        coalesce(holidays.retail_significance, 'none')          as retail_significance,

        trim(to_char(revenue.order_date, 'Day'))                as day_of_week,
        extract(isodow from revenue.order_date) in (6, 7)       as is_weekend,

        -- coalesced to 0 for a category with no SKUs registered yet (a new launch): there is no
        -- cost basis to exclude from, which is a known zero rather than an unknown.
        coalesce(category_cost_basis.skus_in_category, 0)        as skus_in_category,
        coalesce(category_cost_basis.skus_with_unit_cost, 0)     as skus_with_unit_cost,
        coalesce(category_cost_basis.skus_excluded_from_cost_basis, 0)
                                                                 as skus_excluded_from_cost_basis

    from revenue

    left join spend_allocated
        on  revenue.order_date = spend_allocated.spend_date
        and revenue.channel    = spend_allocated.channel
        and revenue.category   = spend_allocated.category
        and revenue.region     = spend_allocated.region

    left join holidays
        on revenue.order_date = holidays.holiday_date

    left join category_cost_basis
        on revenue.category = category_cost_basis.category

)

select * from final
