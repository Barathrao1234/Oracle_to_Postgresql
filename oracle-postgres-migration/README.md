# Oracle → PostgreSQL Migration Pipeline

A Copilot Agent–driven pipeline for migrating Oracle SQL queries to PostgreSQL.
The Agent handles all migration reasoning, analysis, and orchestration; Python provides
lightweight, deterministic validation.

---

## Architecture

```
                         INPUT
                           │
                           ▼
                  input/oracle-query.txt
                           │
                           ▼
             ┌─────────────────────────────┐
             │      COPILOT AGENT          │
             │                             │
             │  1. Analyze SQL             │
             │     • CTEs, subqueries      │
             │     • custom functions      │
             │     • collection types      │
             │     • <<PLACEHOLDERS>>      │
             │     • MULTISET / TABLE(...) │
             │  2. Detect dependencies     │
             │     • KNOWN vs UNRESOLVED   │
             │  3. Completeness check      │
             │     • can we safely migrate?│
             │  4. Migrate                 │
             └──────────┬──────────────────┘
                        │
                 ┌──────┴──────────┐
                 │                 │
           Sufficient         Missing critical
                 │            dependencies
                 ▼                 │
          Migrate SQL              ▼
                 │          REVIEW_REQUIRED
                 ▼          (written to
       output/postgresql-query.sql  final-status.json)
                 │
                 ▼
    ┌──────────────────────────────┐
    │      PYTHON VALIDATORS       │
    │                              │
    │  output_sanity                │
    │  syntax                       │
    │  oracle_constructs             │
    │  structure  ← CTEs, EXISTS,   │
    │    TABLE(...), MULTISET,      │
    │    placeholders, ROWNUM       │
    │  completeness                  │
    │  conditions                    │
    │  joins      ← type + ON preds  │
    │  dependencies ← enforces the   │
    │    Agent's own analysis-       │
    │    result.json readiness       │
    │  postgresql_live (optional,    │
    │    skipped for TEMPLATE_SQL)   │
    └──────────┬───────────────────┘
               │
      ┌────────┼─────────────┐
      │        │             │
     PASS  SEMANTIC_       FAIL
      │    UNCERTAINTY       │
      │    only              │
      ▼        │              ▼
  APPROVED     ▼        validation-report.json
           AGENT_REVIEW_       │
           NEEDED               ▼
               │           COPILOT AGENT
        Agent re-reads    (correct SQL)
        original query          │
               │                ▼
       ┌───────┴───────┐    Validate
       │               │  (up to 3 retries)
  acknowledge      genuinely       │
  with reasoning   unresolved  ┌───┴───┐
       │               │       │       │
       ▼               ▼   APPROVED  REVIEW_REQUIRED
   re-validate    REVIEW_REQUIRED
       │           (Agent writes
       ▼            this itself)
   APPROVED
   (if all issues
   now acknowledged)
```

**In one sentence:** the Copilot Agent owns all migration analysis, reasoning,
and the correction loop; Python is a small, deterministic, generic validation layer.

---

## Folder structure

```
oracle-postgres-migration/
│
├── .github/
│   └── agents/
│       └── oracle-postgres-migrator.agent.md   ← Agent instructions
│
├── input/
│   └── oracle-query.txt                         ← Drop your Oracle SQL here
│
├── output/                                       ← Agent-written artefacts (empty at rest)
│   ├── .gitkeep
│   ├── postgresql-query.sql                     ← generated during a run
│   ├── migration-result.json                    ← generated during a run
│   ├── analysis-result.json                     ← generated during a run (STEP 3)
│   ├── agent-judgment.json                      ← generated only if AGENT_REVIEW_NEEDED fires
│   ├── validation-report.json                   ← generated during a run
│   └── final-status.json                        ← generated once the run is finalised
│
├── .runtime/                                     ← Internal pipeline state (not user-facing, not shipped)
│   └── attempt-state.json
│
├── validators/
│   ├── validate.py                ← Single CLI entry point
│   ├── output_validator.py        ← Sanity: no markdown fences / conversational text
│   ├── syntax_validator.py        ← Balanced parens, dangling clauses, sqlparse
│   ├── construct_validator.py     ← Oracle-specific constructs that must not survive
│   ├── structure_validator.py     ← Clause presence + CTEs, EXISTS, TABLE, placeholders
│   ├── completeness_validator.py  ← SELECT count, GROUP BY, ORDER BY, pagination
│   ├── condition_validator.py     ← WHERE predicate presence and value integrity
│   ├── join_validator.py          ← Join table presence, type, and ON conditions
│   ├── dependency_validator.py    ← Enforces the Agent's own analysis-result.json readiness
│   ├── postgres_validator.py      ← Live PostgreSQL PREPARE (SQL_ERROR vs ENV_ERROR); skips TEMPLATE_SQL
│   ├── sql_utils.py               ← Shared paren-aware parsing helpers + placeholder detection
│   └── __init__.py
│
├── requirements.txt
└── README.md
```

