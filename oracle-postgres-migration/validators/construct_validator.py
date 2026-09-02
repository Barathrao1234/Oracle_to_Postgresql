"""Detects Oracle-specific constructs that should not survive migration.

This is deliberately generic: it's a list of well-known Oracle-only
syntax/functions, not a set of application-specific rules. Add to
ORACLE_CONSTRUCTS if a genuinely new generic Oracle construct is found —
do not add query-specific or app-specific hacks here.
"""

import re

# Oracle-only constructs that have no direct PostgreSQL equivalent under the
# same name. If any of these still appear in the migrated query, the
# migration is incomplete.
ORACLE_CONSTRUCTS = [
    (r"\bNVL2?\s*\(", "NVL/NVL2"),
    (r"\bSYSDATE\b", "SYSDATE"),
    (r"\bROWNUM\b", "ROWNUM"),
    (r"\bDECODE\s*\(", "DECODE"),
    (r"\bCONNECT\s+BY\b", "CONNECT BY"),
    (r"\bSTART\s+WITH\b", "START WITH"),
    (r"\(\s*\+\s*\)", "(+) outer join syntax"),
    (r"\bMINUS\b", "MINUS (use EXCEPT in PostgreSQL)"),
    (r"\bFROM\s+DUAL\b", "FROM DUAL"),
    (r"\bDUAL\b", "DUAL"),
    (r"\bROWID\b", "ROWID"),
    (r"\bTO_NUMBER\s*\(", "TO_NUMBER (verify PostgreSQL cast semantics)"),
    (r"\bWM_CONCAT\s*\(", "WM_CONCAT (use string_agg)"),
    (r"\bSYS_CONTEXT\s*\(", "SYS_CONTEXT"),
    (r"\bNEXTVAL\b(?!\s*\()", "sequence.NEXTVAL syntax (PostgreSQL uses nextval('seq'))"),
    (r"\bCURRVAL\b(?!\s*\()", "sequence.CURRVAL syntax (PostgreSQL uses currval('seq'))"),
]


def _strip_comments_and_strings(sql: str) -> str:
    """Remove string literals and comments so constructs inside them
    aren't falsely flagged."""
    sql = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return sql


def validate(oracle_sql: str, postgres_sql: str) -> dict:
    issues = []
    cleaned = _strip_comments_and_strings(postgres_sql)

    for pattern, label in ORACLE_CONSTRUCTS:
        if re.search(pattern, cleaned, flags=re.IGNORECASE):
            issues.append({
                "type": "ORACLE_CONSTRUCT_REMAINS",
                "message": f"Oracle-specific construct still present in target: {label}",
            })

    status = "FAIL" if issues else "PASS"
    return {"status": status, "issues": issues}
