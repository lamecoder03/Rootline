# Day 7 — Agent guardrails

The walls, built before anything exists that could test them. Day 8 adds the Anthropic tool-use
loop; today's job is to make sure that when it arrives, the worst it can do is read six tables
and write a log line.

The four guardrails from `CLAUDE.md`, in the order a query meets them:

| # | Guardrail | Enforced by | Stops |
|---|---|---|---|
| 1 | Tool-call ceiling | `agent/guardrails/call_budget.py` | Unbounded loops |
| 2 | SQL validator | `agent/guardrails/sql_validator.py` (sqlglot AST) | Writes, injections, off-limits tables |
| 3 | Read-only role | Postgres grants (`agent/guardrails/provision.py`) | Everything the validator missed |
| 4 | Append-only audit | `audit.agent_tool_calls` + grants + trigger | Anything happening unobserved |

They are deliberately redundant. Guardrail 2 is application code, and application code is the
part an attacker gets to influence — so guardrail 3 assumes it has already failed. Every attack in
section 5 below was fired **twice**: once through the validator, and once straight down the
database connection with the validator removed.

---

## 1. The read-only role

### What was built

A new login role, `revenue_agent`, created by `python -m agent.guardrails.provision`. It is not a
reuse of `revenue_ops` — the owner is a superuser that dbt and the loader need, and sharing it
would make every guardrail below decorative.

The role starts with the empty privilege set and is granted upward:

```
CREATE ROLE revenue_agent WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
                               NOREPLICATION NOBYPASSRLS PASSWORD ...

GRANT CONNECT ON DATABASE revenue_anomaly TO revenue_agent
GRANT USAGE   ON SCHEMA analytics         TO revenue_agent
GRANT SELECT  ON ALL TABLES IN SCHEMA analytics TO revenue_agent
GRANT USAGE   ON SCHEMA audit             TO revenue_agent
GRANT INSERT  ON TABLE audit.agent_tool_calls   TO revenue_agent
```

Three things in there are not obvious and are the reason this is a script rather than a paragraph
in a README.

**`ALTER DEFAULT PRIVILEGES` is what keeps the grant alive.** A grant is attached to an object, so
it dies with the object. dbt materialises marts with `CREATE TABLE AS`, which means every
`dbt build` destroys the old table and its grants and creates a new one with none. Without this
line the agent would work until the next DAG run and then silently lose access:

```
ALTER DEFAULT PRIVILEGES FOR ROLE revenue_ops IN SCHEMA analytics
    GRANT SELECT ON TABLES TO revenue_agent
```

Verified rather than assumed — a full `run_dbt.bat build` was run, which dropped and recreated
every object in `analytics` (191 tests, all passing), and the agent's reads were retested
afterwards:

```
AFTER a full dbt build that dropped and recreated every analytics object:
  analytics.fct_daily_revenue           43,860 rows  -- readable
  analytics.fct_daily_margin            43,860 rows  -- readable
  analytics.fct_daily_stockout          14,620 rows  -- readable
  analytics.dim_product                    120 rows  -- readable
  analytics.detected_anomalies              44 rows  -- readable
  analytics.detected_anomaly_points        166 rows  -- readable
```

**`PUBLIC` had to be revoked from, not just skipped.** `PUBLIC` is a pseudo-role every login
inherits, and by default it holds `CREATE` and `TEMPORARY` on the database. Those were never
granted to `revenue_agent`, so there is nothing to revoke *from the role* — the only way to take
them away is to revoke them from `PUBLIC`:

```
REVOKE CREATE, TEMPORARY ON DATABASE revenue_anomaly FROM PUBLIC
REVOKE ALL ON SCHEMA public FROM PUBLIC
```

Without that line, `CREATE TEMP TABLE scratch (x int)` succeeds. It is now attack 2.6 below, and
it fails.

**`search_path` is pinned on the role.** `ALTER ROLE revenue_agent SET search_path = analytics,
pg_catalog`. `search_path` decides what an unqualified table name resolves to at execution time,
which makes it a redirection primitive. Pinning it here is the second half of a pair — the
validator schema-qualifies every table reference before the query runs, so neither layer relies on
the other.

A `statement_timeout` of 30s is also set. That is a resource guard, not a security boundary — the
role can raise it with `SET` — and it is written down as such. `default_transaction_read_only` was
considered and deliberately **not** set: it is equally overridable, so it buys the appearance of a
control rather than the substance, and it would block the audit `INSERT` that the same connection
has to make.

### Verifying the denial, not assuming it

The brief was explicit that granting `analytics` and assuming the rest is denied is not good
enough. Two independent confirmations.

**The database's own view of the privileges** (`has_schema_privilege` / `has_table_privilege`,
which is what Postgres itself consults):

```
    schema    | usage | create
--------------+-------+--------
 analytics    | t     | f
 audit        | t     | f
 intermediate | f     | f
 public       | f     | f
 raw          | f     | f
 staging      | f     | f
```

