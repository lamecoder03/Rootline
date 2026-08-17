-- Types raw.marketing_spend into the spend fact at date x channel x category.
-- Exists as the spend side of root-cause analysis: it is what the agent checks first when
-- revenue moves, and it is the negative control for ANOM-02, which has no spend footprint.
-- Casts explicitly and keeps the source grain — the roll-up onto revenue is Day 4 mart work.

with source as (

    select * from {{ source('raw', 'marketing_spend') }}

),

cleaned as (

    select
        cast(spend_date as date)                as spend_date,
        trim(channel)                           as channel,
        trim(category)                          as category,
        cast(spend_usd as numeric(12, 2))       as spend_usd,
        cast(impressions as integer)            as impressions,
        cast(clicks as integer)                 as clicks

    from source

),

final as (

    select
        md5(
            cast(spend_date as text)
            || '|' || channel
            || '|' || category
        )                                       as spend_key,
        spend_date,
        channel,
        category,
        spend_usd,
        impressions,
        clicks

    from cleaned

)

select * from final
