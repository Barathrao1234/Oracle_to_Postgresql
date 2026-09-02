"""Small, dependency-light SQL text helpers shared by several validators.

These are intentionally generic (paren-aware comma/keyword splitting, clause
extraction) rather than query-specific, so validators can be extended to
cover CTEs/subqueries later without every validator re-implementing its own
ad-hoc parsing. This is NOT a full SQL parser — it's "parenthesis and
string-literal aware regex", which is enough for structural sanity checks.
"""

import re

# Application-level placeholders/macros, e.g. <<JOIN_ALT_BATCH_OUT>>. These
# are not Oracle or PostgreSQL syntax — they're expanded by the application
# layer at runtime. A query containing one is TEMPLATE_SQL: it is expected
# to be preserved verbatim, and it will generally NOT execute standalone
# (e.g. against a live PostgreSQL PREPARE), so validators that require
# syntactic/executable completeness must treat it differently. See
# postgres_validator.py and the "Application placeholders" section of the
# agent instructions.
PLACEHOLDER_RE = re.compile(r"<<[A-Za-z_][A-Za-z0-9_]*>>")


def is_template_sql(sql: str) -> bool:
    """True if `sql` contains one or more <<PLACEHOLDER>> application macros."""
    return bool(PLACEHOLDER_RE.search(sql))


def strip_comments_and_strings(sql: str) -> str:
    """Blank out comments and string literal contents so keywords/values
    inside them don't get mistaken for SQL structure."""
    sql = re.sub(r"--.*?$", " ", sql, flags=re.MULTILINE)
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"'(?:[^']|'')*'", "''", sql)
    return sql


def split_top_level(text: str, delimiter: str = ",") -> list[str]:
    """Split `text` on `delimiter`, but only at paren-depth 0 and outside
    string literals. Used for SELECT expression lists, GROUP BY columns,
    ORDER BY columns, etc."""
    parts = []
    depth = 0
    current = ""
    in_string = False
    i = 0
    while i < len(text):
        ch = text[i]
        if ch == "'" and not in_string:
            in_string = True
            current += ch
        elif ch == "'" and in_string:
            in_string = False
            current += ch
        elif in_string:
            current += ch
        elif ch == "(":
            depth += 1
            current += ch
        elif ch == ")":
            depth -= 1
            current += ch
        elif depth == 0 and text[i:i + len(delimiter)] == delimiter:
            parts.append(current.strip())
            current = ""
            i += len(delimiter) - 1
        else:
            current += ch
        i += 1
    if current.strip():
        parts.append(current.strip())
    return [p for p in parts if p]


def extract_clause(sql: str, start_pattern: str, end_patterns: list[str]) -> str:
    """Extract the text of a clause starting at `start_pattern` (a regex,
    not including it in the result) up to the first of `end_patterns` (or
    a trailing semicolon / end of string)."""
    end_alt = "|".join(end_patterns)
    pattern = re.compile(
        rf"{start_pattern}(.*?)(?:{end_alt}|;|$)",
        re.IGNORECASE | re.DOTALL,
    )
    match = pattern.search(sql)
    return match.group(1).strip() if match else ""


# Common "next clause" boundary patterns, reused by several extractors.
CLAUSE_BOUNDARIES = [
    r"\bFROM\b", r"\bWHERE\b", r"\bGROUP\s+BY\b", r"\bHAVING\b",
    r"\bORDER\s+BY\b", r"\bFETCH\s+FIRST\b", r"\bLIMIT\b", r"\bOFFSET\b",
]
