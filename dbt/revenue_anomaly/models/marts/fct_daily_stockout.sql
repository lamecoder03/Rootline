-- Daily stock health at date x category x region -- the inventory evidence the agent
-- checks when revenue drops and marketing spend cannot account for it.
-- Exists because ANOM-02 has no spend footprint by design, so inventory is the only table that
-- can explain it; this rolls SKU-level stock up to a grain fct_daily_revenue joins to directly.
-- Join on (order_date, category, region); stock is not channel-specific, so it is many-to-one.

{{ config(materialized='view') }}

with inventory as (

    select * from {{ ref('stg_inventory_snapshot') }}

),

products as (

    select
        sku_id,
        category
    from {{ ref('dim_product') }}

),

joined as (

    select
        inventory.snapshot_date,
        products.category,
        inventory.region,
        inventory.sku_id,
        inventory.units_on_hand,
        inventory.reorder_point

    from inventory
    inner join products
        on inventory.sku_id = products.sku_id

),

final as (

    select
        md5(
            cast(snapshot_date as text)
            || '|' || category
            || '|' || region
        )                                                       as stockout_key,
        snapshot_date,
        category,
        region,

        count(*)                                                as skus_tracked,
        count(*) filter (where units_on_hand = 0)                as skus_out_of_stock,
        count(*) filter (where units_on_hand <= reorder_point)   as skus_below_reorder_point,
        sum(units_on_hand)                                       as total_units_on_hand,

        cast(
            100.0 * count(*) filter (where units_on_hand = 0) / count(*) as numeric(5, 1)
        )                                                        as stockout_rate_pct,

        count(*) filter (where units_on_hand = 0) > 0            as has_stockout,
        count(*) filter (where units_on_hand = 0) = count(*)     as is_total_stockout,

        string_agg(sku_id, ', ' order by sku_id)
            filter (where units_on_hand = 0)                     as stocked_out_sku_ids

    from joined
    group by snapshot_date, category, region

)

select * from final
