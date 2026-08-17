{#
    Overrides dbt's default schema naming so a model's custom schema is used verbatim.
    Exists because the default prepends the target schema, yielding `analytics_staging` and
    blurring the boundary the agent's read-only role will be granted on.
    Result: staging models land in `staging`, Day 4's marts land in `analytics`.
#}

{% macro generate_schema_name(custom_schema_name, node) -%}

    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}

{%- endmacro %}
