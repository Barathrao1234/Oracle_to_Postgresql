"""Detects predicates that existed in the source WHERE clause but appear to
have been dropped or materially altered in the target.

Changes from v1
---------------
- Boolean operator inventory: counts top-level AND / OR / IS NULL / BETWEEN /
  LIKE / EXISTS occurrences; flags significant drops.
- Bind-parameter pattern detection: recognises :param_name style parameters
  (Oracle) and $N / %(name)s (PostgreSQL); tolerates dialect renaming while
  checking that the *count* of bind parameters hasn't shrunk.
- Nested flag-pattern detection: patterns like
  (:p_flag = 'true' AND col = 1) OR (:p_flag = 'false' AND col IS NULL)
  are identified and a SEMANTIC_UNCERTAINTY is emitted so a human can
  confirm they survived intact.
- Retains all v1 logic: per-predicate column/operator/value fingerprinting,
  SEMANTIC_UNCERTAINTY for unparseable predicates.
"""

import re

from validators.sql_utils import strip_comments_and_strings

# ---------------------------------------------------------------------------
# WHERE clause extraction
# ---------------------------------------------------------------------------
WHERE_RE = re.compile(
    r"\bWHERE\b(.*?)(?:\bGROUP\s+BY\b|\bHAVING\b|\bORDER\s+BY\b|\bFETCH\s+FIRST\b|\bLIMIT\b|;|$)",
    re.IGNORECASE | re.DOTALL,
)

FUNC_WRAP_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\)$"
)

PREDICATE_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_.]*|\w+\s*\([^)]*\))\s*"
    r"(=|!=|<>|>=|<=|>|<|\bIS\s+NOT\s+NULL\b|\bIS\s+NULL\b|\bLIKE\b|\bIN\b|\bBETWEEN\b)"
    r"\s*(.*)",
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Boolean operator / clause-type counters
# ---------------------------------------------------------------------------
AND_RE      = re.compile(r"\bAND\b",      re.IGNORECASE)
OR_RE       = re.compile(r"\bOR\b",       re.IGNORECASE)
IS_NULL_RE  = re.compile(r"\bIS\s+NULL\b",  re.IGNORECASE)
BETWEEN_RE  = re.compile(r"\bBETWEEN\b",  re.IGNORECASE)
LIKE_RE     = re.compile(r"\bLIKE\b",     re.IGNORECASE)
EXISTS_RE   = re.compile(r"\bEXISTS\s*\(", re.IGNORECASE)

# Bind parameter patterns
ORACLE_BIND_RE = re.compile(r":\w+")
PG_BIND_RE     = re.compile(r"\$\d+|%\(\w+\)s")


def _extract_where(sql: str) -> str:
    sql = strip_comments_and_strings(sql)
    match = WHERE_RE.search(sql)
    return match.group(1).strip() if match else ""


def _split_predicates(where_clause: str) -> list[str]:
    """Split on top-level AND / OR only (respects parenthesis depth)."""
    if not where_clause:
        return []
    parts: list[str] = []
    depth = 0
    current = ""
    tokens = re.split(r"(\(|\)|\bAND\b|\bOR\b)", where_clause, flags=re.IGNORECASE)
    for tok in tokens:
        if tok is None:
            continue
        if tok == "(":
            depth += 1
            current += tok
        elif tok == ")":
            depth -= 1
            current += tok
        elif re.fullmatch(r"\s*(AND|OR)\s*", tok, re.IGNORECASE) and depth == 0:
            if current.strip():
                parts.append(current.strip())
            current = ""
        else:
            current += tok
    if current.strip():
        parts.append(current.strip())
    return parts


def _normalize_value(value: str) -> str:
    value = value.strip().rstrip(";").strip()
    value = re.sub(r"\s+", " ", value)
    return value.lower()


def _parse_predicate(predicate: str) -> dict | None:
    pred = predicate.strip()
    m = PREDICATE_RE.match(pred)
    if not m:
        return None

    lhs_raw  = m.group(1).strip()
    operator = re.sub(r"\s+", " ", m.group(2).upper().strip())
    value    = _normalize_value(m.group(3) or "")

    fw = FUNC_WRAP_RE.match(lhs_raw)
    if fw:
        fn     = fw.group(1).upper()
        column = fw.group(2).split(".")[-1].lower()
    else:
        fn     = ""
        column = lhs_raw.split(".")[-1].lower()
        if "(" in column:
            return None

    loose  = f"{column}:{operator}"
    strict = f"{column}:{operator}:{value}"

    return {
        "column":   column,
        "operator": operator,
        "value":    value,
        "fn":       fn,
        "loose":    loose,
        "strict":   strict,
    }


def _operator_counts(clause: str) -> dict:
    """Count key boolean operators / clause types in a WHERE clause."""
    return {
        "AND":     len(AND_RE.findall(clause)),
        "OR":      len(OR_RE.findall(clause)),
        "IS NULL": len(IS_NULL_RE.findall(clause)),
        "BETWEEN": len(BETWEEN_RE.findall(clause)),
        "LIKE":    len(LIKE_RE.findall(clause)),
        "EXISTS":  len(EXISTS_RE.findall(clause)),
    }


