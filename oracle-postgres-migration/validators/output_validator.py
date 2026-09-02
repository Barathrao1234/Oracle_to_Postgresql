"""Sanity-checks that output/postgresql-query.sql actually contains SQL,
not leftover conversational text or markdown code fences the Agent might
accidentally include, e.g.:

    Here is the migrated query:

    ```sql
    SELECT ...
    ```

This is a cheap, deterministic guard that runs before the "real" validators
bother trying to parse the file.
"""

import re

SQL_START_KEYWORDS = (
    "SELECT", "WITH", "INSERT", "UPDATE", "DELETE", "MERGE",
)

MARKDOWN_FENCE_RE = re.compile(r"^```", re.MULTILINE)
CONVERSATIONAL_RE = re.compile(
    r"^(here'?s|here is|this is|the migrated|below is|i've|i have)\b",
    re.IGNORECASE,
)


def validate(oracle_sql: str, postgres_sql: str) -> dict:
    issues = []
    text = postgres_sql.strip()

    if not text:
        return {
            "status": "FAIL",
            "issues": [{"type": "EMPTY_OUTPUT", "message": "output/postgresql-query.sql is empty"}],
        }

    if MARKDOWN_FENCE_RE.search(text):
        issues.append({
            "type": "MARKDOWN_FENCE_IN_OUTPUT",
            "message": "Output file contains markdown code fences (```); it should contain "
                       "raw SQL only",
        })

    first_line = text.splitlines()[0].strip()
    if CONVERSATIONAL_RE.match(first_line):
        issues.append({
            "type": "CONVERSATIONAL_TEXT_IN_OUTPUT",
            "message": f"Output file appears to start with conversational text, not SQL: "
                       f"'{first_line[:80]}'",
        })

    # First meaningful (non-comment, non-blank) line should start with a
    # recognized SQL keyword.
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        first_word = re.split(r"[\s(]", stripped, maxsplit=1)[0].upper()
        if first_word not in SQL_START_KEYWORDS:
            issues.append({
                "type": "UNEXPECTED_OUTPUT_START",
                "message": f"Output file's first statement doesn't start with a recognized SQL "
                           f"keyword ({'/'.join(SQL_START_KEYWORDS)}); found: '{stripped[:80]}'",
            })
        break

    status = "FAIL" if issues else "PASS"
    return {"status": status, "issues": issues}
