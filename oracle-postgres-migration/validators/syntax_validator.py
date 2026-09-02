"""Basic syntax sanity checks on the migrated PostgreSQL query.

This uses `sqlparse` for lightweight, dependency-light parsing. It is NOT a
full PostgreSQL grammar validator — `postgres_validator.py` handles the
authoritative check when a live database is available. This validator
catches obvious problems cheaply and without needing a database connection.
"""

import re

try:
    import sqlparse
except ImportError:  # pragma: no cover
    sqlparse = None


def _check_balanced(sql: str) -> list[str]:
    errors = []
    depth = 0
    in_single = False
    for i, ch in enumerate(sql):
        if ch == "'" and (i == 0 or sql[i - 1] != "\\"):
            in_single = not in_single
        if in_single:
            continue
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                errors.append("Unbalanced parentheses: unexpected ')'")
                depth = 0
    if depth > 0:
        errors.append(f"Unbalanced parentheses: {depth} unclosed '('")
    if in_single:
        errors.append("Unterminated string literal")
    return errors


def _check_dangling_clause(sql: str) -> list[str]:
    """Catch obviously truncated clauses like 'WHERE;' or 'FROM ORDER BY'."""
    errors = []
    stripped = re.sub(r";\s*$", "", sql.strip())
    if re.search(r"\b(WHERE|AND|OR|FROM|JOIN|ON|SELECT|SET)\s*$", stripped, re.IGNORECASE):
        errors.append("Query appears to end with a dangling clause / missing predicate")
    return errors


def validate(oracle_sql: str, postgres_sql: str) -> dict:
    issues = []
    text = postgres_sql.strip()

    if not text:
        return {
            "status": "FAIL",
            "issues": [{"type": "SYNTAX", "message": "Generated PostgreSQL query is empty"}],
        }

    for msg in _check_balanced(text):
        issues.append({"type": "SYNTAX", "message": msg})

    for msg in _check_dangling_clause(text):
        issues.append({"type": "SYNTAX", "message": msg})

    if sqlparse is not None:
        try:
            parsed = sqlparse.parse(text)
            if not parsed or not any(str(stmt).strip() for stmt in parsed):
                issues.append({"type": "SYNTAX", "message": "sqlparse could not parse any statement"})
            else:
                stmt = parsed[0]
                if stmt.get_type() == "UNKNOWN":
                    issues.append({
                        "type": "SYNTAX",
                        "message": "sqlparse could not determine a valid statement type "
                                   "(query may be malformed)",
                    })
        except Exception as exc:  # pragma: no cover
            issues.append({"type": "SYNTAX", "message": f"sqlparse raised an error: {exc}"})

    status = "FAIL" if issues else "PASS"
    return {"status": status, "issues": issues}