```
    schema    |             object             | sel | ins | upd | del
--------------+--------------------------------+-----+-----+-----+-----
 analytics    | detected_anomalies             | t   | f   | f   | f
 analytics    | detected_anomaly_points        | t   | f   | f   | f
 analytics    | dim_product                    | t   | f   | f   | f
 analytics    | fct_daily_margin               | t   | f   | f   | f
 analytics    | fct_daily_revenue              | t   | f   | f   | f
 analytics    | fct_daily_stockout             | t   | f   | f   | f
 audit        | agent_tool_calls               | f   | t   | f   | f
 intermediate | int_category_cost_basis        | f   | f   | f   | f
 intermediate | int_marketing_spend_allocated  | f   | f   | f   | f
 raw          | daily_revenue                  | f   | f   | f   | f
 raw          | holiday_calendar               | f   | f   | f   | f
 raw          | inventory_snapshot             | f   | f   | f   | f
 raw          | marketing_spend                | f   | f   | f   | f
 raw          | product_master                 | f   | f   | f   | f
 staging      | category_alias_map             | f   | f   | f   | f
 staging      | sku_prefix_category_map        | f   | f   | f   | f
 staging      | stg_daily_revenue              | f   | f   | f   | f
 staging      | stg_holiday_calendar           | f   | f   | f   | f
 staging      | stg_inventory_snapshot         | f   | f   | f   | f
 staging      | stg_marketing_spend            | f   | f   | f   | f
 staging      | stg_product_master             | f   | f   | f   | f
 staging      | stg_product_master_dedup_audit | f   | f   | f   | f
```

Every object outside `analytics` is `f` across the board, and the audit table is `INSERT` only.

**A live attempt against all fourteen forbidden objects** (5 raw + 6 staging + 2 intermediate +
the audit table), because a privilege bit and a refused query are not the same evidence.
Section 5.1 below has the transcript; all fourteen return `permission denied`.

### What this role can still do — stated honestly

Three things a reader should not be misled about:

1. **`analytics.fct_daily_stockout` is a view that reads `staging` underneath.** Postgres runs a
   view with its *owner's* privileges, so the agent gets the rows even though it cannot query
   `staging` directly. This is intended: a curated view is the controlled way to expose derived
   data. It is a read-through of exactly the columns and rows the view defines, not arbitrary SQL
   against staging. Confirmed: the agent reads 14,620 rows from it.
2. **Object *names* in other schemas are visible through `pg_catalog`.** Postgres lets every role
   read `pg_class`, so a direct connection can count 5 tables in `raw`. Their **contents and
   column names are not** — `information_schema.columns` filters to objects you hold privileges
   on and returns 0 rows for `raw`, and `pg_authid` (password hashes) is denied. On the agent's
   own path this is moot anyway: the validator refuses `pg_catalog.*` as `TABLE_NOT_ALLOWED`.
3. **The password lives in `.env`**, which is gitignored, with a placeholder in `.env.example`.
   That is the same contract the rest of the project uses; it is not a secrets manager, and a real
   deployment would want one.

---

## 2. The sqlglot validator

### Why an AST and not a keyword list

A keyword blocklist fails in both directions, and both failures are demonstrated in the tests:

- **It misses.** `SELECT category FROM fct_daily_revenue; DROP TABLE analytics.dim_product` contains
  a perfectly ordinary `SELECT`. A blocklist scanning for `DROP` catches this one, but not
  `SELECT * INTO analytics.exfiltrated FROM analytics.dim_product`, which contains no forbidden
  keyword at all and creates a table.
- **It false-positives.** `SELECT sku_id AS drop_reason FROM analytics.dim_product` is a legitimate
  query that a text scan for `drop` refuses. `WHERE product_name = 'DELETE FROM x'` is a string
  literal. So is a semicolon inside `WHERE sku_id = 'a;b'`, which a semicolon-splitter would read
  as two statements.

Parsing turns all of these from string questions into structural ones. "Is this one statement?" is
`len(sqlglot.parse(sql)) == 1`. "Does it write?" is "is the root node an `exp.Select`". Neither
question can be confused by a literal.

### Why the postgres dialect is set explicitly

`sqlglot.parse(sql, read="postgres")` — and `expression.sql(dialect="postgres")` on the way out.
Three reasons, in increasing order of how much they matter:

1. **Valid queries would be rejected.** Postgres-specific syntax — `::` casts, `ILIKE`,
   `DISTINCT ON`, `E''` escapes, dollar-quoting — is not in the generic grammar. A parse error on a
   legitimate analyst query is a broken agent.
2. **Constructs would be mis-modelled.** The danger is not a loud parse failure, it is a quiet
   mis-parse in which a table reference ends up somewhere the allowlist walk does not look. A table
   the validator cannot see is a table the validator cannot refuse.
3. **The string that runs must be the string that was checked.** The validator does not pass the
   original text through — it re-serialises the tree it inspected. If it parsed in one dialect and
   emitted in another, the rewrite could reintroduce something the check had cleared. One dialect
   in, the same dialect out, and the guarantee holds end to end.

### The checks, in order

| Order | Check | Refusal code |
|---|---|---|
| 1 | Non-empty, parses as valid Postgres | `EMPTY_QUERY`, `PARSE_ERROR` |
| 2 | Exactly one statement | `MULTI_STATEMENT` |
| 3 | Root node is `exp.Select` | `NOT_A_SELECT`, `UNSUPPORTED_STATEMENT` |
| 4 | No `INTO`, no locking clause, no nested write node | `SELECT_INTO`, `ROW_LOCK`, `NESTED_WRITE` |
| 5 | No denied function anywhere in the tree | `DENIED_FUNCTION` |
| 6 | Every table reference schema-qualified and on the allowlist | `TABLE_NOT_ALLOWED`, `CROSS_DATABASE`, `NO_TABLE` |
| 7 | `LIMIT` injected or clamped to 1,000 | — (rewrite, not a refusal) |

Four of these exist because of a specific hole:

