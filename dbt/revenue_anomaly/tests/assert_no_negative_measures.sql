-- Fails if any measure that is physically incapable of being negative is negative.
-- Exists because a sign error in a cast is silent — it would flow into the z-score baseline
-- and shift the control limits before anyone noticed the number was impossible.
-- Returns the offending key per model; dbt fails the test when anything comes back.

select 'stg_daily_revenue' as model_name, revenue_key as offending_key
from {{ ref('stg_daily_revenue') }}
where gross_revenue < 0 or orders < 0 or units < 0

union all

select 'stg_marketing_spend', spend_key
from {{ ref('stg_marketing_spend') }}
where spend_usd < 0 or impressions < 0 or clicks < 0

union all

select 'stg_inventory_snapshot', inventory_key
from {{ ref('stg_inventory_snapshot') }}
where units_on_hand < 0 or reorder_point < 0

union all

select 'stg_product_master', sku_id
from {{ ref('stg_product_master') }}
where unit_cost < 0