`input/oracle-query.txt` ships with a representative sample query (CTE,
unresolved custom functions/types, an application placeholder, a non-equi
join, nested `ROWNUM` pagination) so the pipeline is actually exercised
end-to-end, not just described. Replace it with your real query before
running the Agent for a real migration.

---

## Quick start

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Place your Oracle query

```bash
cp your-oracle-query.sql input/oracle-query.txt
```

### 3. Run the Agent

Open GitHub Copilot Chat in Agent mode and it will follow the instructions in
`.github/agents/oracle-postgres-migrator.agent.md` automatically.

### 4. Manual validator usage (for debugging)

```bash
# Reset state before a fresh run
python validators/validate.py --reset

# Validate after writing output/postgresql-query.sql manually
python validators/validate.py \
    --oracle input/oracle-query.txt \
    --postgres output/postgresql-query.sql

# With a live PostgreSQL connection (strongest check)
python validators/validate.py \
    --oracle input/oracle-query.txt \
    --postgres output/postgresql-query.sql \
    --postgres-dsn "postgresql://user:pass@host:5432/dbname"
```

---

## Exit codes

| Code | Name | Meaning |
|------|------|---------| 
| `0` | `APPROVED` | All required checks passed (any `SEMANTIC_UNCERTAINTY` issues, if there were any, are acknowledged with a reasoned justification). |
| `1` | `CORRECTION_NEEDED` | A hard-fail issue was found; correction attempts remain. |
| `2` | `REVIEW_REQUIRED` | Retry limit reached, or `VALIDATION_ENVIRONMENT_ERROR` detected. |
| `3` | `VALIDATION_ERROR` | Infrastructure/IO error; do **not** modify the SQL. |
| `4` | `AGENT_REVIEW_NEEDED` | Only `SEMANTIC_UNCERTAINTY` issue(s) remain, unacknowledged. The Agent must judge them (`output/agent-judgment.json`) or escalate to `REVIEW_REQUIRED` itself. Not a final state. |

---

## Retry and versioning semantics

| Event | `query_version` | `correction_number` |
|-------|-----------------|---------------------|
| Initial migration | 1 | 0 |
| Correction 1 | 2 | 1 |
| Correction 2 | 3 | 2 |
| Correction 3 | 4 | 3 |
| → `REVIEW_REQUIRED` | — | — |

`MAX_CORRECTION_ATTEMPTS = 3` means at most **4 total generated versions** before
the run escalates to `REVIEW_REQUIRED`.

---

## Validator reference

| Validator | What it checks |
|-----------|----------------|
| `output_sanity` | No markdown fences, no conversational preamble, starts with SQL keyword |
| `syntax` | Balanced parentheses, dangling clauses, `sqlparse` statement type |
| `oracle_constructs` | Oracle-only syntax remaining (`NVL`, `SYSDATE`, `ROWNUM`, `DECODE`, …) |
| `structure` | Clause presence; CTEs; EXISTS subqueries; TABLE(...) → unnest/LATERAL; ROWNUM at all query levels; application placeholder preservation |
| `completeness` | SELECT expression count, GROUP BY columns, ORDER BY columns/direction, pagination row count |
| `conditions` | WHERE predicate presence (column + operator); value-change detection for simple literals; SEMANTIC_UNCERTAINTY for complex predicates |
| `joins` | Table presence, join type (INNER/LEFT/RIGHT/FULL), ON equi-join column pairs; SEMANTIC_UNCERTAINTY for non-equi predicates |
| `dependencies` | Reads the Agent's own `analysis-result.json`; fails if `migration_readiness` is `BLOCKED`, or if `UNRESOLVED` dependencies exist without an explicit `READY_WITH_UNRESOLVED` |
| `postgresql_live` | `PREPARE` against a real PostgreSQL schema; classifies errors as `SQL_ERROR` vs `VALIDATION_ENVIRONMENT_ERROR` (requires `--postgres-dsn`); automatically `SKIPPED` (with a note) when the target is `TEMPLATE_SQL` (contains `<<PLACEHOLDER>>` tokens) |