**Check 4 — a `SELECT` is not automatically a read.** `SELECT * INTO evil FROM analytics.dim_product`
parses as an `exp.Select` with an `into` argument and creates a table. `SELECT ... FOR UPDATE`
parses as an `exp.Select` and takes row locks. Both would pass a check that only looked at the
statement type.

**Check 5 — some queries reference no table at all.** `SELECT pg_read_file('/etc/passwd')` has
nothing for the allowlist to inspect. The denylist is matched against AST function nodes, not
against the query text. This one is genuinely belt-and-braces: `pg_read_file` needs privileges the
agent role does not have, so the database would refuse it too — the validator just refuses it
earlier and logs a cleaner reason.

**Check 6 — CTE names look exactly like table names.** `WITH leak AS (SELECT * FROM
raw.marketing_spend) SELECT * FROM leak` produces *two* table nodes in the tree: `raw.marketing_spend`
and `leak`. CTE aliases are collected and excused; the real tables inside each CTE body are still
walked. Excusing the whole CTE instead would be the hole this attack is aiming at.

**Check 6 again — unqualified names are rewritten, not just accepted.** `SELECT * FROM
fct_daily_revenue` becomes `SELECT * FROM analytics.fct_daily_revenue` in the emitted SQL. Without
the rewrite, the validated name and the executed name are resolved by `search_path` at two
different moments, and a redirected `search_path` would point them at different tables.

**Check 7 — the cap is clamped, not only injected.** `LIMIT 999999` is technically a limit. A limit
the model chose is not a limit the guardrail imposed, so anything above 1,000 is clamped down to
1,000. Measured on a 43,860-row table: no `LIMIT` → 1,000 rows returned; `LIMIT 999999` → 1,000 rows
returned.

---

## 3. The append-only audit log

`audit.agent_tool_calls`, DDL in `agent/audit/schema.sql`, writer in `agent/audit/audit_log.py`.
One row per **attempt** — the rejected ones are the point, since a guardrail that blocks silently
leaves no evidence it ever fired.

Columns: `occurred_at`, `investigation_id`, `call_index`, `tool_name`, `tool_input` (jsonb),
`generated_sql` (what was asked for), `executed_sql` (what the validator actually emitted),
`validation_outcome`, `rejection_code`, `rejection_reason`, `tables_referenced`, `row_count`,
`duration_ms`, `db_role`, `error_message`.

Storing `generated_sql` and `executed_sql` separately is deliberate: the difference between them is
the rewrite, so the log shows what the guardrail changed as well as what it allowed.

### Append-only is enforced twice

**Layer one — grants.** `GRANT INSERT` and nothing else. Not `SELECT`, not `UPDATE`, not `DELETE`.

This turned out to be tighter than expected, in a way worth recording. The writer originally ended
with `RETURNING audit_id`, and it failed:

```
psycopg2.errors.InsufficientPrivilege: permission denied for table agent_tool_calls
```

`RETURNING` reads the row back, so it needs `SELECT`. The grant is narrow enough that the writer
cannot see what it just wrote — which is exactly the property append-only is supposed to give. The
clause was removed rather than the grant widened.

**Layer two — a trigger.** Grants restrain the agent role. They do not restrain `revenue_ops`, which
owns the table. A `BEFORE UPDATE OR DELETE OR TRUNCATE ... FOR EACH STATEMENT` trigger raises
`insufficient_privilege` for every role, owner included:

```sql
RAISE EXCEPTION 'audit.agent_tool_calls is append-only; % is refused', TG_OP
    USING ERRCODE = 'insufficient_privilege';
```

This is not tamper-proof — the owner can drop the trigger. That is the honest boundary, and it is
still worth having: it converts a quiet `DELETE FROM audit...` into a loud, separate, privileged
action that itself has to be chosen.

**Failure mode: closed.** If the audit row cannot be written, the exception propagates and the tool
call fails. An unlogged tool call is the one outcome the design does not permit.

**The residual gap, stated plainly.** The row is written in a `finally` block, after the outcome is
known. A process killed mid-execution between the query and the log write would leave that call
unrecorded. A two-phase design — an intent row before, an outcome row after — would close it, at
the cost of two rows per call. Given a single-process portfolio agent, one row per call in a
`finally` is the trade-off taken, not an oversight.

---

## 4. The tool-call ceiling

`agent/guardrails/call_budget.py`. `MAX_TOOL_CALLS = 25`, sized from the Day 8 plan (roughly a dozen
queries to characterise one anomaly, doubled for slack).

Two design choices carry the whole guarantee:

**It raises, it does not return `False`.** `spend()` raises `ToolCallBudgetExceeded`. A caller that
ignores a boolean keeps going; a caller that ignores an exception does not. This is the difference
between a counter and a cap, and it is what section 5.5 is testing.

**It charges on attempt, not on success.** A call that fails validation still consumed a model turn.
Not charging for failures is precisely how a loop that keeps re-issuing a rejected query runs
forever.

`BudgetedInvestigation` is the context-manager form for Day 8: it catches the exhaustion exception at
the boundary and turns it into a clean early exit, so the loop stops and a partial brief ships rather
than the process crashing. `budget.stopped_reason` is the line the brief prints to say it was cut
short — a truncated investigation that does not say so is worse than none.

---

## 5. Attack attempts — real queries, real outcomes

Harness: `python -m tests.attack_attempts`. Full transcript at the end of this document.

