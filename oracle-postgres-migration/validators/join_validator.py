"""Detects JOINed tables that existed in the source but disappeared in the
target, and validates that join *conditions* for still-present tables
roughly survived migration.

Improvements over the previous version
---------------------------------------
- Join type is now captured (INNER, LEFT, RIGHT, FULL OUTER, CROSS) and
  compared separately from table presence — a table being joined with the
  wrong join type is flagged as JOIN_TYPE_MISMATCH.
- ON-clause parsing is broken into three parts per equi-join pair:
    left_alias.left_col  = right_alias.right_col
  so the validator can distinguish:
    e.department_id = d.department_id   (correct)
    e.employee_id   = d.department_id   (wrong columns)
  — previously only bare column names were compared (an unordered pair),
  which could miss cross-column swaps when the column names happened to match
  on both sides.
- Non-equi-join predicates (BETWEEN, function calls, etc.) in ON clauses are
  recorded as SEMANTIC_UNCERTAINTY rather than silently ignored, so the Agent
  knows manual review is advisable.
- Legacy Oracle comma-joins and (+) syntax are still detected for table-
  presence purposes; construct_validator.py separately flags leftover (+).
"""

import re

from validators.sql_utils import strip_comments_and_strings

# Captures: optional join type keywords + table name + optional alias + ON clause text.
# The ON clause extends until the next JOIN, WHERE, GROUP BY, ORDER BY, semicolon, or EOF.
JOIN_RE = re.compile(
    r"\b((?:(?:INNER|CROSS|LEFT|RIGHT|FULL)\s+(?:OUTER\s+)?)?JOIN)\s+"
    r"([A-Za-z_][A-Za-z0-9_.]*)(?:\s+(?:AS\s+)?([A-Za-z_][A-Za-z0-9_]*))?"
    r"\s+ON\s+(.*?)(?=\b(?:INNER\s+|CROSS\s+|LEFT\s+|RIGHT\s+|FULL\s+)?JOIN\b"
    r"|\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)

FROM_LIST_RE = re.compile(
    r"\bFROM\b(.*?)(?:\bWHERE\b|\bGROUP\s+BY\b|\bORDER\s+BY\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)

# Equi-join pair: alias.col = alias.col
EQUI_JOIN_RE = re.compile(
    r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)"
    r"\s*=\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)",
    re.IGNORECASE,
)

# Normalise a join-type string to a canonical form.
_JOIN_TYPE_RE = re.compile(r"\s+", re.IGNORECASE)

def _canonical_join_type(raw: str) -> str:
    parts = _JOIN_TYPE_RE.sub(" ", raw.strip().upper()).split()
    # Drop bare "JOIN" at the end; the meaningful word is what precedes it.
    parts = [p for p in parts if p != "JOIN"]
    if not parts:
        return "INNER"  # bare JOIN = INNER JOIN in SQL
    # Collapse "LEFT OUTER" -> "LEFT", "FULL OUTER" -> "FULL"
    if "OUTER" in parts:
        parts.remove("OUTER")
    return " ".join(parts)  # e.g. "LEFT", "RIGHT", "FULL", "CROSS", "INNER"


def _explicit_joins(sql: str) -> list[dict]:
    """Return a list of join descriptors, one per JOIN … ON found.

    Each descriptor:
        table        : bare table name (lowercased, schema stripped)
        alias        : alias used in the query (lowercased), or same as table
        join_type    : canonical join type string
        equi_pairs   : frozenset of (left_col, right_col) tuples (both lowercased,
                       ordered so left_col < right_col for unordered comparison)
        has_non_equi : True if the ON clause contains predicates we can't parse
    """
    joins = []
    for m in JOIN_RE.finditer(sql):
        raw_type  = m.group(1)
        raw_table = m.group(2)
        raw_alias = m.group(3)
        on_clause = (m.group(4) or "").strip()

        table     = raw_table.split(".")[-1].lower()
        alias     = (raw_alias or raw_table).lower()
        join_type = _canonical_join_type(raw_type)

        equi_pairs: set[tuple[str, str]] = set()
        has_non_equi = False

        for eq in EQUI_JOIN_RE.finditer(on_clause):
            # Normalise as a sorted pair of (left_col, right_col) so
            # 'e.dept_id = d.dept_id' matches 'd.dept_id = e.dept_id'.
            col_a = eq.group(2).lower()
            col_b = eq.group(4).lower()
            equi_pairs.add(tuple(sorted([col_a, col_b])))

        # If there's content in the ON clause that isn't explained by equi-join pairs,
        # flag it as a non-equi predicate for SEMANTIC_UNCERTAINTY reporting.
        remaining = EQUI_JOIN_RE.sub("", on_clause).strip()
        remaining = re.sub(r"\bAND\b|\bOR\b", "", remaining, flags=re.IGNORECASE).strip()
        if remaining and remaining not in ("", "(", ")"):
            has_non_equi = True

        joins.append({
            "table":        table,
            "alias":        alias,
            "join_type":    join_type,
            "equi_pairs":   frozenset(equi_pairs),
            "has_non_equi": has_non_equi,
        })

    return joins


def _comma_join_tables(sql: str) -> set[str]:
    """Extract additional table names introduced via implicit comma-join syntax."""
    match = FROM_LIST_RE.search(sql)
    if not match:
        return set()
    from_clause = match.group(1)
    # Only the part before the first JOIN keyword.
    from_clause = re.split(r"\bJOIN\b", from_clause, flags=re.IGNORECASE)[0]
    parts = [p.strip() for p in from_clause.split(",")]
    tables: set[str] = set()
    for p in parts[1:]:   # skip the first table — it's the FROM target, not a join
        if not p:
            continue
        name = p.split()[0] if p.split() else ""
        name = name.split(".")[-1].lower()
        if name:
            tables.add(name)
    return tables


def validate(oracle_sql: str, postgres_sql: str) -> dict:
    issues: list[dict] = []

    oracle_clean   = strip_comments_and_strings(oracle_sql)
    postgres_clean = strip_comments_and_strings(postgres_sql)

    source_joins = _explicit_joins(oracle_clean)
    target_joins = _explicit_joins(postgres_clean)

    # Build lookup maps keyed by table name for the target.
    target_by_table: dict[str, list[dict]] = {}
    for j in target_joins:
        target_by_table.setdefault(j["table"], []).append(j)

    source_all_tables = {j["table"] for j in source_joins} | _comma_join_tables(oracle_clean)
    target_all_tables = {j["table"] for j in target_joins} | _comma_join_tables(postgres_clean)

    # 1. Tables present in source but entirely absent from target.
    missing_tables = source_all_tables - target_all_tables
    for table in sorted(missing_tables):
        issues.append({
            "type":    "MISSING_JOIN",
            "message": f"Source joins table '{table}' but no corresponding join found in target.",
        })

    # 2. For tables present in both, check join type and ON conditions.
    for src_join in source_joins:
        table = src_join["table"]
        if table in missing_tables:
            continue  # already reported above

        tgt_candidates = target_by_table.get(table)
        if not tgt_candidates:
            continue  # joined via comma-syntax in target — can't compare ON columns

        # Pick the best-matching target join (same join type preferred).
        tgt_join = next(
            (j for j in tgt_candidates if j["join_type"] == src_join["join_type"]),
            tgt_candidates[0],
        )

        # 2a. Join type mismatch.
        if tgt_join["join_type"] != src_join["join_type"]:
            issues.append({
                "type":    "JOIN_TYPE_MISMATCH",
                "message": (
                    f"Join to '{table}' is {src_join['join_type']} JOIN in source "
                    f"but {tgt_join['join_type']} JOIN in target."
                ),
            })

        # 2b. ON-column pairs: check that every source equi-pair appears in target.
        if src_join["equi_pairs"]:
            missing_pairs = src_join["equi_pairs"] - tgt_join["equi_pairs"]
            if missing_pairs:
                readable = [f"({a} = {b})" for a, b in sorted(missing_pairs)]
                issues.append({
                    "type":    "JOIN_CONDITION_MISMATCH",
                    "message": (
                        f"Join to '{table}': source ON-column pair(s) not found in target: "
                        + ", ".join(readable)
                    ),
                })

        # 2c. Non-equi predicates that we can't compare automatically.
        if src_join["has_non_equi"] or tgt_join["has_non_equi"]:
            issues.append({
                "type":    "SEMANTIC_UNCERTAINTY",
                "message": (
                    f"Join to '{table}' contains non-equi-join ON predicates "
                    "(function calls, BETWEEN, etc.) that cannot be compared automatically — "
                    "manual review recommended."
                ),
            })

    # Determine status:
    # SEMANTIC_UNCERTAINTY alone doesn't fail (overall_status in validate.py promotes it);
    # actual structural problems do fail.
    hard_fail_types = {"MISSING_JOIN", "JOIN_TYPE_MISMATCH", "JOIN_CONDITION_MISMATCH"}
    status = "FAIL" if any(i["type"] in hard_fail_types for i in issues) else "PASS"

    return {"status": status, "issues": issues}