---

## Issue type severity

### Hard-fail → Agent corrects the SQL

```
STRUCTURE_LOST            ORACLE_CONSTRUCT_REMAINS
MISSING_PLACEHOLDER       SQL_ERROR
INCOMPLETE_SELECT_LIST    INCOMPLETE_GROUP_BY
INCOMPLETE_ORDER_BY       ORDER_BY_DIRECTION_CHANGED
MISSING_ROW_LIMIT         ROW_LIMIT_CHANGED
MISSING_CONDITION         CONDITION_VALUE_CHANGED
MISSING_JOIN              JOIN_TYPE_MISMATCH
JOIN_CONDITION_MISMATCH   SYNTAX
EMPTY_OUTPUT              MARKDOWN_FENCE_IN_OUTPUT
UNRESOLVED_DEPENDENCY     — the Agent's own analysis-result.json says BLOCKED,
                            or has UNRESOLVED deps without READY_WITH_UNRESOLVED
```

These can never be waived by `agent-judgment.json` — only fixing the SQL (or,
for `UNRESOLVED_DEPENDENCY`, fixing `analysis-result.json`) resolves them.

### Immediate escalation → REVIEW_REQUIRED (do NOT modify SQL)

```
VALIDATION_ENVIRONMENT_ERROR  — live DB infrastructure broken; SQL may be correct
```

### Deferred to the Agent → AGENT_REVIEW_NEEDED (exit 4)

```
SEMANTIC_UNCERTAINTY  — automated comparison is unreliable here; the Agent
                         (not Python) has the semantic understanding to judge
                         whether this is really a problem. See "Agent-judgment
                         loop" below.
```

---

## Agent analysis step

Before every migration the Agent performs a structured analysis of the source
query and writes `output/analysis-result.json`:

```json
{
  "structural_elements": ["CTE", "EXISTS", "TABLE_COLLECTION", "ROWNUM"],
  "dependencies": [
    {"name": "FN_CUSTOM",   "kind": "custom_function", "status": "UNRESOLVED"},
    {"name": "<<MACRO>>",   "kind": "placeholder",     "status": "UNRESOLVED"},
    {"name": "COLL_TYPE_T", "kind": "custom_type",     "status": "UNRESOLVED"}
  ],
  "application_placeholders": ["<<MACRO>>"],
  "complexity": "HIGH",
  "migration_readiness": "READY_WITH_UNRESOLVED",
  "requires_template_validation": true,
  "requires_human_review": false
}
```

`dependency_validator.py` reads `migration_readiness` on every run — see
"Dependency resolution rules" below. This is the enforcement mechanism behind
"don't automatically approve a query with unresolved semantic dependencies."

### Dependency resolution rules

| Dependency kind | Definition supplied? | Action | `migration_readiness` |
|----------------|---------------------|--------|---|
| Custom function | Yes | Migrate using the supplied definition | `READY` |
| Custom function | No, but query still meaningful with call preserved | Preserve call as-is; mark UNRESOLVED | `READY_WITH_UNRESOLVED` |
| Custom function | No, and the call is load-bearing for correctness | Do not migrate | `BLOCKED` → Agent writes `REVIEW_REQUIRED` itself |
| Custom type | Yes | Migrate using the supplied definition | `READY` |
| Custom type | No, but query still meaningful with usage preserved | Preserve usage; mark UNRESOLVED | `READY_WITH_UNRESOLVED` |
| Custom type | No, and unsafe to guess at | Do not migrate | `BLOCKED` |
| Application placeholder (`<<…>>`) | Expansion known | Substitute and migrate | `READY` |
| Application placeholder (`<<…>>`) | Expansion unknown | Preserve verbatim; mark UNRESOLVED; this is TEMPLATE_SQL | does not by itself force `BLOCKED` |

**The Agent never invents an implementation for an unknown dependency**, and
Python (`dependency_validator.py`) will not approve a run where
`migration_readiness` is `BLOCKED`, or where `UNRESOLVED` dependencies exist
without an explicit `READY_WITH_UNRESOLVED`.

