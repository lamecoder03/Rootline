-- The product dimension, one row per SKU, joined by every mart that needs product attributes.
-- Exists so the question "can this SKU be costed at all?" is answered in exactly one place:
-- 8 of 120 SKUs arrived with no unit_cost and are flagged here rather than silently dropped.
-- Passes stg_product_master through and derives is_margin_calculable from unit_cost.

with products as (

    select * from {{ ref('stg_product_master') }}

),

final as (

    select
        sku_id,
        product_name,
        category,
        subcategory,
        unit_cost,
        supplier,
        primary_region,
        launch_date,

        unit_cost is not null                               as is_margin_calculable,

        category_was_imputed,
        has_missing_unit_cost,
        has_missing_supplier,
        has_missing_product_name,

        was_deduplicated,
        source_row_count,
        source_system_count,
        last_extracted_at

    from products

)

select * from final
