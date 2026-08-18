-- Estimated gross margin at the revenue grain, with the cost-basis gap visible on every row.
-- Exists because 8 of 120 SKUs have no unit_cost: rather than let those NULLs propagate quietly
-- into a margin figure, this mart costs only the SKUs it can and states on each row how many it
-- had to exclude, so nobody can read the margin without also seeing how complete its basis is.
-- Margin is an ESTIMATE: revenue is per category and cost is per SKU, so units are costed at the
-- category's average unit cost over costed SKUs only -- never at an invented per-SKU cost.

with revenue as (

    select * from {{ ref('fct_daily_revenue') }}

),

category_cost_basis as (

    select * from {{ ref('int_category_cost_basis') }}

),

final as (

    select
        revenue.revenue_key                                     as margin_key,
        revenue.order_date,
        revenue.category,
        revenue.channel,
        revenue.region,

        revenue.units,
        revenue.gross_revenue,

        category_cost_basis.avg_unit_cost                       as cost_basis_avg_unit_cost,

        case when category_cost_basis.is_margin_estimable
             then cast(
                 revenue.units * category_cost_basis.avg_unit_cost as numeric(14, 2)
             )
        end                                                     as estimated_cogs,

        case when category_cost_basis.is_margin_estimable
             then cast(
                 revenue.gross_revenue - (revenue.units * category_cost_basis.avg_unit_cost)
                 as numeric(14, 2)
             )
        end                                                     as estimated_gross_margin,

        case
            when category_cost_basis.is_margin_estimable and revenue.gross_revenue > 0
                then cast(
                    100.0
                    * (revenue.gross_revenue - (revenue.units * category_cost_basis.avg_unit_cost))
                    / revenue.gross_revenue
                    as numeric(6, 2)
                )
        end                                                     as estimated_gross_margin_pct,

        category_cost_basis.is_margin_estimable,
        category_cost_basis.is_fully_costed                     as cost_basis_is_complete,
        category_cost_basis.skus_in_category,
        category_cost_basis.skus_with_unit_cost,
        category_cost_basis.skus_excluded_from_cost_basis,
        category_cost_basis.cost_basis_coverage_pct,
        category_cost_basis.excluded_sku_ids

    from revenue
    inner join category_cost_basis
        on revenue.category = category_cost_basis.category

)

select * from final
