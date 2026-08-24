# Every guardrail constant in one place: the table allowlist, the row and call ceilings, and the
# names of the role, schemas and audit table the other modules depend on.
# Exists so the security boundary can be read and reviewed without reading any implementation,
# and so the validator, the provisioner and the tests provably agree on what is allowed.

# --- The allowlist ----------------------------------------------------------------------
# The six objects dbt and the detector publish into `analytics`. Deliberately written out by
# name rather than discovered from information_schema: an allowlist that reads the database is
# an allowlist the database can change. A new mart is a reviewed edit to this line, not an
# automatic grant of access.
ANALYTICS_SCHEMA = "analytics"

ALLOWED_TABLES = (
    "fct_daily_revenue",
    "fct_daily_margin",
    "fct_daily_stockout",
    "dim_product",
    "detected_anomalies",
    "detected_anomaly_points",
)

# Fully qualified form, which is what the validator compares against after it rewrites every
# table reference to be schema-qualified. Qualifying is the point: an unqualified
# `fct_daily_revenue` resolves through search_path, and search_path is attacker-influenceable.
ALLOWED_QUALIFIED = frozenset(f"{ANALYTICS_SCHEMA}.{name}" for name in ALLOWED_TABLES)

# --- The dialect ------------------------------------------------------------------------
# Parsing and re-serialising both happen in this dialect. Using one dialect for both is what
# makes the guarantee hold: the string that executes is the string that was inspected.
SQL_DIALECT = "postgres"

# --- Row ceiling ------------------------------------------------------------------------
# Injected when a query carries no LIMIT, and clamped down onto any larger one. Bounds the blast
# radius of a careless query: the agent summarises anomalies, it never needs 43,860 rows, and an
# unbounded result set is both a cost problem and a way to push the model past its context.
MAX_ROWS = 1000

# --- Function allowlist -----------------------------------------------------------------
# An ALLOWLIST, like tables and statement types. It replaced a denylist, which was the one
# asymmetry in the design and let six functions through in testing — pg_get_viewdef leaked the
# stockout view's body (naming staging tables the agent cannot query), txid_current forced a WAL
# write from a "read-only" role, and repeat() materialised 64MB in a single row.
#
# NAMES ARE sqlglot's, NOT POSTGRES'S. sqlglot normalises many functions to an internal node, so
# `to_char` parses as TimeToStr, `date_trunc` as TimestampTrunc and `string_agg` as GroupConcat.
# Listing the Postgres spelling would silently fail to permit the function. Each entry below was
# read off a real parse tree; the Postgres spelling is noted where the two differ.
ALLOWED_FUNCTIONS = frozenset({
    # Aggregates — the arithmetic an anomaly brief is made of.
    "sum", "avg", "count", "min", "max",
    "stddev", "stdev", "stddev_pop", "variance", "var_samp", "variance_samp",
    "percentile_cont", "percentile_disc", "corr",
    "array_agg", "group_concat",                    # group_concat <- string_agg

    # Maths — deltas, ratios and magnitudes.
    "abs", "ceil", "ceiling", "floor", "round", "greatest", "least",
    "sqrt", "pow", "power", "ln", "log", "exp", "sign", "signum",

    # Null and conditional handling — the 8 uncosted SKUs make this unavoidable.
    "coalesce", "ifnull", "nvl", "nullif", "if", "iif",

    # Dates — every fact is a daily time series.
    "extract",                                      # extract <- also date_part
    "timestamp_trunc",                              # timestamp_trunc <- date_trunc
    "time_to_str",                                  # time_to_str <- to_char
    "str_to_date",                                  # str_to_date <- to_date
    "current_date", "current_timestamp", "age",     # current_timestamp <- now

    # Strings — category, channel, region and cell_key are all text.
    "lower", "lcase", "upper", "ucase", "initcap",
    "trim", "btrim",                                # trim <- also ltrim / rtrim
    "substring", "substr", "concat", "replace", "left", "right", "split_part",
    "length", "char_length", "character_length", "len",
    "str_position",                                 # str_position <- position / strpos
    "md5",

    # Window functions — trailing comparisons and ranking, which the detector's output invites.
    "row_number", "rank", "dense_rank", "percent_rank", "cume_dist", "ntile",
    "lag", "lead", "first_value", "last_value",
})

