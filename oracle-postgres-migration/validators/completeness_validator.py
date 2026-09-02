"""Catches semantic elements that are easy to accidentally drop or alter
during migration but that structure_validator.py (which only checks clause
*presence*) wouldn't notice:

  - SELECT expressions silently dropped (target selects fewer columns/exprs)
  - GROUP BY columns silently dropped
  - ORDER BY columns dropped, or sort direction flipped (ASC <-> DESC)
  - Row-limiting/pagination silently dropped (ROWNUM/FETCH FIRST -> LIMIT)
    or the row count changed

This stays at the "did an important semantic element go missing or change"
level — it is not a semantic equivalence prover for arbitrary expressions.
"""

import re

from validators.sql_utils import extract_clause, split_top_level, strip_comments_and_strings

SELECT_END = [r"\bFROM\b"]
GROUP_BY_END = [r"\bHAVING\b", r"\bORDER\s+BY\b", r"\bFETCH\s+FIRST\b", r"\bLIMIT\b"]
ORDER_BY_END = [r"\bFETCH\s+FIRST\b", r"\bLIMIT\b", r"\bOFFSET\b"]

ROW_LIMIT_RE = re.compile(
    r"\bROWNUM\s*<=?\s*(\d+)|\bFETCH\s+FIRST\s+(\d+)\s+ROWS?\s+ONLY\b|\bLIMIT\s+(\d+)\b",
    re.IGNORECASE,
)


def _select_list(sql: str) -> list[str]:
    clause = extract_clause(sql, r"\bSELECT\b", SELECT_END)
    return split_top_level(clause, ",")


def _clean_column_name(text: str) -> str:
    """Take a raw clause fragment and extract just the leading identifier
    (optionally dotted), discarding anything after the first character
    that isn't part of an identifier (whitespace, semicolons, stray
    punctuation, etc.)."""
    text = text.strip()
    m = re.match(r"[A-Za-z_][A-Za-z0-9_.]*", text)
    if not m:
        return ""
    return m.group(0).split(".")[-1].lower()


def _group_by_columns(sql: str) -> list[str]:
    clause = extract_clause(sql, r"\bGROUP\s+BY\b", GROUP_BY_END)
    return [_clean_column_name(c) for c in split_top_level(clause, ",") if _clean_column_name(c)]


def _order_by_items(sql: str) -> list[tuple[str, str]]:
    clause = extract_clause(sql, r"\bORDER\s+BY\b", ORDER_BY_END)
    items = []
    for item in split_top_level(clause, ","):
        item = item.strip()
        if not item:
            continue
        direction = "DESC" if re.search(r"\bDESC\b", item, re.IGNORECASE) else "ASC"
        column = re.sub(r"\b(ASC|DESC|NULLS\s+FIRST|NULLS\s+LAST)\b", "", item, flags=re.IGNORECASE)
        column = _clean_column_name(column)
        if column:
            items.append((column, direction))
    return items


def _row_limit(sql: str) -> int | None:
    m = ROW_LIMIT_RE.search(sql)
    if not m:
        return None
    for group in m.groups():
        if group is not None:
            return int(group)
    return None


def validate(oracle_sql: str, postgres_sql: str) -> dict:
    issues = []
    oracle_clean = strip_comments_and_strings(oracle_sql)
    postgres_clean = strip_comments_and_strings(postgres_sql)

    # 1. SELECT expression count shouldn't shrink.
    source_select = _select_list(oracle_clean)
    target_select = _select_list(postgres_clean)
    if source_select and len(target_select) < len(source_select):
        issues.append({
            "type": "INCOMPLETE_SELECT_LIST",
            "message": f"Source SELECTs {len(source_select)} expression(s) but target only "
                       f"has {len(target_select)}",
        })

    # 2. GROUP BY columns shouldn't be dropped.
    source_group = _group_by_columns(oracle_clean)
    target_group = _group_by_columns(postgres_clean)
    missing_group_cols = [c for c in source_group if c not in target_group]
    if missing_group_cols:
        issues.append({
            "type": "INCOMPLETE_GROUP_BY",
            "message": f"GROUP BY column(s) missing in target: {', '.join(missing_group_cols)}",
        })

    # 3. ORDER BY columns shouldn't be dropped, and direction shouldn't flip.
    source_order = _order_by_items(oracle_clean)
    target_order = dict(_order_by_items(postgres_clean))
    for column, direction in source_order:
        if column not in target_order:
            issues.append({
                "type": "INCOMPLETE_ORDER_BY",
                "message": f"ORDER BY column missing in target: {column}",
            })
        elif target_order[column] != direction:
            issues.append({
                "type": "ORDER_BY_DIRECTION_CHANGED",
                "message": f"ORDER BY '{column}' is {direction} in source but "
                           f"{target_order[column]} in target",
            })

    # 4. Pagination: presence and row count.
    source_limit = _row_limit(oracle_clean)
    target_limit = _row_limit(postgres_clean)
    if source_limit is not None and target_limit is None:
        issues.append({
            "type": "MISSING_ROW_LIMIT",
            "message": f"Source limits results to {source_limit} row(s) but target has no "
                       f"LIMIT/FETCH FIRST",
        })
    elif source_limit is not None and target_limit is not None and source_limit != target_limit:
        issues.append({
            "type": "ROW_LIMIT_CHANGED",
            "message": f"Source limits to {source_limit} row(s) but target limits to {target_limit}",
        })

    status = "FAIL" if issues else "PASS"
    return {"status": status, "issues": issues}