**Headline: 67 attempts, 54 blocked as expected, 16 legitimate reads allowed, 0 unexpected
outcomes, and 67 of 67 attempts reconciled against rows in the audit log.**

Those are two different populations and the harness now says so rather than leaving the
arithmetic to the reader: **70 outcomes = 67 agent-identity attempts + 3 owner-identity trigger
tests**. The three owner-side attempts in 5.4 are performed as `revenue_ops`, so they correctly
cannot appear in a log written over the agent's connection. Only the 67 have to reconcile, and
the harness asserts that split rather than printing one merged total.

### 5.1 Reading a forbidden schema — blocked by the grants

Fired straight down the agent's connection, validator removed.

| Attempted | Result |
|---|---|
| `SELECT * FROM raw.daily_revenue LIMIT 1` | `permission denied for schema raw` |
| `SELECT * FROM raw.product_master LIMIT 1` | `permission denied for schema raw` |
| `SELECT * FROM raw.marketing_spend LIMIT 1` | `permission denied for schema raw` |
| `SELECT * FROM raw.inventory_snapshot LIMIT 1` | `permission denied for schema raw` |
| `SELECT * FROM raw.holiday_calendar LIMIT 1` | `permission denied for schema raw` |
| `SELECT * FROM staging.stg_product_master LIMIT 1` | `permission denied for schema staging` |
| `SELECT * FROM staging.stg_daily_revenue LIMIT 1` | `permission denied for schema staging` |
| `SELECT * FROM staging.stg_marketing_spend LIMIT 1` | `permission denied for schema staging` |
| `SELECT * FROM staging.stg_inventory_snapshot LIMIT 1` | `permission denied for schema staging` |
| `SELECT * FROM staging.stg_holiday_calendar LIMIT 1` | `permission denied for schema staging` |
| `SELECT * FROM staging.stg_product_master_dedup_audit LIMIT 1` | `permission denied for schema staging` |
| `SELECT * FROM intermediate.int_marketing_spend_allocated LIMIT 1` | `permission denied for schema intermediate` |
| `SELECT * FROM intermediate.int_category_cost_basis LIMIT 1` | `permission denied for schema intermediate` |
| `SELECT * FROM audit.agent_tool_calls LIMIT 1` | `permission denied for table agent_tool_calls` |

Note that the failure is at the **schema** level, not the table level. The agent has no `USAGE` on
`raw`, so the objects inside it are not even addressable — which is why adding a table to `raw`
later cannot accidentally expose it.

Control: all six allowlisted objects read successfully in the same run.

### 5.2 Writing through the read-only connection — blocked by the grants

| Attempted | Result |
|---|---|
| `DROP TABLE analytics.dim_product` | `must be owner of table dim_product` |
| `DELETE FROM analytics.detected_anomalies` | `permission denied for table detected_anomalies` |
| `UPDATE analytics.fct_daily_revenue SET gross_revenue = 0` | `permission denied for table fct_daily_revenue` |
| `INSERT INTO analytics.dim_product (sku_id) VALUES ('EVIL-0001')` | `permission denied for table dim_product` |
| `CREATE TABLE analytics.exfiltrated AS SELECT * FROM analytics.dim_product` | `permission denied for schema analytics` |
| `CREATE TEMP TABLE scratch (x int)` | `permission denied to create temporary tables in database "revenue_anomaly"` |
| `GRANT SELECT ON raw.daily_revenue TO revenue_agent` | `permission denied for schema raw` |
| `ALTER TABLE analytics.dim_product ADD COLUMN backdoor text` | `must be owner of table dim_product` |
| `ALTER ROLE revenue_agent SUPERUSER` | `must be superuser to alter superuser roles or change superuser attribute` |

The privilege-escalation attempt is the one worth pointing at: the agent cannot grant itself
anything, because `GRANT` requires privileges on the target and `SUPERUSER` requires being one
already.

### 5.3 Attacks through the validator — blocked before the database

Every one of these was refused by `agent/guardrails/sql_validator.py`. **None reached Postgres.**

| Class | Attempted | Refused as |
|---|---|---|
| Write statement | `DROP TABLE analytics.dim_product` | `NOT_A_SELECT` (got DROP) |
| Write statement | `DELETE FROM analytics.detected_anomalies WHERE 1=1` | `NOT_A_SELECT` (got DELETE) |
| Write statement | `UPDATE analytics.dim_product SET unit_cost = 0` | `NOT_A_SELECT` (got UPDATE) |
| Write statement | `TRUNCATE analytics.fct_daily_revenue` | `NOT_A_SELECT` (got TRUNCATETABLE) |
| Multi-statement injection | `SELECT category FROM fct_daily_revenue; DROP TABLE analytics.dim_product` | `MULTI_STATEMENT` — got 2 (SELECT, DROP) |
| Multi-statement injection | `SELECT 1 FROM analytics.dim_product; DELETE FROM analytics.detected_anomalies;` | `MULTI_STATEMENT` — got 2 (SELECT, DELETE) |
| Off-allowlist table | `SELECT * FROM raw.daily_revenue` | `TABLE_NOT_ALLOWED` |
| Off-allowlist table | `SELECT * FROM staging.stg_product_master` | `TABLE_NOT_ALLOWED` |
| Off-allowlist table | `SELECT * FROM intermediate.int_marketing_spend_allocated` | `TABLE_NOT_ALLOWED` |
| Hidden in a CTE | `WITH leak AS (SELECT * FROM raw.marketing_spend) SELECT * FROM leak` | `TABLE_NOT_ALLOWED` — `raw.marketing_spend` |
| Hidden in a subquery | `SELECT (SELECT count(*) FROM raw.inventory_snapshot) AS n FROM analytics.dim_product` | `TABLE_NOT_ALLOWED` — `raw.inventory_snapshot` |
| Hidden in a join | `SELECT a.* FROM analytics.dim_product a JOIN staging.stg_product_master b ON a.sku_id = b.sku_id` | `TABLE_NOT_ALLOWED` — `staging.stg_product_master` |
| Write disguised as a read | `SELECT * INTO analytics.exfiltrated FROM analytics.dim_product` | `SELECT_INTO` |
| Read that takes locks | `SELECT * FROM analytics.dim_product FOR UPDATE` | `ROW_LOCK` |
| File read | `SELECT pg_read_file('/etc/passwd')` | `DENIED_FUNCTION` |
| Resource exhaustion | `SELECT pg_sleep(300) FROM analytics.dim_product` | `DENIED_FUNCTION` |
| Catalog probe | `SELECT * FROM pg_catalog.pg_authid` | `TABLE_NOT_ALLOWED` |
| Non-SELECT command | `EXPLAIN ANALYZE SELECT * FROM analytics.dim_product` | `UNSUPPORTED_STATEMENT` |
| UNION onto a forbidden table | `SELECT category FROM analytics.dim_product UNION ALL SELECT category FROM raw.product_master` | `NOT_A_SELECT` (got UNION) |
| Reads nothing | `SELECT 1` | `NO_TABLE` |