# Deliberately absent, and each for a reason rather than by oversight:
#   repeat / pad (lpad, rpad)        - memory amplification. The 1,000-row cap multiplies this
#                                      rather than containing it, and statement_timeout does not
#                                      fire because the query is fast, not slow. Measured: 64MB
#                                      in one row.
#   exploding_generate_series        - row amplification; generate_series(1, 1e9).
#   regexp_replace / regexp_* / like - catastrophic backtracking is a real denial of service, and
#                                      the six marts hold conformed values that do not need regex.
#   pg_get_viewdef, pg_get_functiondef - leak object definitions, which name schemas the agent is
#                                      denied. No privilege is required to call them.
#   txid_current                     - assigns a transaction id and writes WAL.
#   current_setting, version, pg_backend_pid, inet_server_addr - server fingerprinting.
# Widening this list is a reviewed edit to this file, which is the whole point of an allowlist.

# Structural nodes sqlglot models as functions but which are SQL syntax, not callable functions.
# Refusing them would refuse `gross_revenue::numeric(14,2)` and `CASE WHEN ... END`.
#
# `And` / `Or` are here because sqlglot models the boolean connectives as Func subclasses too.
# Day 8 found this the hard way: with them absent, `WHERE category = 'X' AND region = 'Y'` was
# rejected as `Function and() is not on the allowlist` - every compound WHERE clause in the
# project. The Day 7 regression suite missed it because none of its 18 queries used two
# conditions. Comparison, arithmetic, BETWEEN, IN, LIKE and IS NULL are NOT Func nodes and were
# never affected. Adding these gives up nothing: a boolean connective reads no file and
# allocates no memory.
STRUCTURAL_FUNCTION_NODES = ("Cast", "TryCast", "Case", "And", "Or", "Xor", "Not")

# --- Function denylist (kept for the error message only) ---------------------------------
# The allowlist above already refuses everything here. This set survives so a known-dangerous
# call is reported as "pg_read_file() is not permitted" rather than the generic not-on-the-list
# message, which matters when the reason lands in the audit trail and in front of the model.
DENIED_FUNCTIONS = frozenset({
    "pg_read_file", "pg_read_binary_file", "pg_ls_dir", "pg_stat_file",
    "lo_import", "lo_export",
    "dblink", "dblink_exec", "dblink_connect",
    "query_to_xml", "table_to_xml", "database_to_xml",
    "pg_sleep", "pg_sleep_for", "pg_sleep_until",
    "pg_terminate_backend", "pg_cancel_backend",
    "set_config", "pg_reload_conf",
})

# --- Call ceiling -----------------------------------------------------------------------
# Hard stop on tool calls per investigation: a termination guarantee, not a cost control — a model
# that loops on a failing query stops rather than runs forever. The live value is measured, not
# estimated, and is defined once in agent/config.py; every caller reads it from there.
# Re-exported here so this file still states the whole boundary in one place.
from agent.config import MAX_TOOL_CALLS  # noqa: E402  (value: 8 — see agent/config.py:99-149)

# --- Names the provisioner and the audit writer share ------------------------------------
AGENT_ROLE_ENV_USER = "AGENT_DB_USER"
AGENT_ROLE_ENV_PASSWORD = "AGENT_DB_PASSWORD"

AUDIT_SCHEMA = "audit"
AUDIT_TABLE = "agent_tool_calls"
AUDIT_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"

# Schemas the agent role must never reach. Held here so the isolation check tests exactly what
# the design claims, rather than whatever happens to exist at the time.
FORBIDDEN_SCHEMAS = ("raw", "staging", "intermediate")