### TEMPLATE_SQL

A query containing an `<<IDENTIFIER>>` application placeholder is
TEMPLATE_SQL: the placeholder must be preserved verbatim (`structure_validator`
enforces this via `MISSING_PLACEHOLDER`), but the query is not expected to
execute standalone. `postgres_validator.py` detects placeholders automatically
and skips the live `PREPARE` check with a note, instead of producing a false
`SQL_ERROR`.

### Application placeholder rule

Tokens matching `<<IDENTIFIER>>` are application-level macros, not Oracle SQL.
They must be **preserved verbatim** in the migrated output so the application
layer can still substitute them.  The `structure_validator` checks this and
raises `MISSING_PLACEHOLDER` if any are lost.

---

## Agent-judgment loop (SEMANTIC_UNCERTAINTY)

`condition_validator.py` and `join_validator.py` are deliberately
regex/fingerprint-based heuristics, not a SQL parser — they're guardrails,
not a semantic equivalence prover (see "Extending the pipeline" below for
where this should eventually go). When they hit something they can't
reliably compare automatically (a non-equi JOIN predicate, a compound WHERE
predicate, an unusual collection comparison), they emit `SEMANTIC_UNCERTAINTY`
rather than guessing PASS or FAIL.

Python does **not** auto-escalate these to `REVIEW_REQUIRED` — that would
stop a possibly-correct complex query on a technicality the validator simply
couldn't parse. Instead, when a run's *only* remaining issues are
`SEMANTIC_UNCERTAINTY`, `validate.py` exits `4` (`AGENT_REVIEW_NEEDED`) and
defers the call to the Agent, who re-reads the original Oracle query and
either:

- writes `output/agent-judgment.json` with a reasoned justification per
  issue `id` and re-validates (becomes `APPROVED` if that resolves every
  open issue), or
- writes `REVIEW_REQUIRED` to `output/final-status.json` itself if the issue
  is genuinely unresolved.

`output/agent-judgment.json` shape:

```json
{
  "acknowledged": [
    {"issue_id": "bac1d87763", "justification": "Non-equi NVL-guarded join predicate confirmed translated to COALESCE with identical semantics."}
  ]
}
```

An unjustified or empty entry is ignored — Python requires an actual
justification string before it will treat an issue as resolved, and it never
writes one itself.

---

## What Python does NOT do

- ❌ Migrate SQL (Agent's job)
- ❌ Analyze query structure (Agent's job — written to analysis-result.json)
- ❌ Decide when to stop correcting (Agent's job)
- ❌ Judge whether a SEMANTIC_UNCERTAINTY issue is really a problem (Agent's job — Python only records the Agent's own justification)
- ❌ Contain application-specific conversion rules
- ❌ Call the LLM
- ❌ Orchestrate the correction loop
- ❌ Invent implementations for unknown dependencies
- ❌ Override the Agent's own `migration_readiness: BLOCKED` verdict

---

## Live PostgreSQL validation (optional)

Set `POSTGRES_DSN` or pass `--postgres-dsn` to enable.  When not configured,
`postgresql_live` returns `SKIPPED` — which is **not** the same as `PASS`.  The
other seven validators still run and provide meaningful coverage.

```bash
export POSTGRES_DSN="postgresql://user:pass@localhost:5432/mydb"
python validators/validate.py \
    --oracle input/oracle-query.txt \
    --postgres output/postgresql-query.sql
```

### Error classification

The live validator now distinguishes:

| Classification | Meaning | Agent action |
|----------------|---------|--------------|
| `SQL_ERROR` | Column/function/type does not exist; syntax error | Correct the SQL |
| `VALIDATION_ENVIRONMENT_ERROR` | DB connection broken; permission error | Stop; fix infra; do NOT change SQL |
| `SKIPPED` | No DSN configured | No action; other validators ran |

---

## Extending the pipeline

### Adding a new Oracle construct to detect

Add a `(pattern, label)` tuple to `ORACLE_CONSTRUCTS` in
`validators/construct_validator.py`.  Keep it generic — no query-specific
or application-specific patterns.

### Adding a new validator

1. Create `validators/my_validator.py` implementing `validate(oracle_sql, postgres_sql) -> dict`.
2. Import it in `validators/validate.py` and add it to `STATIC_VALIDATORS`.
3. Document it in this README's validator reference table.