The three "hidden" rows are the ones that justify walking the tree rather than checking the `FROM`
clause. In each case the forbidden table is somewhere a shallow check would not look, and in each
case the AST walk found it.

Control — legitimate queries in the same run:

| Query | Outcome |
|---|---|
| Grouped aggregate on unqualified `fct_daily_revenue` | 20 rows; rewritten to `analytics.fct_daily_revenue`, `LIMIT 1000` injected |
| Top 5 anomalies by `abs(peak_z_score)` | 5 rows; existing `LIMIT 5` left alone |
| CTE joining `detected_anomalies` to `detected_anomaly_points` | 23 rows; CTE alias correctly excused |
| `SELECT * FROM analytics.fct_daily_revenue` (43,860 rows, no LIMIT) | **1,000 rows** — cap injected |
| `SELECT * FROM analytics.fct_daily_revenue LIMIT 999999` | **1,000 rows** — cap clamped down |

### 5.4 Tampering with the audit trail

As the agent — blocked by the grants:

| Attempted | Result |
|---|---|
| `DELETE FROM audit.agent_tool_calls` | `permission denied for table agent_tool_calls` |
| `UPDATE audit.agent_tool_calls SET validation_outcome = 'pass'` | `permission denied for table agent_tool_calls` |
| `TRUNCATE audit.agent_tool_calls` | `permission denied for table agent_tool_calls` |
| `DROP TABLE audit.agent_tool_calls` | `must be owner of table agent_tool_calls` |
| `SELECT * FROM audit.agent_tool_calls` | `permission denied for table agent_tool_calls` |

As `revenue_ops`, the **owner**, whom the grants do not restrain — blocked by the trigger:

| Attempted | Result |
|---|---|
| `DELETE FROM audit.agent_tool_calls` | `audit.agent_tool_calls is append-only; DELETE is refused` |
| `UPDATE audit.agent_tool_calls SET row_count = 0` | `audit.agent_tool_calls is append-only; UPDATE is refused` |
| `TRUNCATE audit.agent_tool_calls` | `audit.agent_tool_calls is append-only; TRUNCATE is refused` |

### 5.5 The tool-call ceiling — does it abort, or merely count?

Ceiling lowered to 5, then eight identical and entirely legitimate calls fired:

```
  call 1    ALLOWED  executed, 1 rows
  call 2    ALLOWED  executed, 1 rows
  call 3    ALLOWED  executed, 1 rows
  call 4    ALLOWED  executed, 1 rows
  call 5    ALLOWED  executed, 1 rows
  call 6    BLOCKED  BUDGET_EXCEEDED  Tool-call ceiling of 5 reached; aborting the
                     investigation and returning a partial brief.
  call 7    BLOCKED  BUDGET_EXCEEDED  ...
  call 8    BLOCKED  BUDGET_EXCEEDED  ...

  5/5 tool calls used (EXHAUSTED); 0 remaining.
  Queries that actually reached the database: 5 of 8 attempts.
```

Five queries reached the database, not eight. The cap aborts; it does not merely count. Calls 6–8
each still wrote an audit row with outcome `budget_exceeded`, so the truncation is visible in the
record rather than looking like an investigation that simply ended.

### 5.6 Audit reconciliation — did every attempt leave a row?

```
  Two populations, and they are deliberately different sizes:

    Outcomes printed above           : 70
      agent-identity attempts        :  67  <- each MUST leave an audit row
      owner-identity trigger tests   :   3  <- each MUST NOT: the log is written over the
                                            agent's connection, and revenue_ops is not
                                            the audited identity
    Audit rows for ATTACK-c8120893  : 67
    Reconciles                       : YES (67 audited attempts == 67 rows)

  By recorded outcome:
    budget_exceeded      3
    error               28
    pass                16
    reject              20

  Blocked attempts that left a row : 51
  Successful reads that left a row : 16
  Author of every row (db_role)    : ['revenue_agent']
------------------------------------------------------------------------------------------------
  Attempts blocked as expected :  54  (51 audited + 3 owner-side)
  Attempts allowed as expected :  16
  UNEXPECTED outcomes          :   0
  Audit trail reconciles       : True (67 of 67)
```

