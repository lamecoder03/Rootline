-- Preserves the duplication evidence that stg_product_master collapses away.
-- Exists because a dedupe that quietly discards 38 rows is precisely the kind of data loss a
-- reviewer must be able to audit; every discarded row stays inspectable here.
-- One row per raw row: its rank, whether it survived, and which field it donated to the merge.

{{ config(materialized='table') }}

with ranked as (

    select * from {{ ref('stg_product_master_ranked') }}

),

rows_per_source_system as (

    select
        sku_id,
        source_system,
        count(*)                                        as rows_in_source_system

    from ranked
    group by sku_id, source_system

),

group_stats as (

    select
        sku_id,
        sum(rows_in_source_system)                      as duplicate_group_size,
        count(*)                                        as distinct_source_system_count,
        max(rows_in_source_system)                      as max_rows_per_source_system

    from rows_per_source_system
    group by sku_id

),

contributions as (

    select
        sku_id,
        sku_id_source_value,
        source_system,
        extracted_at,
        category_raw,
        category_mapped,
        completeness_score,
        source_row_rank,

        min(source_row_rank) filter (where product_name    is not null)
            over (partition by sku_id)                  as product_name_from_rank,
        min(source_row_rank) filter (where category_mapped is not null)
            over (partition by sku_id)                  as category_from_rank,
        min(source_row_rank) filter (where unit_cost       is not null)
            over (partition by sku_id)                  as unit_cost_from_rank,
        min(source_row_rank) filter (where supplier        is not null)
            over (partition by sku_id)                  as supplier_from_rank,
        min(source_row_rank) filter (where launch_date     is not null)
            over (partition by sku_id)                  as launch_date_from_rank

    from ranked

),

final as (

    select
        md5(
            contributions.sku_id
            || '|' || cast(contributions.source_row_rank as text)
        )                                               as audit_key,
        contributions.sku_id,
        contributions.sku_id_source_value,
        contributions.source_system,
        contributions.extracted_at,
        contributions.category_raw,
        contributions.category_mapped,
        contributions.completeness_score,
        contributions.source_row_rank,

        contributions.source_row_rank = 1               as was_kept_as_primary,
        group_stats.duplicate_group_size,
        group_stats.distinct_source_system_count > 1    as has_cross_system_duplicate,
        group_stats.max_rows_per_source_system > 1      as has_intra_system_duplicate,

        coalesce(contributions.source_row_rank = contributions.product_name_from_rank, false)
                                                        as contributed_product_name,
        coalesce(contributions.source_row_rank = contributions.category_from_rank, false)
                                                        as contributed_category,
        coalesce(contributions.source_row_rank = contributions.unit_cost_from_rank, false)
                                                        as contributed_unit_cost,
        coalesce(contributions.source_row_rank = contributions.supplier_from_rank, false)
                                                        as contributed_supplier,
        coalesce(contributions.source_row_rank = contributions.launch_date_from_rank, false)
                                                        as contributed_launch_date

    from contributions
    inner join group_stats
        on contributions.sku_id = group_stats.sku_id

)

select * from final
