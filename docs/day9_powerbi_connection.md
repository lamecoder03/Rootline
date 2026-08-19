# Day 9 — Connecting Power BI Desktop to the warehouse

How Power BI Desktop reaches the `analytics` marts, which login it uses, and why that login
exists at all. The dashboard itself is built by hand from
`docs/day9_dashboard_build_guide.md`; this document is the connection layer underneath it.

---

## Why a third role

There are now three logins against one database, and the split is the point.

| Role | Who uses it | Privileges | Created by |
|---|---|---|---|
| `revenue_ops` | dbt, the loader, the detector, Airflow | Owner. Creates and drops every object | `docker-compose.yml` / initdb |
| `revenue_agent` | The Day 8 autonomous agent | `SELECT` on `analytics`, `INSERT` on `audit.agent_tool_calls` | `agent/guardrails/provision.py` |
| **`revenue_reporting`** | **Power BI Desktop** | **`SELECT` on `analytics`, nothing else** | **`dashboards/provision_reporting.py`** |

Day 7 established that an agent which can query a production-shaped database is a security
story. Day 9 applies the identical reasoning to the second consumer of the same marts, and the
argument does not depend on anyone distrusting Power BI:

1. **Least privilege is per-consumer, not per-system.** The agent and the dashboard read the
   same six tables, so it is tempting to hand Power BI the `revenue_agent` credentials and move
   on. That would silently widen the BI tool's authority to include `INSERT` on the audit log —
   a privilege a dashboard has no use for, and one that lets a BI user write rows that look like
   agent activity. A privilege granted "because it was already there" is exactly the drift
   least privilege exists to prevent.

2. **Attribution.** With one shared login, `pg_stat_activity`, the Postgres log and any future
   query-audit all show `revenue_agent` for both consumers. "Which of these two things ran this
   query at 02:00?" becomes unanswerable — and it is the question you ask first when something
   is wrong. Two logins make the answer free.

3. **Independent revocation.** Rotating the dashboard's password, or cutting off BI access
   during an incident, must not stop the agent from investigating, and vice versa. Shared
   credentials couple two unrelated lifecycles.

**`revenue_reporting` is deliberately *tighter* than `revenue_agent`, not merely different.**
It has no access to the `audit` schema at all — not even `SELECT`. The agent holds `INSERT`
there because it is required to record its own tool calls; a dashboard has nothing to record,
and read access would expose every query the agent ever ran, including the SQL, to anyone who
opens the `.pbix`.

---

## Connection parameters

| Field in Power BI | Value | Notes |
|---|---|---|
| **Server** | `localhost:5433` | Host **and** port in the one field, colon-separated |
| **Database** | `revenue_anomaly` | |
| **Data Connectivity mode** | **Import** | See "Import, not DirectQuery" below |
| **Username** | `revenue_reporting` | `REPORTING_DB_USER` in `.env` |
| **Password** | *(in `.env`)* | `REPORTING_DB_PASSWORD` — never written into this file |
| **Encrypt connection** | **unchecked** | See "The encryption checkbox" below |

The password is not printed in this document because `docs/` is committed to git and `.env` is
not. Read it out of `.env` when Power BI asks:

```
findstr REPORTING_DB .env
```

### Why `localhost:5433` and not `postgres:5432`

This is CLAUDE.md's fourth gotcha seen from a third angle. **Power BI Desktop is a Windows
application running on the host, not a container**, so it reaches Postgres exactly the way the
loader and `run_dbt.bat` do — through the port the compose file publishes to the host.

- The container always listens on **5432** internally. Asking the server itself confirms it:
  `SELECT inet_server_port()` returns `5432` even over the host connection, because that is the
  port the *server* is bound to inside its own network namespace.
- Docker publishes that to **5433** on the host, because a native `postgresql-x64-18` Windows
  service already owns 5432 on this machine. `POSTGRES_PORT=5433` in `.env` drives both the
  compose publish and every host-side client.
