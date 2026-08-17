-- Types raw.inventory_snapshot into daily stock levels per SKU.
-- Exists so the agent can test an inventory explanation for a revenue dip — the only
-- evidence that explains ANOM-02, which is invisible in the spend table by design.
-- Casts explicitly and normalises sku_id with the same rule stg_product_master uses.

with source as (

    select * from {{ source('raw', 'inventory_snapshot') }}

),

cleaned as (

    select
        cast(snapshot_date as date)                     as snapshot_date,
        upper(replace(trim(sku_id), '_', '-'))          as sku_id,
        trim(region)                                    as region,
        cast(units_on_hand as integer)                  as units_on_hand,
        cast(reorder_point as integer)                  as reorder_point

    from source

),

final as (

    select
        md5(
            cast(snapshot_date as text)
            || '|' || sku_id
        )                                               as inventory_key,
        snapshot_date,
        sku_id,
        region,
        units_on_hand,
        reorder_point

    from cleaned

)

select * from final
