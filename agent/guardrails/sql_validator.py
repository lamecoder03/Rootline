# Parses every query the agent generates into a syntax tree and refuses it unless it is one
# read-only SELECT against an allowlisted analytics table, then rewrites it with a row cap.
# Exists because a keyword blocklist is not a security control: it cannot see a DROP hidden in a
# second statement, and it rejects a column honestly named `delete_reason`.
# Structural facts about the parsed tree are checked, never the text of the query.

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from . import config as cfg

# sqlglot logs a warning and degrades to a generic Command node for syntax it does not model
# (EXPLAIN, VACUUM, SET). That degradation is exactly what the statement-type check catches, so
# the warning is noise on a path that is already handled.
logging.getLogger("sqlglot").setLevel(logging.ERROR)


class SqlValidationError(Exception):
    """Raised when a query is refused. The message is written to be logged verbatim into the
    audit trail and handed back to the model as the reason its call failed."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class ValidationResult:
    """What the executor needs and nothing else: the SQL to run, plus the evidence of what was
    inspected so the audit row records a decision rather than an assertion."""

    sql: str
    tables: tuple = ()
    limit_applied: int = 0
    limit_was_injected: bool = False
    notes: list = field(default_factory=list)


def _fail(code, message):
    raise SqlValidationError(code, message)


def _parse_single_statement(sql):
    """Rejects multi-statement input structurally. sqlglot returns one expression per statement,
    so a semicolon-chained injection arrives as a list of length two and is refused on the count
    - no scan for semicolons, which would also reject a legitimate literal containing one."""
    if not sql or not sql.strip():
        _fail("EMPTY_QUERY", "Query is empty.")

    try:
        statements = sqlglot.parse(sql, read=cfg.SQL_DIALECT)
    except ParseError as error:
        _fail("PARSE_ERROR", f"Query is not valid {cfg.SQL_DIALECT} SQL: {error}")

    statements = [statement for statement in statements if statement is not None]

    if not statements:
        _fail("EMPTY_QUERY", "Query parsed to no statement.")
    if len(statements) > 1:
        kinds = ", ".join(type(statement).__name__.upper() for statement in statements)
        _fail(
            "MULTI_STATEMENT",
            f"Only one statement is allowed; got {len(statements)} ({kinds}). "
            "Multi-statement input is refused outright.",
        )
    return statements[0]


def _require_plain_select(statement):
    """The top node must be a SELECT and nothing else. UNION, subquery-wrapped selects and
    anything sqlglot could not model land on other node types and are refused, which keeps the
    rule one line long: if it is not exp.Select, it does not run."""
    if isinstance(statement, exp.Command):
        _fail(
            "UNSUPPORTED_STATEMENT",
            "Statement is not a SELECT (parsed as an unsupported command: "
            f"{statement.name or statement.this}).",
        )
    if not isinstance(statement, exp.Select):
        _fail("NOT_A_SELECT", f"Only SELECT is allowed; got {type(statement).__name__.upper()}.")


def _reject_write_clauses(statement):
    """SELECT is not automatically read-only in Postgres. `SELECT ... INTO new_table` creates a
    table and `FOR UPDATE` takes row locks, and both parse as exp.Select - so both would sail
    past a check that only looked at the statement type."""
    if statement.args.get("into"):
        _fail(
            "SELECT_INTO",
            "SELECT ... INTO creates a table and is refused; it is a write disguised as a read.",
        )
    if statement.args.get("locks"):
        _fail(
            "ROW_LOCK",
            "Locking clauses (FOR UPDATE / FOR SHARE) are refused; a read must not take locks.",
        )
    for node_type, label in (
        (exp.Insert, "INSERT"), (exp.Update, "UPDATE"), (exp.Delete, "DELETE"),
        (exp.Drop, "DROP"), (exp.Create, "CREATE"), (exp.Alter, "ALTER"),
        (exp.TruncateTable, "TRUNCATE"), (exp.Grant, "GRANT"), (exp.Copy, "COPY"),
    ):
        if statement.find(node_type):
            _fail("NESTED_WRITE", f"A nested {label} was found inside the query and is refused.")


def _function_names(node):
    """The names a function node could be written as, lowercased. Anonymous nodes are functions
    sqlglot does not model, so the raw name is all there is; modelled nodes carry sqlglot's own
    names, which are frequently NOT the Postgres spelling - to_char parses as TimeToStr."""
    raw = {node.name} if isinstance(node, exp.Anonymous) else set(node.sql_names())
    return {name.lower() for name in raw if name}


def _require_allowed_functions(statement):
    """Allowlist, matched against AST function nodes rather than query text. A file read, a
    dblink call or a 64MB repeat() references no table, so the table allowlist cannot see any of
    them - and a denylist here only stops the ones somebody thought of first."""
    for node in statement.find_all(exp.Func):
        if type(node).__name__ in cfg.STRUCTURAL_FUNCTION_NODES:
            continue

        names = _function_names(node)
        if names & cfg.ALLOWED_FUNCTIONS:
            continue

        denied = sorted(names & cfg.DENIED_FUNCTIONS)
        if denied:
            _fail("DENIED_FUNCTION", f"Function {denied[0]}() is not permitted.")

        shown = sorted(names)[0] if names else type(node).__name__.lower()
        _fail(
            "FUNCTION_NOT_ALLOWED",
            f"Function {shown}() is not on the allowlist. Permitted functions cover aggregates, "
            "maths, null handling, dates, strings and window functions.",
        )


def _cte_names(statement):
    """CTE aliases look like table references in the tree but are not database objects, so they
    are collected here and excused from the allowlist. The real tables inside each CTE body are
    still walked, which is what stops a raw-schema read hidden in a WITH clause slipping by."""
    return {cte.alias_or_name.lower() for cte in statement.find_all(exp.CTE)}


def _qualify_and_check_tables(statement):
    """Rewrites every table reference to be schema-qualified, then compares it to the allowlist.
    Qualifying first is the security step, not a tidiness step: an unqualified name is resolved
    at execution time through search_path, so validating one and executing the other would leave
    a gap wide enough to redirect `dim_product` at a different schema entirely."""
    allowed_bare = set(cfg.ALLOWED_TABLES)
    cte_aliases = _cte_names(statement)
    referenced = []

    for table in statement.find_all(exp.Table):
        name = table.name.lower()
        schema = (table.db or "").lower()
        catalog = (table.catalog or "").lower()

        if not schema and name in cte_aliases:
            continue

        if catalog:
            _fail("CROSS_DATABASE", f"Cross-database reference {catalog}.{schema}.{name} is refused.")

        if not schema:
            if name not in allowed_bare:
                _fail(
                    "TABLE_NOT_ALLOWED",
                    f"Table '{name}' is not on the allowlist. "
                    f"Allowed: {', '.join(sorted(cfg.ALLOWED_QUALIFIED))}.",
                )
            table.set("db", exp.to_identifier(cfg.ANALYTICS_SCHEMA))
            schema = cfg.ANALYTICS_SCHEMA

        qualified = f"{schema}.{name}"
        if qualified not in cfg.ALLOWED_QUALIFIED:
            _fail(
                "TABLE_NOT_ALLOWED",
                f"Table '{qualified}' is not on the allowlist. "
                f"Allowed: {', '.join(sorted(cfg.ALLOWED_QUALIFIED))}.",
            )
        referenced.append(qualified)

    if not referenced:
        _fail(
            "NO_TABLE",
            "Query references no allowlisted table; a tool call must read from the warehouse.",
        )
    return tuple(sorted(set(referenced)))


def _apply_row_cap(statement, max_rows):
    """Injects LIMIT when there is none, and clamps a larger or non-literal one down to the cap.
    Clamping matters as much as injecting: `LIMIT 10000000` is technically a limit, and a limit
    the model chose is not a limit the guardrail imposed."""
    limit = statement.args.get("limit")
    current = None
    if limit is not None:
        expression = limit.expression
        if isinstance(expression, exp.Literal) and expression.is_int:
            current = int(expression.name)

    if current is not None and current <= max_rows:
        return statement, current, False

    return statement.limit(max_rows), max_rows, limit is None


def validate(sql, max_rows=None):
    """The one entry point. Returns the rewritten, capped SQL to execute, or raises
    SqlValidationError with a code and a human reason for the audit trail."""
    max_rows = cfg.MAX_ROWS if max_rows is None else max_rows

    statement = _parse_single_statement(sql)
    _require_plain_select(statement)
    _reject_write_clauses(statement)
    _require_allowed_functions(statement)
    tables = _qualify_and_check_tables(statement)
    statement, limit_applied, injected = _apply_row_cap(statement, max_rows)

    notes = []
    if injected:
        notes.append(f"LIMIT {limit_applied} injected (query had none).")
    elif limit_applied == max_rows:
        notes.append(f"LIMIT clamped down to the {max_rows}-row ceiling.")

    return ValidationResult(
        sql=statement.sql(dialect=cfg.SQL_DIALECT),
        tables=tables,
        limit_applied=limit_applied,
        limit_was_injected=injected,
        notes=notes,
    )