- `postgres:5432` is the address used *inside* the compose network — that is what the Airflow
  DAG uses, and it will not resolve from Power BI.

So: **containers say `postgres:5432`, the Windows host says `localhost:5433`.** Power BI is on
the Windows host.

### Import, not DirectQuery

Import is correct here, and the reason is not just "it is faster":

- **Size makes it free.** The largest table is 43,860 rows and the whole model is under 105,000
  rows. That is trivially small for an in-memory model — there is no size argument for
  DirectQuery.
- **The data is daily-batch, not live.** It changes when the Airflow DAG runs, not continuously.
  DirectQuery's only real advantage is freshness the source cannot actually provide.
- **DAX is not fully available in DirectQuery.** Several measures in
  `docs/day9_dax_measures.md` — particularly the severity banding and the weighted-ratio
  patterns — are either unsupported or fold into slow SQL under DirectQuery.
- **It keeps the warehouse out of the interactive path.** Every slicer click in DirectQuery is a
  query against Postgres. With Import, exploring the dashboard cannot affect the database the
  agent is simultaneously querying.

The trade-off, stated plainly: an imported model is a **snapshot**. After the DAG runs, the
`.pbix` shows yesterday's picture until you hit **Refresh**. That is acceptable because the
deliverable is the agent's brief, not the dashboard — the dashboard exists so a human can
sanity-check a brief, and it is refreshed when they do.

### The encryption checkbox

Power BI defaults to requesting an encrypted connection and will fail with a message about
being unable to connect securely. The Postgres container runs the stock `postgres:15` image,
which ships with `ssl = off` — there is no certificate to negotiate against.

Untick **"Encrypt connection"** in the credentials dialog. This is acceptable **only** because
the connection never leaves the machine: Power BI Desktop and the Docker-published port are both
on `localhost`, so there is no network segment for anyone to sit on. If this warehouse were ever
reachable over a network, that checkbox would have to stay ticked and the server would need TLS
configured — a real change, not a preference.

---

## Which tables to import

Import exactly these six, all in the `analytics` schema:

| Table | Grain | Rows | Why the dashboard needs it |
|---|---|---|---|
| `fct_daily_revenue` | date × category × channel × region | 43,860 | The revenue series and marketing spend context |
| `fct_daily_margin` | date × category × channel × region | 43,860 | Margin, and the cost-basis coverage that qualifies it |
| `fct_daily_stockout` | date × category × region | 14,620 | The inventory picture — **no channel column** |
| `dim_product` | sku_id | 120 | SKU reference and the uncosted-SKU disclosure |
| `detected_anomalies` | one incident | 44 | The anomaly overlay and severity breakdown |
| `detected_anomaly_points` | flagged cell-day | 166 | Per-day evidence behind each incident |

Those counts are the contract in CLAUDE.md, and the provisioner verifies them on every run.

**There is nothing else to choose from, by construction.** `raw`, `staging` and `intermediate`
are not merely unselected — `revenue_reporting` has no `USAGE` on those schemas, so they do not
appear in the Navigator at all. The Navigator is showing you the privilege boundary, not a
curated list. Verified:

```
denied  raw.daily_revenue                            permission denied for schema raw
denied  staging.stg_daily_revenue                    permission denied for schema staging
denied  intermediate.int_marketing_spend_allocated   permission denied for schema intermediate
denied  audit.agent_tool_calls                       permission denied for schema audit
```

---

## Setup, start to finish

1. **Make sure the stack is up.** Power BI cannot start the database for you.
   ```
   docker compose up -d
   docker exec revenue_anomaly_postgres pg_isready -U revenue_ops -d revenue_anomaly
   ```

2. **Create the role.** Idempotent — safe to re-run at any time.
   ```
   python -m dashboards.provision_reporting --verify
   ```
   `--verify` proves the boundary by using it: it reads all six `analytics` objects over the
   reporting role's own connection, then attempts four forbidden schema reads and five writes
   and confirms each is refused. It prints `BOUNDARY HOLDS` or names the leak.

