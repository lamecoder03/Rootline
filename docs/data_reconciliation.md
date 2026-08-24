# Data Reconciliation — staging layer: how the messy data was cleaned, and why

This is the reasoning behind the dbt staging layer, written to be read on its own. The code
is in `dbt/revenue_anomaly/models/staging/`; this document explains the decisions the code
implements and the alternatives that were rejected.

---

## What was actually wrong with the raw data

Four of the five raw tables were already clean — they came out of a simulation, so their
types and grains were consistent. One table, `raw.product_master`, was dirty on purpose,
because it stands in for the thing that is always dirty in a real warehouse: the product
dimension assembled from more than one operational system.

Measured, not assumed. Before writing a line of SQL:

| Problem | Extent |
|---|---|
| Rows vs. real products | 158 rows describing 120 real SKUs |
| Duplicate SKUs | 34 SKUs appear more than once; 30 across both systems, 8 double-entered inside the ERP |
| Join key corrupted | 15% of keys scrambled: ` ELEC-0031 `, `beau-0007`, `HOME-0010  `, `BEAU_0018` |
| Category spellings | 24 distinct spellings of 5 real categories |
| Date formats | Two: `2023-03-26` from the ERP, `01/04/2024` from Shopify |
| Missing values | `category_raw` 18, `unit_cost` 18, `supplier` 17, `product_name` 9 blank, `subcategory` 0 |

The two systems are `erp_prod` and `shopify_export`. They disagree about everything: how a
category is spelled, how a date is formatted, and which fields they happen to have populated
for any given product.

**Why this matters downstream, not just aesthetically.** If `Consumer Electronics` and
`ELECTRONICS` survive as two categories, the z-score detector splits one revenue signal
into two half-sized ones. Each looks less significant than the real combined signal, so the
detector's control limits are computed on the wrong baseline and it misses the anomaly it
exists to catch. The cleaning is not tidiness — it is the difference between the detector
working and not working.

---

## Decision 1 — Normalise the join key before matching anything

`sku_id` is the key everything else joins on, and it was the column the source systems had
corrupted. The rule:

```
upper(replace(trim(sku_id), '_', '-'))
```

Trim the padding, uppercase the case drift, convert the underscore variant to the hyphen
form. That collapses ` ELEC-0031 `, `elec-0031`, `ELEC_0031` and `ELEC-0031  ` onto one key.

Two things worth saying about this:

**The identical expression is used in `stg_inventory_snapshot`.** The inventory table's SKUs
were already clean, so normalising them is a no-op today. It is there anyway, because the
moment the two models normalise differently the product join silently drops rows. A
`relationships` test between the two now guards that: if the expressions ever diverge, that
test fails before anything downstream is affected.

**The original value is kept.** `sku_id_source_value` in the audit table holds the key exactly
as the source wrote it. Normalisation should be reversible for investigation — if someone asks
"which system sent us `BEAU_0018`?", the answer must still be in the warehouse.

---

## Decision 2 — Category standardisation lives in a seed, not in a CASE statement

Two steps.

**Step one: normalise, which does most of the work for free.** Lowercase the raw value and
strip every non-alphanumeric character:

```
lower(regexp_replace(category_raw, '[^a-zA-Z0-9]', '', 'g'))
```

`Home & Garden`, `HOME & GARDEN`, `home_garden` and `Home&Garden` all become `homegarden`.
That single expression collapses 24 spellings down to 10 buckets without anyone maintaining a
list — it handles case, whitespace, ampersands, underscores and slashes automatically.

**Step two: map the 10 remaining genuine synonyms.** Normalisation cannot know that
`consumerelectronics` and `electronics` are the same thing, or that `homeandgarden` and
`homegarden` are. That is business knowledge, and it lives in
`seeds/category_alias_map.csv` — 10 rows, loaded into the warehouse as a table by `dbt seed`.

**Why a seed and not a CASE statement.** Three reasons, in order of how much they matter:

