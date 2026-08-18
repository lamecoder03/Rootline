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

# --- Function denylist ------------------------------------------------------------------
# Matched against AST function nodes, not against the query text. These read files, open network
# connections, execute nested SQL from a string, or burn wall-clock — none of which the table
# allowlist can see, because none of them reference a table. The read-only role blocks them a
# second time (they need privileges it does not have); this layer refuses them earlier.
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
# Hard stop on tool calls per investigation. Sized from the Day 8 plan: roughly a dozen queries
# to characterise one anomaly, doubled for slack. The cap is not a cost control, it is a
# termination guarantee — a model that loops on a failing query stops rather than runs forever.
MAX_TOOL_CALLS = 25

# --- Names the provisioner and the audit writer share ------------------------------------
AGENT_ROLE_ENV_USER = "AGENT_DB_USER"
AGENT_ROLE_ENV_PASSWORD = "AGENT_DB_PASSWORD"

AUDIT_SCHEMA = "audit"
AUDIT_TABLE = "agent_tool_calls"
AUDIT_QUALIFIED = f"{AUDIT_SCHEMA}.{AUDIT_TABLE}"

# Schemas the agent role must never reach. Held here so the isolation check tests exactly what
# the design claims, rather than whatever happens to exist at the time.
FORBIDDEN_SCHEMAS = ("raw", "staging", "intermediate")
