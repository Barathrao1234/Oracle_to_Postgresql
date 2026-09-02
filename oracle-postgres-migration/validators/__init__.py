"""Deterministic validators for the Oracle → PostgreSQL migration pipeline.

Each validator implements:

    validate(oracle_sql: str, postgres_sql: str) -> dict

and returns:

    {
        "status": "PASS" | "FAIL" | "SKIPPED",
        "issues": [
            {
                "type":    "<ISSUE_TYPE>",
                "message": "<human-readable description>"
            },
            ...
        ]
    }

Statuses
--------
PASS     — the validator ran and found no issues.
FAIL     — the validator ran and found one or more hard issues.
SKIPPED  — the validator did not run (e.g. no live DB connection configured).
           SKIPPED must never be treated as equivalent to PASS.

Issue type reference
--------------------
Hard-fail types (drive Agent correction retries):
  STRUCTURE_LOST              — A major clause / structural element disappeared.
  ORACLE_CONSTRUCT_REMAINS    — Oracle-specific syntax survived into the target.
  MISSING_PLACEHOLDER         — An application placeholder (<<MACRO>>) was lost.
  SQL_ERROR                   — PostgreSQL live check rejected the query (SQL bug).
  INCOMPLETE_SELECT_LIST      — Target SELECT has fewer expressions than source.
  INCOMPLETE_GROUP_BY         — GROUP BY column(s) dropped.
  INCOMPLETE_ORDER_BY         — ORDER BY column(s) dropped.
  ORDER_BY_DIRECTION_CHANGED  — ASC/DESC flipped.
  MISSING_ROW_LIMIT           — Pagination row count disappeared.
  ROW_LIMIT_CHANGED           — Pagination row count changed.
  MISSING_CONDITION           — WHERE predicate disappeared.
  CONDITION_VALUE_CHANGED     — WHERE literal value silently changed.
  MISSING_JOIN                — Joined table disappeared from target.
  JOIN_TYPE_MISMATCH          — Join type changed (INNER→LEFT, etc.).
  JOIN_CONDITION_MISMATCH     — ON-clause equi-join pair disappeared.
  SYNTAX                      — Unbalanced parentheses / dangling clause.
  EMPTY_OUTPUT                — Output file is empty.
  MARKDOWN_FENCE_IN_OUTPUT    — Output contains markdown code fences.
  CONVERSATIONAL_TEXT_IN_OUTPUT — Output starts with prose, not SQL.
  UNEXPECTED_OUTPUT_START     — First statement doesn't start with SQL keyword.
  POSTGRESQL_ERROR            — Legacy alias for SQL_ERROR from live validator.

Escalation-only types (promote run to REVIEW_REQUIRED — do NOT retry SQL):
  SEMANTIC_UNCERTAINTY        — Validator cannot reliably compare a construct
                                automatically; human review required.
  VALIDATION_ENVIRONMENT_ERROR — Live DB infrastructure broken; SQL may be
                                correct — do NOT modify it.

Design rules (all validators must follow these)
-----------------------------------------------
- Never raise on "normal" bad input.  Catch parsing errors and return them
  as FAIL issues.
- Stay generic: no Oracle-query-specific or application-specific hardcoding.
- Contain NO migration logic: compare source vs. target, never rewrite either.
- Keep each validator focused on one concern; cross-concern checks belong in
  a separate validator.
- Never automatically fail a construct the Agent has correctly converted.
  Prefer SEMANTIC_UNCERTAINTY over FAIL when the outcome is ambiguous.

Entry point
-----------
`validators/validate.py` is the single CLI entry point that runs all
validators, aggregates results, manages the attempt counter, and writes the
compact reports that the Copilot Agent reads.  There is deliberately no
orchestrator or retry-loop in this package — the Copilot Agent owns that
logic (see .github/agents/oracle-postgres-migrator.agent.md).
"""
