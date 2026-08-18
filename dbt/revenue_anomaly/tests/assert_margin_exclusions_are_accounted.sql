-- Fails if a SKU without a unit_cost was quietly folded into the margin numbers, or if the
-- exclusion count a margin row advertises does not match the dimension it came from.
-- Exists because the whole point of the is_margin_calculable flag is that the gap stays
-- visible: an exclusion that is not counted is the same as a silent drop.
-- Cross-checks dim_product against int_category_cost_basis and fct_daily_margin.

with dimension_counts as (

    select
        category,
        count(*)                                             as skus_in_category,
        count(*) filter (where not is_margin_calculable)     as uncosted_skus
    from {{ ref('dim_product') }}
    group by category

),

cost_basis_disagrees_with_dimension as (

    select
        'category ' || dimension_counts.category
        || ': dimension says ' || dimension_counts.uncosted_skus
        || ' uncosted, cost basis says '
        || cost_basis.skus_excluded_from_cost_basis                     as failure
    from dimension_counts
    inner join {{ ref('int_category_cost_basis') }} as cost_basis
        on dimension_counts.category = cost_basis.category
    where dimension_counts.uncosted_skus <> cost_basis.skus_excluded_from_cost_basis
       or dimension_counts.skus_in_category <> cost_basis.skus_in_category

),

margin_row_hides_the_gap as (

    select distinct
        'category ' || category || ' margin rows do not carry an exclusion count' as failure
    from {{ ref('fct_daily_margin') }}
    where skus_excluded_from_cost_basis is null
       or skus_in_category is null
       or cost_basis_coverage_pct is null

),

margin_reported_without_a_cost_basis as (

    select distinct
        'category ' || category || ' reports a margin with no costed SKU behind it' as failure
    from {{ ref('fct_daily_margin') }}
    where estimated_gross_margin is not null
      and not is_margin_estimable

),

cost_basis_used_an_uncosted_sku as (

    select
        'category ' || category || ' average unit cost is null despite '
        || skus_with_unit_cost || ' costed SKUs'                        as failure
    from {{ ref('int_category_cost_basis') }}
    where skus_with_unit_cost > 0
      and avg_unit_cost is null

)

select failure from cost_basis_disagrees_with_dimension
union all
select failure from margin_row_hides_the_gap
union all
select failure from margin_reported_without_a_cost_basis
union all
select failure from cost_basis_used_an_uncosted_sku
