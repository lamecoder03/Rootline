-- Fails if the dedupe lost a row: the audit must account for every raw product row, every
-- SKU must have exactly one survivor, and the dimension must have one row per audited SKU.
-- Exists because 158 -> 120 is the one step in this layer that legitimately destroys rows,
-- so the arithmetic of what it destroyed is asserted rather than trusted.

with audit_row_count as (

    select count(*) as row_count from {{ ref('stg_product_master_dedup_audit') }}

),

source_row_count as (

    select count(*) as row_count from {{ source('raw', 'product_master') }}

),

dimension_row_count as (

    select count(*) as row_count from {{ ref('stg_product_master') }}

),

audited_sku_count as (

    select count(distinct sku_id) as row_count from {{ ref('stg_product_master_dedup_audit') }}

),

every_source_row_is_audited as (

    select
        'audit has ' || audit_row_count.row_count
        || ' rows but raw.product_master has ' || source_row_count.row_count as failure
    from audit_row_count, source_row_count
    where audit_row_count.row_count <> source_row_count.row_count

),

exactly_one_survivor_per_sku as (

    select
        'sku ' || sku_id || ' kept '
        || count(*) filter (where was_kept_as_primary) || ' primary rows' as failure
    from {{ ref('stg_product_master_dedup_audit') }}
    group by sku_id
    having count(*) filter (where was_kept_as_primary) <> 1

),

dimension_matches_audited_skus as (

    select
        'dimension has ' || dimension_row_count.row_count
        || ' rows but audit covers ' || audited_sku_count.row_count || ' distinct SKUs' as failure
    from dimension_row_count, audited_sku_count
    where dimension_row_count.row_count <> audited_sku_count.row_count

)

select failure from every_source_row_is_audited
union all
select failure from exactly_one_survivor_per_sku
union all
select failure from dimension_matches_audited_skus