1. When a source system invents an eleventh spelling, fixing it is a one-line CSV edit that a
   data analyst can review — not a SQL change that needs an engineer.
2. The mapping is queryable. "What did `home_garden` become, and what else mapped to Home &
   Garden?" is answerable from the warehouse instead of by reading code.
3. It separates the mechanical part (normalisation, which is logic) from the judgement part
   (these two names mean the same thing, which is a business decision). Those change for
   different reasons and at different rates, so they belong in different places.

**The join is a LEFT join on purpose.** An unrecognised spelling produces NULL rather than
being quietly bucketed into a default. NULL then trips the `accepted_values` test, which fails
the build. The design principle throughout this layer: **unknown data should stop the
pipeline, not flow through it wearing a plausible disguise.**

---

## Decision 3 — Dedupe by merging the best value per column, not by picking a winning row

This is the decision with the most reasoning behind it, so it gets the most space.

The obvious approach is survivor-take-all: rank each SKU's rows, keep the best one, discard the
rest. It is simple and it is what most warehouses do. It is also lossy here, because the two
source systems have **independently** missing fields. The ERP row for a product might have its
cost but not its category; the Shopify row for the same product might have the reverse.
Survivor-take-all keeps one of those rows and throws away a value that was sitting right there.

So the logic runs in two stages.

**Stage one — rank the duplicates.** For each SKU, order its rows by:

1. **Completeness** — how many of the five meaningful fields are populated. Most complete first.
2. **Recency** — `extracted_at`, newest first. Shopify extracted a day after the ERP, so it
   wins ties.
3. **Source system name**, then **an md5 hash of the whole row.**

That last tiebreaker exists for a specific reason. Eight SKUs are exact duplicates inside the
ERP — identical in every field, so completeness and recency cannot separate them. Without a
final deterministic key, which row "wins" would depend on the order Postgres happens to return
rows in, and the model's output could change between runs with no change to the input. A build
that isn't reproducible isn't trustworthy, so the hash makes the choice arbitrary but fixed.

**Stage two — merge, field by field.** Rather than taking the rank-1 row whole, each field
independently takes **the first non-null value in rank order**:

```sql
(array_agg(unit_cost order by source_row_rank)
    filter (where unit_cost is not null))[1]
```

In plain terms: *line this SKU's rows up best-first, and for each field take the first one that
actually has a value.* Postgres 15 has no `IGNORE NULLS` option on window functions, so this
array trick is the equivalent. The rank-1 row still wins any field where both rows have a
value — it is the most trustworthy record — but a field it is missing gets filled from the
next-best row that has it.

### A worked example

`BEAU-0010` — a beard oil present in both systems:

| Rank | Source | Extracted | Category in source | Score | Kept? | Fields it donated |
|---|---|---|---|---|---|---|
| 1 | `shopify_export` | 2026-01-06 | `Beauty & Personal Care` | 4/5 | **yes** | name, category, supplier, launch_date |
| 2 | `erp_prod` | 2026-01-05 | *null* | 4/5 | no | **unit_cost** |

Both rows scored 4 out of 5, so recency broke the tie and the Shopify row became the primary.
But the Shopify row has no `unit_cost`, and the ERP row does. The merge takes it.

The result is a complete record — name, category, cost, supplier and launch date — that
**neither source system had on its own**. Survivor-take-all would have produced this same
product with a NULL cost, and nobody would have known the value existed.

Four SKUs were completed this way. The measurable effect across the dimension:

| Field | Nulls in raw (158 rows) | Nulls after merge (120 SKUs) |
|---|---|---|
| `unit_cost` | 18 | 8 |
| `supplier` | 17 | 9 |
| `product_name` | 9 | 4 |

Some of that reduction is simply the row count falling; the rest is genuinely recovered data.

---

## Decision 4 — The dedupe evidence is kept, in a queryable table

Collapsing 158 rows to 120 destroys 38 rows. That is the only step in this layer that
legitimately deletes data, and a deletion nobody can inspect is indistinguishable from a bug.

