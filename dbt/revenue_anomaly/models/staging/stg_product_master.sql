-- Resolves the 158-row two-system product master into 120 clean, one-row-per-SKU products.
-- Exists because every category rollup downstream needs one canonical category set and one
-- SKU key; the raw table has 24 category spellings and case/whitespace-scrambled keys.
-- Merges the best non-null value per column in rank order, so no field is lost to a loser row.

with ranked as (

    select * from {{ ref('stg_product_master_ranked') }}

),

merged as (

    select
        sku_id,

        (array_agg(product_name    order by source_row_rank)
            filter (where product_name    is not null))[1]  as product_name,
        (array_agg(category_mapped order by source_row_rank)
            filter (where category_mapped is not null))[1]  as category_mapped,
        (array_agg(subcategory     order by source_row_rank)
            filter (where subcategory     is not null))[1]  as subcategory,
        (array_agg(unit_cost       order by source_row_rank)
            filter (where unit_cost       is not null))[1]  as unit_cost,
        (array_agg(supplier        order by source_row_rank)
            filter (where supplier        is not null))[1]  as supplier,
        (array_agg(primary_region  order by source_row_rank)
            filter (where primary_region  is not null))[1]  as primary_region,
        (array_agg(launch_date     order by source_row_rank)
            filter (where launch_date     is not null))[1]  as launch_date,

        count(*)                                            as source_row_count,
        count(distinct source_system)                       as source_system_count,
        max(extracted_at)                                   as last_extracted_at

    from ranked
    group by sku_id

),

final as (

    select
        merged.sku_id,
        merged.product_name,
        coalesce(
            merged.category_mapped,
            prefix_map.category_canonical,
            'Unknown'
        )                                                   as category,
        merged.subcategory,
        merged.unit_cost,
        merged.supplier,
        merged.primary_region,
        merged.launch_date,

        merged.source_row_count,
        merged.source_system_count,
        merged.last_extracted_at,

        merged.source_row_count > 1                         as was_deduplicated,
        merged.category_mapped is null                      as category_was_imputed,
        merged.unit_cost       is null                      as has_missing_unit_cost,
        merged.supplier        is null                      as has_missing_supplier,
        merged.product_name    is null                      as has_missing_product_name

    from merged
    left join {{ ref('sku_prefix_category_map') }} as prefix_map
        on split_part(merged.sku_id, '-', 1) = prefix_map.sku_prefix

)

select * from final