The `70 = 67 + 3` split is asserted, not just printed: if the two counters ever disagree the
harness prints an explicit mismatch line. The audit table is **append-only, so it accumulates
across runs** — every run is isolated by `investigation_id`, and the reconciliation above is
scoped to one.

Broken out by refusal code, straight from the table:

```
 validation_outcome |    rejection_code     | count
--------------------+-----------------------+-------
 budget_exceeded    | BUDGET_EXCEEDED       |     3
 error              |                       |    28
 pass               |                       |    16
 reject             | TABLE_NOT_ALLOWED     |     7
 reject             | NOT_A_SELECT          |     5
 reject             | DENIED_FUNCTION       |     2
 reject             | MULTI_STATEMENT       |     2
 reject             | UNSUPPORTED_STATEMENT |     1
 reject             | NO_TABLE              |     1
 reject             | ROW_LOCK              |     1
 reject             | SELECT_INTO           |     1
```

`reject` means the validator refused it and the database never saw it. `error` means the query
reached Postgres and Postgres refused it — those 28 are the direct-connection attacks from 5.1,
5.2 and 5.4, which is the number you want to be high, because it is the count of times the second
wall caught something the first was not asked about.

---

## 6. Defence in depth, demonstrated

Two independent layers, shown separating on a single case. A brand-new table was created in
`analytics` by the owner, with no `GRANT` issued for it:

```
  DB layer  : agent SELECT -> 1 row  (ALTER DEFAULT PRIVILEGES did this)
  Validator : REJECTED (TABLE_NOT_ALLOWED) -- a new mart is not reachable until config.py is edited
```

The database says yes because default privileges cover anything the owner creates in `analytics`.
The validator says no because the allowlist in `agent/guardrails/config.py` is written out by name
and nobody edited it. This is why the allowlist is a hand-maintained tuple rather than something
read from `information_schema`: an allowlist that discovers its own contents from the database is
an allowlist the database can widen. A new mart becoming visible to the agent should be a reviewed
line in a diff.

---

## 7. What this does not protect against

Stated so the claim is bounded:

- **Prompt injection into the brief.** Nothing here stops a model being persuaded to write a
  misleading summary of data it legitimately read. The guardrails bound *access*, not *judgement*.
- **A compromised owner.** `revenue_ops` is a superuser. Anyone holding it can drop the trigger,
  widen the grants, or read anything. The boundary protects against a compromised *agent*.
- **Data exfiltration within the allowlist.** The agent can read all six analytics objects, 1,000
  rows at a time. It is supposed to. If that data were sensitive, column-level grants or row-level
  security would be the next layer.
- **Secrets management.** `.env` is a gitignored file, not a vault.

---

## 8. Running it

```
python -m agent.guardrails.provision     # create/refresh the role, grants and audit table
python -m tests.test_guardrails          # 18 unit tests, no database needed
python -m tests.attack_attempts          # the live attack harness above
```

All three run from `.venv` (Python 3.14). `provision.py` is idempotent and should be re-run after
any change to the analytics schema; it is not yet wired into the DAG — `ALTER DEFAULT PRIVILEGES`
means it does not need to be for a routine `dbt build`, and adding a fourth task is a Day 8+
decision, not a Day 7 one.

`tests/test_guardrails.py` uses plain asserts and a small runner rather than pytest, because pytest
is not a dependency of this project and CI is explicitly deferred in the locked scope.

---

## Appendix — full attack transcript