So `stg_product_master_dedup_audit` retains **all 158 raw rows**, one per row, recording:

- the SKU key both as normalised and as the source wrote it
- which source system it came from and when it was extracted
- its completeness score and its rank within its SKU
- whether it survived (`was_kept_as_primary`)
- whether its SKU was duplicated across systems, inside one system, or both
- **which specific fields it donated to the merged record** (`contributed_unit_cost`, etc.)

That last group is what makes the merge auditable rather than merely logged. For any product
you can see every raw row, which one won, and exactly which field came from which loser.

It is materialised as a **table**, not a view — every other staging model is a view. Evidence
should persist as it was when the build ran, not be silently recomputed on next read.

Query it like this:

```sql
select sku_id, source_system, source_row_rank, was_kept_as_primary,
       contributed_unit_cost, contributed_category
from staging.stg_product_master_dedup_audit
where sku_id = 'BEAU-0010'
order by source_row_rank;
```

What it shows for this dataset: 38 rows discarded, 30 SKUs present in both systems, 8
double-entered inside the ERP, and 4 losing rows that still contributed a field.

---

## Decision 5 — Missing values, one documented decision per column

The governing principle: **no row is ever dropped for having a missing value, and a value is
only invented where the inference rests on evidence rather than on a plausible guess.** Every
column that had nulls carries a boolean flag saying so, so a downstream consumer can always
tell an imputed or absent value from a real one.

### `category` — 18 null → **imputed**, flagged `category_was_imputed`

The only column that gets a value invented, resolved in three steps:

1. Take the mapped category from any of this SKU's source rows (the merge above). This alone
   fixes most of them.
2. If every row for that SKU was null, infer from the SKU prefix — `ELEC-0007` is Electronics —
   via `seeds/sku_prefix_category_map.csv`.
3. Otherwise `'Unknown'`.

**Why impute here and nowhere else.** Category is the grouping key for the entire detection
layer. A SKU with a null category silently disappears from every category rollup — it does not
error, it just quietly stops being counted, which is the most dangerous kind of data problem.
And the inference is not a guess: the SKU prefix is the company's own internal encoding, so
`ELEC-` meaning Electronics is a documented fact about the business, not a statistical hunch.

7 SKUs needed the prefix fallback. **None fell through to `'Unknown'`** — and the
`accepted_values` test, which permits only the five real categories, is what proves it. If a
future load contained a SKU with an unrecognised prefix, that test fails the build.

### `unit_cost` — 18 null → **kept NULL**, flagged `has_missing_unit_cost` (8 remain)

Rejected alternative: impute with the category median.

`unit_cost` feeds margin calculations. A median-imputed cost is a plausible-looking number
that flows into a financial metric and is never questioned again — the error is invisible
precisely because the value looks reasonable. A NULL that makes a `SUM` come back empty is
a worse-looking outcome and a better one: it is loud, and loud errors get fixed.

### `supplier` — 17 null → **kept NULL**, flagged `has_missing_supplier` (9 remain)

The agent names suppliers in briefs that a human reads and acts on ("supplier X shipped late").
Imputing a supplier would put a real company's name next to an accusation that has no evidence
behind it. There is no imputation rule that justifies that risk.

### `product_name` — 9 blank → **normalised to NULL**, flagged (4 remain)

The blanks arrived as empty strings, which is worse than NULL because `''` and NULL are two
different representations of the same nothing and every downstream filter has to remember
both. `nullif(trim(product_name), '')` collapses them to one.

Not placeholdered with something like `"(unnamed) ELEC-0007"`. `product_name` is a display
label — never a join key, never a metric. Whether the dashboard shows a blank cell or a
placeholder is a presentation decision, so it belongs in the mart or the BI layer where
someone can see it being made, not buried in a model whose job is cleaning.

### `subcategory` — 0 null → no policy needed

Verified complete rather than assumed complete, and covered by a `not_null` test so it stays
that way.

