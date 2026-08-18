-- Splits marketing spend from date x channel x category down to the revenue grain by region.
-- Exists because spend arrives one grain coarser than revenue, and joining them requires an
-- allocation rule -- a business assumption, not a cleaning step, so it lives in its own model.
-- Allocates on each region's TRAILING 28-day revenue share, deliberately excluding today.
--
-- Why trailing and not same-day: allocating on the current day's revenue would make spend
-- mechanically follow revenue. ANOM-02 is a West-region Electronics stockout with no spend
-- footprint at all -- it is the generator's negative control proving inventory, not marketing,
-- caused it. Same-day allocation would drag allocated West spend down alongside the revenue it
-- is supposed to explain, manufacturing a marketing cause for an inventory problem. A trailing
-- window keeps the allocation independent of the anomaly it is being used to investigate, and
-- matches how media budgets are actually planned: on last month's performance, not today's.
--
-- Pennies reconcile exactly: shares are rounded to cents, then the rounding residue is given to
-- the largest-share region (the largest remainder method), so each day's four regional amounts
-- sum to the source total to the cent. assert_spend_allocation_reconciles.sql proves it.

with revenue as (

    select * from {{ ref('stg_daily_revenue') }}

),

spend as (

    select * from {{ ref('stg_marketing_spend') }}

),

trailing_revenue as (

    select
        order_date,
        channel,
        category,
        region,
        sum(gross_revenue) over (
            partition by channel, category, region
            order by order_date
            rows between 28 preceding and 1 preceding
        )                                                   as trailing_revenue

    from revenue

),

region_shares as (

    select
        order_date,
        channel,
        category,
        region,
        coalesce(trailing_revenue, 0)                       as trailing_revenue,
        sum(coalesce(trailing_revenue, 0)) over (
            partition by order_date, channel, category
        )                                                   as trailing_revenue_all_regions

    from trailing_revenue

),

shares_resolved as (

    select
        order_date,
        channel,
        category,
        region,
        trailing_revenue,
        trailing_revenue_all_regions,
        case
            when trailing_revenue_all_regions > 0
                then trailing_revenue / trailing_revenue_all_regions
            else 0.25
        end                                                 as region_share,
        case
            when trailing_revenue_all_regions > 0
                then 'trailing_28d_revenue_share'
            else 'even_split_no_history'
        end                                                 as allocation_basis

    from region_shares

),

allocated_unadjusted as (

    select
        spend.spend_key,
        spend.spend_date,
        spend.channel,
        spend.category,
        shares_resolved.region,
        spend.spend_usd,
        spend.impressions,
        spend.clicks,
        shares_resolved.region_share,
        shares_resolved.allocation_basis,

        round(spend.spend_usd * shares_resolved.region_share, 2)        as spend_allocated_usd,
        round(spend.impressions * shares_resolved.region_share)         as impressions_allocated,
        round(spend.clicks * shares_resolved.region_share)              as clicks_allocated

    from spend
    inner join shares_resolved
        on  spend.spend_date = shares_resolved.order_date
        and spend.channel    = shares_resolved.channel
        and spend.category   = shares_resolved.category

),

with_residue as (

    select
        allocated_unadjusted.*,

        spend_usd   - sum(spend_allocated_usd)   over (partition by spend_key) as spend_residue,
        impressions - sum(impressions_allocated) over (partition by spend_key) as impressions_residue,
        clicks      - sum(clicks_allocated)      over (partition by spend_key) as clicks_residue,

        row_number() over (
            partition by spend_key
            order by region_share desc, region
        )                                                   as share_rank

    from allocated_unadjusted

),

final as (

    select
        md5(
            cast(spend_date as text)
            || '|' || channel
            || '|' || category
            || '|' || region
        )                                                   as spend_allocation_key,
        spend_key,
        spend_date,
        channel,
        category,
        region,

        spend_usd                                           as source_spend_usd,
        region_share,
        allocation_basis,

        cast(
            case when share_rank = 1
                 then spend_allocated_usd + spend_residue
                 else spend_allocated_usd
            end as numeric(12, 2)
        )                                                   as spend_allocated_usd,
        cast(
            case when share_rank = 1
                 then impressions_allocated + impressions_residue
                 else impressions_allocated
            end as integer
        )                                                   as impressions_allocated,
        cast(
            case when share_rank = 1
                 then clicks_allocated + clicks_residue
                 else clicks_allocated
            end as integer
        )                                                   as clicks_allocated,

        share_rank = 1                                      as carries_rounding_residue

    from with_residue

)

select * from final
