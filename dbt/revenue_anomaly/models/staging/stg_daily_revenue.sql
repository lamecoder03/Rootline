-- Types raw.daily_revenue into the revenue fact at date x category x channel x region.
-- Exists because marts, the z-score detector, and the agent all read revenue from
-- here, so the casts and the grain are declared once instead of in every consumer.
-- Casts every column explicitly, trims the dimension strings, and stamps an md5 grain key.

with source as (

    select * from {{ source('raw', 'daily_revenue') }}

),

cleaned as (

    select
        cast(order_date as date)                as order_date,
        trim(category)                          as category,
        trim(channel)                           as channel,
        trim(region)                            as region,
        cast(orders as integer)                 as orders,
        cast(units as integer)                  as units,
        cast(gross_revenue as numeric(14, 2))   as gross_revenue

    from source

),

final as (

    select
        md5(
            cast(order_date as text)
            || '|' || category
            || '|' || channel
            || '|' || region
        )                                       as revenue_key,
        order_date,
        category,
        channel,
        region,
        orders,
        units,
        gross_revenue

    from cleaned

)

select * from final
