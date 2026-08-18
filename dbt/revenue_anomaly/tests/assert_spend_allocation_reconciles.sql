-- Fails if the region allocation created or destroyed a single cent of marketing spend.
-- Exists because allocating a coarser grain down to a finer one is the step most likely to
-- silently leak money, and a spend total that no longer ties to source makes every downstream
-- efficiency number wrong in a way nobody would notice.
-- Checks three things: per-cell reconciliation, the grand total, and that shares sum to 1.

with per_cell as (

    select
        spend_key,
        max(source_spend_usd)                                   as source_spend_usd,
        sum(spend_allocated_usd)                                as allocated_spend_usd,
        sum(region_share)                                       as total_region_share,
        count(*)                                                as region_count
    from {{ ref('int_marketing_spend_allocated') }}
    group by spend_key

),

cell_does_not_reconcile as (

    select
        'spend_key ' || spend_key
        || ' source ' || source_spend_usd
        || ' but allocated ' || allocated_spend_usd               as failure
    from per_cell
    where allocated_spend_usd <> source_spend_usd

),

shares_do_not_sum_to_one as (

    select
        'spend_key ' || spend_key
        || ' region shares sum to ' || total_region_share         as failure
    from per_cell
    where abs(total_region_share - 1) > 0.000001

),

wrong_region_count as (

    select
        'spend_key ' || spend_key
        || ' allocated across ' || region_count || ' regions, expected 4' as failure
    from per_cell
    where region_count <> 4

),

grand_total as (

    select
        (select sum(spend_usd)           from {{ ref('stg_marketing_spend') }})        as source_total,
        (select sum(spend_allocated_usd) from {{ ref('int_marketing_spend_allocated') }}) as allocated_total

),

grand_total_does_not_reconcile as (

    select
        'grand total: source ' || source_total
        || ' but allocated ' || allocated_total                   as failure
    from grand_total
    where source_total <> allocated_total

)

select failure from cell_does_not_reconcile
union all
select failure from shares_do_not_sum_to_one
union all
select failure from wrong_region_count
union all
select failure from grand_total_does_not_reconcile