def _has_bind_flag_pattern(clause: str) -> bool:
    """Return True when the WHERE clause contains a flag-parameter pattern like
    (:p_flag = 'true' AND col = val) OR (:p_flag = 'false' AND col IS NULL).
    """
    return bool(re.search(
        r"\(\s*:\w+\s*=\s*'[^']+'\s+AND\b",
        clause,
        re.IGNORECASE,
    ))


def validate(oracle_sql: str, postgres_sql: str) -> dict:
    issues: list[dict] = []

    source_where = _extract_where(oracle_sql)
    target_where = _extract_where(postgres_sql)

    # ------------------------------------------------------------------
    # 1. Whole-clause disappearance
    # ------------------------------------------------------------------
    source_predicates = _split_predicates(source_where)
    target_predicates = _split_predicates(target_where)

    if source_predicates and not target_predicates:
        issues.append({
            "type":    "MISSING_CONDITION",
            "message": (
                f"Source has {len(source_predicates)} WHERE predicate(s) "
                "but the target has none."
            ),
        })
        return {"status": "FAIL", "issues": issues}

    # ------------------------------------------------------------------
    # 2. Boolean operator inventory — significant drops are suspicious
    # ------------------------------------------------------------------
    src_ops = _operator_counts(source_where)
    tgt_ops = _operator_counts(target_where)

    SIGNIFICANT_DROP_THRESHOLD = 2   # tolerate minor changes from restructuring
    for op, src_count in src_ops.items():
        tgt_count = tgt_ops.get(op, 0)
        drop = src_count - tgt_count
        if src_count > 0 and drop >= SIGNIFICANT_DROP_THRESHOLD:
            issues.append({
                "type":    "SEMANTIC_UNCERTAINTY",
                "message": (
                    f"WHERE clause: source has {src_count} {op} "
                    f"occurrence(s) but target only has {tgt_count}. "
                    f"Verify {op} conditions were not accidentally dropped."
                ),
            })

    # ------------------------------------------------------------------
    # 3. Bind-parameter count integrity
    # ------------------------------------------------------------------
    src_bind_count = len(ORACLE_BIND_RE.findall(source_where))
    # Target may use :name (kept as-is) or $N / %(name)s style
    tgt_bind_oracle = len(ORACLE_BIND_RE.findall(target_where))
    tgt_bind_pg     = len(PG_BIND_RE.findall(target_where))
    tgt_bind_count  = tgt_bind_oracle + tgt_bind_pg

    if src_bind_count > 0 and tgt_bind_count < src_bind_count:
        issues.append({
            "type":    "SEMANTIC_UNCERTAINTY",
            "message": (
                f"Source WHERE clause has {src_bind_count} bind parameter(s) "
                f"but target only has {tgt_bind_count}. "
                "Confirm no conditions were accidentally dropped."
            ),
        })

    # ------------------------------------------------------------------
    # 4. Flag-parameter pattern detection
    # ------------------------------------------------------------------
    if _has_bind_flag_pattern(source_where) and not _has_bind_flag_pattern(target_where):
        issues.append({
            "type":    "SEMANTIC_UNCERTAINTY",
            "message": (
                "Source WHERE clause contains a flag-parameter conditional pattern "
                "(e.g. ':flag = ''true'' AND col = val) OR (:flag = ''false'' AND col IS NULL)) "
                "that was not detected in the target. Manual review recommended."
            ),
        })

    # ------------------------------------------------------------------
    # 5. Per-predicate fingerprint check (v1 logic, unchanged)
    # ------------------------------------------------------------------
    target_loose:  set[str] = set()
    target_strict: set[str] = set()
    for p in target_predicates:
        sig = _parse_predicate(p)
        if sig:
            target_loose.add(sig["loose"])
            target_strict.add(sig["strict"])

    for pred in source_predicates:
        sig = _parse_predicate(pred)

        if sig is None:
            issues.append({
                "type":    "SEMANTIC_UNCERTAINTY",
                "message": (
                    "Cannot reliably fingerprint source predicate for automated comparison "
                    f"(manual review recommended): {pred.strip()[:120]}"
                ),
            })
            continue

        if sig["loose"] not in target_loose:
            issues.append({
                "type":    "MISSING_CONDITION",
                "message": f"Source predicate appears missing from target: {pred.strip()[:120]}",
            })
        elif sig["strict"] not in target_strict:
            if "(" not in sig["value"]:
                issues.append({
                    "type":    "CONDITION_VALUE_CHANGED",
                    "message": (
                        f"Source predicate '{pred.strip()[:80]}' — same column/operator "
                        "found in target but the compared value differs."
                    ),
                })

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------
    fail_types = {"MISSING_CONDITION", "CONDITION_VALUE_CHANGED"}
    if any(i["type"] in fail_types for i in issues):
        status = "FAIL"
    else:
        status = "PASS"

    return {"status": status, "issues": issues}