```
================================================================================================
DAY 7 GUARDRAIL ATTACK HARNESS   investigation_id = ATTACK-3e9af2aa
Agent connects as 'revenue_agent'; the owner is 'revenue_ops'.
================================================================================================

================================================================================================
SECTION 1 - Schema isolation: can the read-only role reach anything but analytics?
================================================================================================
  SELECT * FROM raw.daily_revenue LIMIT 1
    BLOCKED  permission denied for schema raw
  SELECT * FROM raw.product_master LIMIT 1
    BLOCKED  permission denied for schema raw
  SELECT * FROM raw.marketing_spend LIMIT 1
    BLOCKED  permission denied for schema raw
  SELECT * FROM raw.inventory_snapshot LIMIT 1
    BLOCKED  permission denied for schema raw
  SELECT * FROM raw.holiday_calendar LIMIT 1
    BLOCKED  permission denied for schema raw
  SELECT * FROM staging.stg_product_master LIMIT 1
    BLOCKED  permission denied for schema staging
  SELECT * FROM staging.stg_daily_revenue LIMIT 1
    BLOCKED  permission denied for schema staging
  SELECT * FROM staging.stg_marketing_spend LIMIT 1
    BLOCKED  permission denied for schema staging
  SELECT * FROM staging.stg_inventory_snapshot LIMIT 1
    BLOCKED  permission denied for schema staging
  SELECT * FROM staging.stg_holiday_calendar LIMIT 1
    BLOCKED  permission denied for schema staging
  SELECT * FROM staging.stg_product_master_dedup_audit LIMIT 1
    BLOCKED  permission denied for schema staging
  SELECT * FROM intermediate.int_marketing_spend_allocated LIMIT 1
    BLOCKED  permission denied for schema intermediate
  SELECT * FROM intermediate.int_category_cost_basis LIMIT 1
    BLOCKED  permission denied for schema intermediate
  SELECT * FROM audit.agent_tool_calls LIMIT 1
    BLOCKED  permission denied for table agent_tool_calls

  Control: the six allowlisted analytics objects must all still be readable.
  SELECT count(*) FROM analytics.fct_daily_revenue
    ALLOWED  succeeded, 1 rows
  SELECT count(*) FROM analytics.fct_daily_margin
    ALLOWED  succeeded, 1 rows
  SELECT count(*) FROM analytics.fct_daily_stockout
    ALLOWED  succeeded, 1 rows
  SELECT count(*) FROM analytics.dim_product
    ALLOWED  succeeded, 1 rows
  SELECT count(*) FROM analytics.detected_anomalies
    ALLOWED  succeeded, 1 rows
  SELECT count(*) FROM analytics.detected_anomaly_points
    ALLOWED  succeeded, 1 rows

================================================================================================
SECTION 2 - Write attempts straight down the read-only connection (validator bypassed)
================================================================================================
  DROP TABLE analytics.dim_product
    BLOCKED  must be owner of table dim_product
  DELETE FROM analytics.detected_anomalies
    BLOCKED  permission denied for table detected_anomalies
  UPDATE analytics.fct_daily_revenue SET gross_revenue = 0
    BLOCKED  permission denied for table fct_daily_revenue
  INSERT INTO analytics.dim_product (sku_id) VALUES ('EVIL-0001')
    BLOCKED  permission denied for table dim_product
  CREATE TABLE analytics.exfiltrated AS SELECT * FROM analytics.dim_product
    BLOCKED  permission denied for schema analytics
  CREATE TEMP TABLE scratch (x int)
    BLOCKED  permission denied to create temporary tables in database "revenue_anomaly"
  GRANT SELECT ON raw.daily_revenue TO revenue_agent
    BLOCKED  permission denied for schema raw
  ALTER TABLE analytics.dim_product ADD COLUMN backdoor text
    BLOCKED  must be owner of table dim_product
  ALTER ROLE revenue_agent SUPERUSER
    BLOCKED  must be superuser to alter superuser roles or change superuser attribute

================================================================================================
SECTION 3 - Attacks through the SQL validator (they never reach the database)
================================================================================================
  [write statement] DROP TABLE analytics.dim_product
    BLOCKED  NOT_A_SELECT          Only SELECT is allowed; got DROP.
  [write statement] DELETE FROM analytics.detected_anomalies WHERE 1=1
    BLOCKED  NOT_A_SELECT          Only SELECT is allowed; got DELETE.
  [write statement] UPDATE analytics.dim_product SET unit_cost = 0
    BLOCKED  NOT_A_SELECT          Only SELECT is allowed; got UPDATE.
  [write statement] TRUNCATE analytics.fct_daily_revenue
    BLOCKED  NOT_A_SELECT          Only SELECT is allowed; got TRUNCATETABLE.
  [multi-statement injection] SELECT category FROM fct_daily_revenue; DROP TABLE analytics.dim_product
    BLOCKED  MULTI_STATEMENT       Only one statement is allowed; got 2 (SELECT, DROP). Multi-state
  [multi-statement injection] SELECT 1 FROM analytics.dim_product; DELETE FROM analytics.detected_anomalie
    BLOCKED  MULTI_STATEMENT       Only one statement is allowed; got 2 (SELECT, DELETE). Multi-sta
  [off-allowlist table] SELECT * FROM raw.daily_revenue
    BLOCKED  TABLE_NOT_ALLOWED     Table 'raw.daily_revenue' is not on the allowlist. Allowed: anal
  [off-allowlist table] SELECT * FROM staging.stg_product_master
    BLOCKED  TABLE_NOT_ALLOWED     Table 'staging.stg_product_master' is not on the allowlist. Allo
  [off-allowlist table] SELECT * FROM intermediate.int_marketing_spend_allocated
    BLOCKED  TABLE_NOT_ALLOWED     Table 'intermediate.int_marketing_spend_allocated' is not on the
  [off-allowlist, hidden in a CTE] WITH leak AS (SELECT * FROM raw.marketing_spend) SELECT * FROM leak
    BLOCKED  TABLE_NOT_ALLOWED     Table 'raw.marketing_spend' is not on the allowlist. Allowed: an
  [off-allowlist, hidden in a subquery] SELECT (SELECT count(*) FROM raw.inventory_snapshot) AS n FROM analytics.dim
    BLOCKED  TABLE_NOT_ALLOWED     Table 'raw.inventory_snapshot' is not on the allowlist. Allowed:
  [off-allowlist, hidden in a join] SELECT a.* FROM analytics.dim_product a JOIN staging.stg_product_master b ON
    BLOCKED  TABLE_NOT_ALLOWED     Table 'staging.stg_product_master' is not on the allowlist. Allo
  [write disguised as a read] SELECT * INTO analytics.exfiltrated FROM analytics.dim_product
    BLOCKED  SELECT_INTO           SELECT ... INTO creates a table and is refused; it is a write di
  [read that takes locks] SELECT * FROM analytics.dim_product FOR UPDATE
    BLOCKED  ROW_LOCK              Locking clauses (FOR UPDATE / FOR SHARE) are refused; a read mus
  [file read via function] SELECT pg_read_file('/etc/passwd')
    BLOCKED  DENIED_FUNCTION       Function pg_read_file() is not permitted.
  [sleep via function] SELECT pg_sleep(300) FROM analytics.dim_product
    BLOCKED  DENIED_FUNCTION       Function pg_sleep() is not permitted.
  [catalog probe] SELECT * FROM pg_catalog.pg_authid
    BLOCKED  TABLE_NOT_ALLOWED     Table 'pg_catalog.pg_authid' is not on the allowlist. Allowed: a
  [non-SELECT command] EXPLAIN ANALYZE SELECT * FROM analytics.dim_product
    BLOCKED  UNSUPPORTED_STATEMENT Statement is not a SELECT (parsed as an unsupported command: EXP
  [UNION onto a forbidden table] SELECT category FROM analytics.dim_product UNION ALL SELECT category FROM ra
    BLOCKED  NOT_A_SELECT          Only SELECT is allowed; got UNION.
  [nothing to read at all] SELECT 1
    BLOCKED  NO_TABLE              Query references no allowlisted table; a tool call must read fro

  Control: legitimate analyst queries must still run, and the row cap must bite.
  [grouped aggregate, unqualified table name]
      SELECT category, region, sum(gross_revenue) AS revenue FROM fct_daily_revenue WHERE orde
    ALLOWED  executed, 20 rows [LIMIT 1000 injected (query had none).]
  [top anomalies by severity]
      SELECT anomaly_key, cell_key, start_date, peak_z_score FROM analytics.detected_anomalies
    ALLOWED  executed, 5 rows
  [CTE across two allowlisted tables]
      WITH worst AS (SELECT cell_key FROM analytics.detected_anomalies ORDER BY abs(peak_z_sco
    ALLOWED  executed, 23 rows [LIMIT 1000 injected (query had none).]
  [NO LIMIT on a 43,860-row table - cap must be injected]
      SELECT * FROM analytics.fct_daily_revenue
    ALLOWED  executed, 1,000 rows [LIMIT 1000 injected (query had none).]
  [LIMIT 999999 - cap must be clamped down]
      SELECT * FROM analytics.fct_daily_revenue LIMIT 999999
    ALLOWED  executed, 1,000 rows [LIMIT clamped down to the 1000-row ceiling.]

================================================================================================
SECTION 4 - Tampering with the audit trail itself
================================================================================================
  DELETE FROM audit.agent_tool_calls
    BLOCKED  permission denied for table agent_tool_calls
  UPDATE audit.agent_tool_calls SET validation_outcome = 'pass'
    BLOCKED  permission denied for table agent_tool_calls
  TRUNCATE audit.agent_tool_calls
    BLOCKED  permission denied for table agent_tool_calls
  DROP TABLE audit.agent_tool_calls
    BLOCKED  must be owner of table agent_tool_calls
  SELECT * FROM audit.agent_tool_calls
    BLOCKED  permission denied for table agent_tool_calls

  And as the OWNER, whom the grants do not restrain - the trigger must:
  [as owner] DELETE FROM audit.agent_tool_calls
    BLOCKED  audit.agent_tool_calls is append-only; DELETE is refused
  [as owner] UPDATE audit.agent_tool_calls SET row_count = 0
    BLOCKED  audit.agent_tool_calls is append-only; UPDATE is refused
  [as owner] TRUNCATE audit.agent_tool_calls
    BLOCKED  audit.agent_tool_calls is append-only; TRUNCATE is refused

================================================================================================
SECTION 5 - Tool-call ceiling: does it abort, or merely count?
================================================================================================
  Ceiling set to 5. Firing 8 identical, entirely legitimate calls.

  call 1
    ALLOWED  executed, 1 rows [LIMIT 1000 injected (query had none).]
  call 2
    ALLOWED  executed, 1 rows [LIMIT 1000 injected (query had none).]
  call 3
    ALLOWED  executed, 1 rows [LIMIT 1000 injected (query had none).]
  call 4
    ALLOWED  executed, 1 rows [LIMIT 1000 injected (query had none).]
  call 5
    ALLOWED  executed, 1 rows [LIMIT 1000 injected (query had none).]
  call 6
    BLOCKED  BUDGET_EXCEEDED  Tool-call ceiling of 5 reached; aborting the investigation and returning a partial brief.
  call 7
    BLOCKED  BUDGET_EXCEEDED  Tool-call ceiling of 5 reached; aborting the investigation and returning a partial brief.
  call 8
    BLOCKED  BUDGET_EXCEEDED  Tool-call ceiling of 5 reached; aborting the investigation and returning a partial brief.

  5/5 tool calls used (EXHAUSTED); 0 remaining.
  Queries that actually reached the database: 5 of 8 attempts.
  Stop reason handed to the partial brief: Tool-call ceiling of 5 reached; aborting the investigation and returning a partial brief.

================================================================================================
SECTION 6 - Audit reconciliation: did every attempt leave a row?
================================================================================================
  Attempts made by this harness : 67
  Audit rows found for ATTACK-3e9af2aa : 67
  Reconciles: YES

  By recorded outcome:
    budget_exceeded      3
    error               28
    pass                16
    reject              20

  Blocked attempts that left a row : 51
  Successful reads that left a row : 16
  Author of every row (db_role)    : ['revenue_agent']
------------------------------------------------------------------------------------------------
  Attempts blocked as expected : 54
  Attempts allowed as expected : 16
  UNEXPECTED outcomes          : 0
  Audit trail reconciles       : True
------------------------------------------------------------------------------------------------

Full trail:  SELECT * FROM audit.agent_tool_calls WHERE investigation_id = 'ATTACK-3e9af2aa' ORDER BY audit_id;
```