---

## Decision 6 — Dates parsed by shape, not by source system

`launch_date_raw` arrives as `2023-03-26` from the ERP and `01/04/2024` from Shopify. The
tempting implementation is `case when source_system = 'erp_prod' then ...`.

That was rejected. It hard-codes an assumption about what a system *currently* does into a
model that will keep running after the system changes. The day Shopify switches its export to
ISO format, `01/04/2024` logic applied to `2024-01-04` does not error — it silently produces
wrong dates.

Instead the parse matches the string's actual shape with a regex, and anything matching neither
pattern becomes NULL and trips the `not_null` test. **The failure mode is a failed build rather
than a quietly wrong number.** All 158 rows parsed successfully.

---

## Decision 7 — Marketing spend keeps its own grain until the marts layer

`raw.marketing_spend` is one grain coarser than revenue: it has no `region`. Revenue is
date × category × channel × region; spend is only date × channel × category.

Staging keeps the source grain and **declares** it, via a surrogate key over exactly those
three columns and a `unique` test on it. The roll-up that reconciles spend to revenue is
deliberately deferred to the marts layer.

**Why.** Splitting one spend number across four regions requires choosing an allocation rule —
by revenue share, evenly, by population. That is a business assumption, and it materially
changes what a per-region spend efficiency number means. Burying that choice inside a model
whose stated job is "cleaning" hides it from anyone reading the pipeline. Cleaning models
should not contain business judgements; marts should, where they are labelled as such.

---

## How the layer is structured, and why

Six staging models plus a shared intermediate:

| Model | What it is |
|---|---|
| `stg_daily_revenue` | Typed revenue fact, 43,860 rows |
| `stg_marketing_spend` | Typed spend fact, 10,965 rows |
| `stg_product_master` | Cleaned product dimension, 120 rows |
| `stg_product_master_dedup_audit` | Dedupe evidence, 158 rows (table) |
| `stg_inventory_snapshot` | Typed stock levels, 87,720 rows |
| `stg_holiday_calendar` | Typed calendar, 30 rows |
| `stg_product_master_ranked` | Shared intermediate (ephemeral) |

`stg_product_master_ranked` is **ephemeral**: it creates no database object, and dbt pastes it
in as a CTE wherever it is referenced. Both the dimension and its audit table need the same
normalise-and-rank logic. Written twice, the two copies would eventually drift and the audit
would stop describing what the dimension actually did. Written once as an ephemeral model,
they cannot.

Every other staging model is a **view**. Staging does no expensive computation — it casts and
trims — so a view is always fresh, costs no storage, and never needs a refresh schedule. The
audit table is the single exception, for the reason given above.

**Explicit casts everywhere, even where they are redundant.** `order_date` already arrives as
a `DATE`; it is still cast to `date`. Money columns arrive as `double precision` and are cast
to `numeric(14,2)` — that one is not cosmetic, because floating-point is the wrong type for
money and rounding drift in a revenue baseline would corrupt the anomaly detection. The
redundant casts stay because a staging model should state its output types rather than inherit
whatever the loader happened to produce.

---

## What the tests protect against

81 tests, all passing. The ones that carry real weight:

**`accepted_values` on `stg_product_master.category`** — the proof that the standardisation
worked. It permits exactly the five canonical values, so it fails if any of the 24 raw
spellings survived, and it fails if any SKU fell through to `'Unknown'`. This one test is the
difference between believing the cleaning worked and knowing it did.

**`relationships` from `stg_marketing_spend.category` to `stg_product_master.category`** — the
most valuable test in the layer. It proves the two mock source systems now speak the same
category language as the transactional tables. Standardisation could be internally consistent
and still wrong; this checks it against an independent table.

**`relationships` from `stg_inventory_snapshot.sku_id` to `stg_product_master.sku_id`** — proves
the two SKU normalisations agree. If they diverge, this fails before the product join silently
starts dropping rows.

