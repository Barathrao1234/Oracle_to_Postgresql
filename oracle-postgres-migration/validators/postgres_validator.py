"""Strongest available validation: ask a real PostgreSQL server whether the
migrated query is valid, using PREPARE (checked without side effects or
requiring real data/rows, only that referenced tables/columns/functions
exist against the connected schema).

This validator is OPTIONAL. It only runs if a connection is configured via
the POSTGRES_DSN environment variable (or --postgres-dsn on the CLI).

IMPORTANT: when no connection is available, this returns status "SKIPPED",
never "PASS". Skipped is not the same as passed — the overall pipeline
report must not represent "we didn't check" as "we checked and it's fine".

Error classification
--------------------
This validator distinguishes three outcomes:

  SQL_ERROR
    PostgreSQL understood the connection but rejected the query.
    Examples: column does not exist, function does not exist,
    syntax error, type mismatch.
    → The SQL is likely wrong; the Agent should try to correct it.

  VALIDATION_ENVIRONMENT_ERROR
    The connection itself worked but something in the validation
    infrastructure broke (e.g. permission denied on PREPARE,
    unexpected server error unrelated to the query content).
    → Do NOT modify the SQL; report the infrastructure problem.

  SKIPPED
    No DSN configured, or psycopg2 not installed.
    → The check simply did not run.  Other validators still provide coverage.
"""

import os
import re
import uuid

from validators.sql_utils import is_template_sql

try:
    import psycopg2
    import psycopg2.errors as pgerrors
except ImportError:  # pragma: no cover
    psycopg2 = None
    pgerrors = None


# PostgreSQL error codes that indicate the SQL itself is wrong
# (class 42 = Syntax Error or Access Rule Violation).
_SQL_ERROR_CLASSES = {
    "42",   # undefined_table, undefined_column, syntax_error, etc.
    "22",   # data exception (type mismatch, value out of range, etc.)
    "2B",   # dependent privilege descriptors still exist (rare but SQL-level)
}

# Patterns in error messages that strongly suggest the SQL content is the issue
_SQL_ERROR_PATTERNS = re.compile(
    r"column .+ does not exist"
    r"|function .+ does not exist"
    r"|operator does not exist"
    r"|syntax error"
    r"|relation .+ does not exist"
    r"|type .+ does not exist"
    r"|invalid input syntax",
    re.IGNORECASE,
)


def _is_available(dsn: str | None) -> bool:
    return bool(dsn) and psycopg2 is not None


def _classify_error(exc: Exception) -> str:
    """Return 'SQL_ERROR' or 'VALIDATION_ENVIRONMENT_ERROR'."""
    message = str(exc).strip()

    # psycopg2 exposes a pgcode attribute on its exceptions.
    pgcode = getattr(exc, "pgcode", None) or ""
    if pgcode[:2] in _SQL_ERROR_CLASSES:
        return "SQL_ERROR"

    # Fall back to message inspection for generic Exception subclasses.
    if _SQL_ERROR_PATTERNS.search(message):
        return "SQL_ERROR"

    return "VALIDATION_ENVIRONMENT_ERROR"


def validate(oracle_sql: str, postgres_sql: str, dsn: str | None = None) -> dict:
    dsn = dsn or os.environ.get("POSTGRES_DSN")

    if is_template_sql(postgres_sql):
        # TEMPLATE_SQL: contains <<PLACEHOLDER>> application macros that are
        # substituted by the application layer, not by migration. It is
        # expected NOT to be directly executable, so PREPARE-ing it against
        # a live database would produce a false SQL_ERROR. Do not require
        # PREPARE to succeed unless every placeholder has been resolved.
        return {
            "status": "SKIPPED",
            "issues": [],
            "note": (
                "Target SQL contains application placeholder(s) (<<...>>) — "
                "this is TEMPLATE_SQL, not standalone executable SQL. Live "
                "PostgreSQL PREPARE validation was skipped; other validators "
                "(syntax/constructs/structure/conditions/joins/completeness) "
                "still provide coverage, and structure_validator confirms "
                "each placeholder was preserved verbatim."
            ),
        }

    if not _is_available(dsn):
        return {
            "status": "SKIPPED",
            "issues": [],
            "note": (
                "No POSTGRES_DSN configured (or psycopg2 not installed); live PostgreSQL "
                "validation did not run. This is NOT a pass — it means this check simply "
                "didn't happen. Other validators (syntax/constructs/structure/conditions/"
                "joins/completeness) still provide coverage."
            ),
        }

    stmt_name = f"validate_{uuid.uuid4().hex[:8]}"
    sql = postgres_sql.strip().rstrip(";")

    conn = None
    try:
        conn = psycopg2.connect(dsn)
        conn.autocommit = False
        with conn.cursor() as cur:
            cur.execute(f"PREPARE {stmt_name} AS {sql}")
            cur.execute(f"DEALLOCATE {stmt_name}")
        conn.rollback()
        return {"status": "PASS", "issues": []}

    except Exception as exc:  # noqa: BLE001
        if conn is not None:
            try:
                conn.rollback()
            except Exception:
                pass

        message = str(exc).strip().splitlines()[0] if str(exc).strip() else repr(exc)
        error_type = _classify_error(exc)

        location = None
        for line in str(exc).splitlines():
            if line.strip().upper().startswith("LINE "):
                location = line.strip()
                break

        issue: dict = {
            "type":    error_type,
            "message": f"PostgreSQL validation: {message}",
        }
        if location:
            issue["location"] = location

        if error_type == "VALIDATION_ENVIRONMENT_ERROR":
            # Do not set status to FAIL — the SQL may be correct; the env is broken.
            # validate.py will see this issue type and should not trigger a correction.
            return {
                "status": "SKIPPED",
                "issues": [issue],
                "note": (
                    "Live validation encountered an infrastructure/environment error "
                    "(not a SQL error). Do NOT modify the SQL. Fix the validation "
                    "environment and re-run."
                ),
            }

        # SQL_ERROR → genuine problem with the query
        return {"status": "FAIL", "issues": [issue]}

    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
