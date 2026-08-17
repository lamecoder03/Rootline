-- Shared intermediate: normalises every raw product row and ranks a SKU's duplicates.
-- Exists so the deduplicated dimension and its audit trail derive from one ranking rule
-- instead of two copies of the logic that could silently drift apart.
-- Ephemeral — dbt inlines it as a CTE into both consumers and builds no database object.

{{ config(materialized='ephemeral') }}

with source as (

    select * from {{ source('raw', 'product_master') }}

),

normalised as (

    select
        upper(replace(trim(sku_id), '_', '-'))                          as sku_id,
        sku_id                                                          as sku_id_source_value,
        nullif(trim(product_name), '')                                  as product_name,
        category_raw,
        nullif(
            lower(regexp_replace(coalesce(category_raw, ''), '[^a-zA-Z0-9]', '', 'g')),
            ''
        )                                                               as category_match_key,
        nullif(trim(subcategory), '')                                   as subcategory,
        cast(unit_cost as numeric(10, 2))                               as unit_cost,
        nullif(trim(supplier), '')                                      as supplier,
        nullif(trim(primary_region), '')                                as primary_region,
        case
            when trim(launch_date_raw) ~ '^\d{4}-\d{2}-\d{2}$'
                then to_date(trim(launch_date_raw), 'YYYY-MM-DD')
            when trim(launch_date_raw) ~ '^\d{1,2}/\d{1,2}/\d{4}$'
                then to_date(trim(launch_date_raw), 'MM/DD/YYYY')
        end                                                             as launch_date,
        launch_date_raw,
        trim(source_system)                                             as source_system,
        cast(extracted_at as date)                                      as extracted_at

    from source

),

mapped as (

    select
        normalised.*,
        alias_map.category_canonical                                    as category_mapped

    from normalised
    left join {{ ref('category_alias_map') }} as alias_map
        on normalised.category_match_key = alias_map.category_match_key

),

scored as (

    select
        mapped.*,
        (
            cast((category_mapped is not null) as integer)
            + cast((unit_cost    is not null) as integer)
            + cast((product_name is not null) as integer)
            + cast((supplier     is not null) as integer)
            + cast((launch_date  is not null) as integer)
        )                                                               as completeness_score

    from mapped

),

ranked as (

    select
        scored.*,
        row_number() over (
            partition by sku_id
            order by
                completeness_score desc,
                extracted_at desc,
                source_system,
                md5(
                    coalesce(sku_id_source_value, '')
                    || coalesce(product_name, '')
                    || coalesce(category_raw, '')
                    || coalesce(cast(unit_cost as text), '')
                    || coalesce(supplier, '')
                    || coalesce(launch_date_raw, '')
                )
        )                                                               as source_row_rank

    from scored

)

select * from ranked
