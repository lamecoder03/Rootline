-- Types raw.holiday_calendar into the external-factor calendar keyed on holiday_date.
-- Exists so a revenue swing can be ruled out as a calendar effect before anything is
-- reported as an incident — most of the largest moves in this data are holidays.
-- Casts explicitly and lowercases retail_significance so it is safe to compare against.

with source as (

    select * from {{ source('raw', 'holiday_calendar') }}

),

final as (

    select
        cast(holiday_date as date)                      as holiday_date,
        trim(holiday_name)                              as holiday_name,
        upper(trim(country_code))                       as country_code,
        cast(is_federal_holiday as boolean)             as is_federal_holiday,
        cast(is_retail_event as boolean)                as is_retail_event,
        lower(trim(retail_significance))                as retail_significance,
        trim(day_of_week)                               as day_of_week

    from source

)

select * from final