3. **In Power BI Desktop:** *Home → Get Data → More… → Database → PostgreSQL database*.
   Enter `localhost:5433` as Server and `revenue_anomaly` as Database. Choose **Import**.

4. **Credentials:** pick the **Database** tab (not Windows). Username `revenue_reporting`,
   password from `.env`. Untick *Encrypt connection*. Apply the setting at the
   `localhost:5433` level so it is reused for all six tables.

5. **Navigator:** tick the six tables above, then **Load** — not *Transform Data*. The marts are
   already typed and cleaned by dbt; reshaping them in Power Query would put transformation
   logic in a place that has no tests and no version history. If a column needs changing, it
   changes in dbt, where 191 tests run against it.

6. **Verify the load** by checking row counts against the table above. A mismatch means the DAG
   has run since and the marts moved — not that the connection is wrong.

---

## Troubleshooting

**`permission denied for table fct_daily_revenue` after a pipeline run.**
This should not happen, and if it does the cause is specific. dbt materialises marts with
`CREATE TABLE AS`, which produces *new objects that carry no grants* — a grant lives on an
object and dies with it. `provision_reporting.py` issues `ALTER DEFAULT PRIVILEGES FOR ROLE
revenue_ops IN SCHEMA analytics GRANT SELECT ON TABLES TO revenue_reporting`, which attaches
`SELECT` to whatever the owner creates in `analytics` from then on. If you see this error, the
provisioner has not been run since the role was created, or a mart was built by a different
owner. Re-run `python -m dashboards.provision_reporting --verify`.

**A new mart does not appear.** Same cause, same fix — but note that default privileges only
apply to objects created *after* they were set. Re-running the provisioner also issues
`GRANT SELECT ON ALL TABLES`, which catches anything created in between.

**The Navigator shows no schemas.** The role has `CONNECT` but the connection is landing on the
wrong database. Check `Database` is `revenue_anomaly`, not `postgres`.

**Connection refused.** Docker Desktop is not running, or the container is not up. Note that
`docker ps` showing the container is not sufficient — wait for `pg_isready`.

**Timeout on a large visual.** The role carries a 120-second `statement_timeout`, set higher
than the agent's 30 seconds because a Power BI import legitimately scans whole fact tables where
an agent query never should. If a query genuinely needs more than 120 seconds against a
105,000-row model, the query is wrong, not the limit.

---

## What this role cannot do, verified

Output of `python -m dashboards.provision_reporting --verify`, run against the live warehouse:

```
  Should ALLOW - the six analytics objects Power BI imports:
    ok      analytics.fct_daily_revenue         43,860 rows
    ok      analytics.fct_daily_margin          43,860 rows
    ok      analytics.fct_daily_stockout        14,620 rows
    ok      analytics.dim_product                  120 rows
    ok      analytics.detected_anomalies            44 rows
    ok      analytics.detected_anomaly_points      166 rows

  Should DENY - schemas a reporting tool has no business reading:
    denied  raw.daily_revenue                            permission denied for schema raw
    denied  staging.stg_daily_revenue                    permission denied for schema staging
    denied  intermediate.int_marketing_spend_allocated   permission denied for schema intermediate
    denied  audit.agent_tool_calls                       permission denied for schema audit

  Should DENY - writes to analytics, which this role must never perform:
    denied  INSERT                                       permission denied for table dim_product
    denied  UPDATE                                       permission denied for table dim_product
    denied  DELETE                                       permission denied for table dim_product
    denied  DROP                                         must be owner of table dim_product
    denied  INSERT-audit                                 permission denied for schema audit

  BOUNDARY HOLDS
```

The session is additionally pinned: `search_path = analytics, pg_catalog`, so an unqualified
table name cannot be redirected by a `search_path` change, and `statement_timeout = 120s`.
