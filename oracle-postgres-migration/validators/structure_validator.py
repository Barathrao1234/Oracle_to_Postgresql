"""Compares which major query clauses and structural constructs exist in the
source vs. the target.

Improvements over the previous version
---------------------------------------
- Detects CTE presence (WITH ... AS) separately from the main SELECT.
- Detects MULTISET, TABLE(...), and EXISTS subquery structures — these are
  deep structural elements that should survive migration (possibly transformed).
- Detects ROWNUM at multiple query levels (not just the outermost query), so
  pagination in nested subqueries is also checked.
- Detects application placeholders (<<IDENTIFIER>>) and ensures none were
  silently dropped during migration.
- Still does NOT require clauses to be textually identical — Oracle and
  PostgreSQL syntax legitimately differ.  It only checks that a structural
  category present in the source didn't silently disappear in the target.

Deeper semantic completeness (SELECT expression count, GROUP BY columns,
ORDER BY direction, row-limit values) is handled by
`completeness_validator.py`, not here.
"""

import re

from validators.sql_utils import PLACEHOLDER_RE, strip_comments_and_strings

# ---------------------------------------------------------------------------
# Basic clause patterns (unchanged from original)
# ---------------------------------------------------------------------------

CLAUSES = ["SELECT", "FROM", "JOIN", "WHERE", "GROUP BY", "HAVING", "ORDER BY"]

CLAUSE_PATTERNS = {
    "SELECT":   r"\bSELECT\b",
    "FROM":     r"\bFROM\b",
    "JOIN":     r"\bJOIN\b",
    "WHERE":    r"\bWHERE\b",
    "GROUP BY": r"\bGROUP\s+BY\b",
    "HAVING":   r"\bHAVING\b",
    "ORDER BY": r"\bORDER\s+BY\b",
}

# ---------------------------------------------------------------------------
# Extended structural patterns (new)
# ---------------------------------------------------------------------------

# CTE:  WITH name AS (
CTE_RE = re.compile(r"\bWITH\b\s+[A-Za-z_][A-Za-z0-9_]*\s+AS\s*\(", re.IGNORECASE)

# MULTISET operator / constructor
MULTISET_RE = re.compile(r"\bMULTISET\b", re.IGNORECASE)

# TABLE(...) — Oracle collection unnesting; should become unnest(...) or a
# LATERAL join in PostgreSQL.
TABLE_EXPR_RE = re.compile(r"\bTABLE\s*\(", re.IGNORECASE)

# EXISTS ( SELECT ... )
EXISTS_RE = re.compile(r"\bEXISTS\s*\(", re.IGNORECASE)

# ROWNUM anywhere (including inside subqueries/CTEs)
ROWNUM_RE = re.compile(r"\bROWNUM\b", re.IGNORECASE)

# Application placeholders: <<IDENTIFIER>> — imported from sql_utils so
# every validator (and postgres_validator's TEMPLATE_SQL skip) agrees on
# what counts as a placeholder.

# PostgreSQL equivalents of TABLE(...) — unnest or LATERAL
UNNEST_RE = re.compile(r"\bUNNEST\s*\(|\bLATERAL\b", re.IGNORECASE)

# PostgreSQL row-limit constructs that replace ROWNUM
ROWNUM_PG_RE = re.compile(r"\bLIMIT\b|\bFETCH\s+FIRST\b", re.IGNORECASE)


def _present(pattern, sql: str) -> bool:
    return bool(re.search(pattern, sql, re.IGNORECASE))


def _count(pattern: re.Pattern, sql: str) -> int:
    return len(pattern.findall(sql))


def _extract_placeholders(sql: str) -> list[str]:
    return PLACEHOLDER_RE.findall(sql)


