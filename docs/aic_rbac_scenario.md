# Role-Based Security Scenario

**The scenario:** two consumers read the same warehouse — an autonomous LLM agent that
investigates anomalies, and a read-only BI client a human would use to check the agent's work. They
must not share an identity, and neither may reach the raw data.

This is **built and verified**, not designed. Every table below was read from the running
database while writing this document.

---

## The three identities

| Role | Who | Purpose | Created by |
|---|---|---|---|
| `revenue_ops` | dbt, loader, detector, Airflow | Owner — creates and drops every object | `docker-compose.yml` / initdb |
| `revenue_agent` | The LLM investigator | Read marts, record its own tool calls | `agent/guardrails/provision.py` |
| `revenue_reporting` | A read-only BI client | Read marts | `dashboards/provision_reporting.py` |

Credentials are separate `.env` entries per role. No code path hands agent code the owner's
credentials — `agent/guardrails/db.py` exposes two engine builders, one per identity.

---

## Entitlements, read from the live database

**Table grants** (`information_schema.role_table_grants`):

| Role | Schema | Privileges | Objects |
|---|---|---|---|
| `revenue_agent` | analytics | SELECT | 7 |
| `revenue_agent` | audit | **INSERT** | 1 |
| `revenue_reporting` | analytics | SELECT | 7 |
| `revenue_ops` | raw / staging / intermediate / analytics / audit | ALL | 23 |

> **`ALTER DEFAULT PRIVILEGES` demonstrated, unplanned.** These counts read 6 until
> `analytics.detection_coverage` was created by a later detector run. Neither provisioner was
> re-run: both roles picked up `SELECT` on the new table automatically, which is exactly the
> mechanism described below. Note the agent's *query* allowlist is enforced separately in
> `agent/guardrails/config.py` and still lists 6 tables — the grant is the floor, the allowlist
> is the ceiling.

**Schema-level `USAGE`** (`has_schema_privilege`) — the denial half:

| Role | raw | staging | intermediate | analytics | audit |
|---|---|---|---|---|---|
| `revenue_agent` | ✗ | ✗ | ✗ | ✓ | ✓ |
| `revenue_reporting` | ✗ | ✗ | ✗ | ✓ | ✗ |

Without schema `USAGE`, objects in `raw`, `staging` and `intermediate` are **not addressable** —
not merely unreadable. A query naming them fails before row-level permissions are consulted.

---

## The finding that matters: `ALTER DEFAULT PRIVILEGES`

**A grant lives on an object and dies with it.** dbt rebuilds every mart with `CREATE TABLE AS`
on each pipeline run, which drops the old table and creates a *new* one — carrying none of
yesterday's grants.

Without a default-privilege rule, both consumers work perfectly until the next DAG run and then
**silently lose access**. For a BI client this surfaces as what looks like a client-side
connection error; for the agent, as an investigation that suddenly cannot read anything.

Verified live in `pg_default_acl`:

```
   grantor   |  schema   |                    default_privs
-------------+-----------+-----------------------------------------------------------
 revenue_ops | analytics | {revenue_agent=r/revenue_ops,revenue_reporting=r/revenue_ops}
```

Both roles hold `r` (read) on *future* objects created by `revenue_ops` in `analytics`. This was
proven the hard way — by running a full `run_dbt.bat build` (191 tests, every mart dropped and
recreated) and re-reading all six objects as each role afterwards.

---

## Why the reporting role is not a reuse of the agent role

`revenue_reporting` is **strictly tighter** than `revenue_agent`, not merely different:

> `revenue_agent` holds `INSERT` on `audit.agent_tool_calls` because it must record its own tool
> calls. `revenue_reporting` has **no access to the `audit` schema at all**.

A dashboard has nothing to record, and read access there would expose **every query the agent
ever ran** — including the refused ones — to anyone holding the reporting credentials.

Three reasons a shared login was rejected:

1. **Drift.** Privileges granted "because they were already there" is exactly what least
   privilege exists to stop.
2. **Attribution.** With one shared login, `pg_stat_activity` and the server log cannot answer
   which consumer ran a given query.
3. **Independent rotation.** Rotating the dashboard's password must not stop the agent from
   investigating.

Session limits differ for the same reason: the reporting role gets a **120s** `statement_timeout`
(a BI import legitimately scans whole fact tables); the agent gets **30s** (an agent query
never should).

---

## Proof by use — not by assertion

Each provisioner ships a `--verify` mode that exercises the boundary rather than describing it.

`python -m dashboards.provision_reporting --verify`

| Check | Expected | Result |
|---|---|---|
| Read 6 `analytics` objects | allowed | ✅ 6/6 |
| Read `raw`, `staging`, `intermediate`, `audit` | refused | ✅ 4/4 refused |
| `INSERT`/`UPDATE`/`DELETE`/`DROP` on `analytics` | refused | ✅ 4/4 refused |
| `INSERT` on `audit.agent_tool_calls` | refused | ✅ refused |

**The agent's boundary is attacked, not just checked.** `python -m tests.attack_attempts` fires
83 deliberate attempts against the live warehouse:

| Outcome | Count |
|---|---|
| Blocked as expected | **62** |
| Legitimate reads allowed | **21** |
| **Unexpected outcomes** | **0** |
| Agent-identity attempts reconciled against audit rows | **80 of 80** |

Every attempt fires **twice** — once through the SQL validator, and once straight down the
connection with the validator removed — so the database-level wall is proven independently of
the application-level one. Writes, `DROP`s, semicolon-chained injections, raw-schema reads hidden
in CTEs and subqueries, `SELECT … INTO`, `pg_read_file`, privilege escalation and audit tampering
were all refused.

---

## Defence in depth: three independent layers

A judge should note that the role is the **last** line, not the only one.

| Layer | Mechanism | Fails closed by |
|---|---|---|
| 1. Application | `sqlglot` AST validator — parses, does not scan | Rejecting anything that is not a single `SELECT` against an allowlisted table |
| 2. Connection | Separate least-privilege login per consumer | Postgres refusing the statement |
| 3. Record | Append-only audit log, enforced by grant **and** trigger | Recording the attempt regardless of outcome |

Layer 1 can be removed entirely and layer 2 still holds — that is precisely what the
validator-removed half of the attack harness demonstrates.

**A real bug this caught:** the function check began as a *denylist*. Six functions passed both
it and the read-only role — `pg_get_viewdef` leaked a view body naming `staging` tables the agent
cannot query, `txid_current` forced a WAL write from a "read-only" role, `repeat()` materialised
64 MB in one row. It is now an allowlist of ~70 names. The asymmetry was the bug; allowlists are
now used for tables, statement types and functions alike.

---

## Reproducing this

```bash
python -m agent.guardrails.provision          # agent role + audit DDL (idempotent)
python -m dashboards.provision_reporting --verify   # BI role + boundary proof
python -m tests.attack_attempts               # 83 live attempts
```

Full method and the complete attack transcript: `docs/security_guardrails.md`.