**`unique` on the md5 surrogate keys** — dbt's built-in `unique` test takes one column, but
three of these models have composite grains. Each model builds an md5 key over its grain
columns, which turns "is this grain actually unique?" into a single testable column. It also
hands the marts a ready-made join key. This is what `dbt_utils.unique_combination_of_columns`
would do; done natively, it costs no dependency.

**Three singular tests** for things generic tests structurally cannot express:

- `assert_dedup_audit_covers_all_source_rows` — the audit accounts for every raw row, every SKU
  has exactly one survivor, and the dimension has one row per audited SKU. This is the guard
  that makes the 158 → 120 collapse arithmetic, not faith.
- `assert_revenue_grain_is_complete` — 731 days × 60 cells with no gap in the date sequence. A
  missing day would read to the detector as a revenue collapse, manufacturing an anomaly
  that never happened.
- `assert_no_negative_measures` — no negative revenue, orders, units, spend, stock or cost. A
  sign error in a cast is silent and would shift the z-score control limits.

---

## Running it

dbt runs from a **separate Python 3.13 virtualenv** (`.venv-dbt`). This is not a preference:
dbt 1.11 pins `mashumaro<3.15`, and that version fails to import on Python 3.14, which is what
the generators run on. dbt 1.12 would fix the pin but cannot install here at all — one of its
build dependencies downloads a wheel from GitHub during install and fails this machine's TLS
certificate validation. Two interpreters is the honest resolution.

```
py -3.13 -m venv .venv-dbt
.venv-dbt\Scripts\pip install -r requirements-dbt.txt
```

Then every dbt command goes through the wrapper:

```
run_dbt.bat debug
run_dbt.bat build
run_dbt.bat test --select stg_product_master
```

### Why the wrapper exists

dbt's `env_var()` reads the process environment. Nothing populates that environment from
`.env` — and it is worth being precise about why, because the file *looks* like it is being
read by everything.

Three components need these credentials, and each gets them a different way. Docker Compose
reads `.env` natively, because Compose has that behaviour built in. The loader in
`generators/load_to_postgres.py` calls `load_dotenv()` explicitly — `python-dotenv` is a Python
library that a program has to invoke on itself, not something that alters the shell. dbt is a
third-party binary that makes neither of those calls, so it was the one component with no path
from the file to its environment. It fails with an `env_var` error, which reads like a broken
profile and is not.

`run_dbt.bat` closes that gap. It parses `.env` into real environment variables, then calls dbt
with `--project-dir` and `--profiles-dir` already set. It parses the file natively rather than
shelling out to a Python helper, so it has no dependency of its own; it anchors every path to
the repo root, so it runs from any working directory; it propagates dbt's exit code, so a failed
build still fails a caller such as the Airflow `transform` task; and it fails with a readable message
if `.env`, the venv, or `POSTGRES_PASSWORD` is missing rather than letting dbt fail obscurely.

One Windows detail worth knowing, because it cost a debugging cycle: **cmd.exe silently
mis-parses a `.bat` file saved with LF line endings**, eating the leading characters of each
line so `setlocal` becomes `local` and `set "X=1"` becomes `"X=1"`. The errors point at
nonsense commands rather than at the real cause. `.gitattributes` now pins `*.bat` to CRLF so
a clone with `core.autocrlf=false` cannot reintroduce it.

This wrapper is also what lets `profiles.yml` be committed to git: it contains only `env_var()`
lookups, so the repository holds the shape of the connection and never the credential.
`.gitignore` ignores `profiles.yml` everywhere by default and re-includes this one specific
file, with a comment saying why.

Staging models land in the `staging` schema, and the marts land in `analytics`. dbt's
default behaviour would have produced `analytics_staging` by prefixing the target schema, so
`macros/generate_schema_name.sql` overrides it to use the custom name verbatim. That keeps the
boundary clean for the guardrail this project is built around: the agent's read-only role gets
granted on `analytics` only, which is a real permission boundary rather than a prefix-matching
exercise.