def validate(oracle_sql: str, postgres_sql: str) -> dict:
    issues = []

    oracle_clean   = strip_comments_and_strings(oracle_sql)
    postgres_clean = strip_comments_and_strings(postgres_sql)

    # ------------------------------------------------------------------
    # 1. Basic clause presence (original behaviour preserved)
    # ------------------------------------------------------------------
    for clause in CLAUSES:
        pattern = CLAUSE_PATTERNS[clause]
        in_source = _present(pattern, oracle_clean)
        in_target = _present(pattern, postgres_clean)
        if in_source and not in_target:
            issues.append({
                "type":    "STRUCTURE_LOST",
                "message": f"Source query has a {clause} clause that is missing in the target",
            })

    # ------------------------------------------------------------------
    # 2. CTE presence
    # ------------------------------------------------------------------
    if CTE_RE.search(oracle_clean):
        if not CTE_RE.search(postgres_clean):
            issues.append({
                "type":    "STRUCTURE_LOST",
                "message": "Source query uses CTEs (WITH … AS) but no CTE block found in target",
            })

    # ------------------------------------------------------------------
    # 3. MULTISET
    # ------------------------------------------------------------------
    if MULTISET_RE.search(oracle_clean) and not MULTISET_RE.search(postgres_clean):
        # PostgreSQL doesn't have MULTISET; the Agent should have converted it.
        # Flag as SEMANTIC_UNCERTAINTY because the correct PostgreSQL equivalent
        # may look very different — we can't do a simple keyword match.
        issues.append({
            "type":    "SEMANTIC_UNCERTAINTY",
            "message": (
                "Source contains MULTISET — verify the target correctly represents "
                "the set/array semantics (manual review recommended)"
            ),
        })

    # ------------------------------------------------------------------
    # 4. TABLE(...) expression — should become unnest/LATERAL in PostgreSQL
    # ------------------------------------------------------------------
    if TABLE_EXPR_RE.search(oracle_clean):
        # In PostgreSQL, TABLE(...) would be an error; it must be converted.
        # Check that a TABLE(...) is NOT still present AND that an unnest/LATERAL
        # is present to account for the conversion.
        if TABLE_EXPR_RE.search(postgres_clean):
            issues.append({
                "type":    "ORACLE_CONSTRUCT_REMAINS",
                "message": "Oracle TABLE(...) expression still present in target SQL",
            })
        elif not UNNEST_RE.search(postgres_clean):
            issues.append({
                "type":    "SEMANTIC_UNCERTAINTY",
                "message": (
                    "Source has TABLE(...) (Oracle collection unnesting) but target has "
                    "neither TABLE(...) nor UNNEST/LATERAL — verify the conversion is correct"
                ),
            })

    # ------------------------------------------------------------------
    # 5. EXISTS subquery structure
    # ------------------------------------------------------------------
    src_exists_count = _count(EXISTS_RE, oracle_clean)
    tgt_exists_count = _count(EXISTS_RE, postgres_clean)
    if src_exists_count > 0 and tgt_exists_count == 0:
        issues.append({
            "type":    "STRUCTURE_LOST",
            "message": (
                f"Source has {src_exists_count} EXISTS subquery/subqueries "
                "but none found in target"
            ),
        })
    elif src_exists_count > tgt_exists_count:
        issues.append({
            "type":    "STRUCTURE_LOST",
            "message": (
                f"Source has {src_exists_count} EXISTS subquery/subqueries "
                f"but target only has {tgt_exists_count}"
            ),
        })

    # ------------------------------------------------------------------
    # 6. ROWNUM / pagination
    #    Oracle uses ROWNUM (anywhere in the query, including subqueries).
    #    PostgreSQL uses LIMIT or FETCH FIRST.
    #    We check that pagination semantics survive, not the keyword itself.
    # ------------------------------------------------------------------
    src_has_rownum = bool(ROWNUM_RE.search(oracle_clean))
    tgt_has_rownum = bool(ROWNUM_RE.search(postgres_clean))
    tgt_has_pg_limit = bool(ROWNUM_PG_RE.search(postgres_clean))

    if tgt_has_rownum:
        # construct_validator already flags ROWNUM remaining; no double-report here.
        pass
    elif src_has_rownum and not tgt_has_pg_limit:
        issues.append({
            "type":    "STRUCTURE_LOST",
            "message": (
                "Source uses ROWNUM for pagination but target has neither "
                "LIMIT nor FETCH FIRST"
            ),
        })

    # ------------------------------------------------------------------
    # 7. Application placeholders — must be preserved verbatim
    # ------------------------------------------------------------------
    src_placeholders = set(_extract_placeholders(oracle_sql))   # raw SQL, not stripped
    tgt_placeholders = set(_extract_placeholders(postgres_sql))

    missing_placeholders = src_placeholders - tgt_placeholders
    for ph in sorted(missing_placeholders):
        issues.append({
            "type":    "MISSING_PLACEHOLDER",
            "message": (
                f"Application placeholder '{ph}' present in source but missing "
                "from target — placeholders must be preserved verbatim"
            ),
        })

    status = "FAIL" if any(
        i["type"] in {
            "STRUCTURE_LOST", "ORACLE_CONSTRUCT_REMAINS", "MISSING_PLACEHOLDER"
        }
        for i in issues
    ) else "PASS"

    return {"status": status, "issues": issues}
