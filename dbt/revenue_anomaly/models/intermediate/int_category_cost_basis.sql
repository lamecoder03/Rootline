-- Rolls the SKU-grain product dimension up to category grain, carrying the cost-basis gap.
-- Exists because revenue is recorded per category, not per SKU: joining dim_product straight
-- onto a category-grain fact would fan one revenue row out to every SKU and multiply revenue.
-- Averages unit_cost over margin-calculable SKUs only, and counts exactly what it excluded.

with products as (

    select * from {{ ref('dim_product') }}

),

final as (

    select
        category,

        count(*)                                            as skus_in_category,
        count(*) filter (where is_margin_calculable)        as skus_with_unit_cost,
        count(*) filter (where not is_margin_calculable)    as skus_excluded_from_cost_basis,

        cast(
            avg(unit_cost) filter (where is_margin_calculable) as numeric(10, 2)
        )                                                   as avg_unit_cost,

        cast(
            100.0 * count(*) filter (where is_margin_calculable) / count(*) as numeric(5, 1)
        )                                                   as cost_basis_coverage_pct,

        count(*) filter (where is_margin_calculable) > 0     as is_margin_estimable,
        count(*) filter (where not is_margin_calculable) = 0 as is_fully_costed,

        string_agg(sku_id, ', ' order by sku_id)
            filter (where not is_margin_calculable)          as excluded_sku_ids

    from products
    group by category

)

select * from final
