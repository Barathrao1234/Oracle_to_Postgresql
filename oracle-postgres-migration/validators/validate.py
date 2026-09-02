#!/usr/bin/env python3
"""The Agent's single entry point for validation.

    python validators/validate.py \\
        --oracle input/oracle-query.txt \\
        --postgres output/postgresql-query.sql

    # optional live PostgreSQL check:
    python validators/validate.py --oracle ... --postgres ... \\
        --postgres-dsn "postgresql://user:pass@host:5432/db"

    # start a fresh migration run (clears the attempt counter/prior reports):
    python validators/validate.py --reset

This script's ONLY job is: run validators -> produce a validation result.
It does not migrate SQL and it does not decide whether to keep correcting —
that loop is controlled by the Copilot Agent (see
.github/agents/oracle-postgres-migrator.agent.md). Python owns two small
pieces of state, purely so the Agent can't accidentally loop forever or
silently self-approve:
  1. an attempt counter (.runtime/attempt-state.json)
  2. an optional agent-judgment file (output/agent-judgment.json) the Agent
     may write to explain why a SEMANTIC_UNCERTAINTY issue is, on inspection,
     not actually a problem. Python only *records and reports* that
     judgment — it never invents one, and it never lets a judgment file
     paper over a hard-fail or dependency issue.

Exit codes:
    0  -> APPROVED            (all required checks passed)
    1  -> CORRECTION_NEEDED   (hard-fail issue(s); correction attempts remain)
    2  -> REVIEW_REQUIRED     (retry limit reached, or environment error)
    3  -> VALIDATION_ERROR    (pipeline/IO error; do NOT modify SQL, fix infra)
    4  -> AGENT_REVIEW_NEEDED (only SEMANTIC_UNCERTAINTY issue(s) remain; the
                               Agent must judge whether they're really
                               unresolved — see "Agent-judgment loop" below)
"""

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validators import (  # noqa: E402
    completeness_validator,
    condition_validator,
    construct_validator,
    dependency_validator,
    join_validator,
    output_validator,
    postgres_validator,
    structure_validator,
    syntax_validator,
)

# Terminology (kept consistent throughout reports and agent instructions):
#   query_version  = 1-based count of total generated versions (V1=initial, V2=correction-1 …)
#   correction_number = 0 for the initial migration, 1+ for each subsequent correction
#
# MAX_CORRECTION_ATTEMPTS = 3 means the Agent may correct up to 3 times after the
# initial migration, so at most 4 versions are generated (V1 … V4).
MAX_CORRECTION_ATTEMPTS = 3

# Static (no-DB-required) validators run in cheap-to-expensive order.
STATIC_VALIDATORS = [
    ("output_sanity",     output_validator),
    ("syntax",            syntax_validator),
    ("oracle_constructs", construct_validator),
    ("structure",         structure_validator),
    ("completeness",      completeness_validator),
    ("conditions",        condition_validator),
    ("joins",             join_validator),
    ("dependencies",      dependency_validator),
]

MAX_ISSUES_IN_REPORT = 20

# Issue types that mean "a human (or the Agent, acting as reviewer) needs to
# make a judgment call" rather than "the SQL is definitely wrong". These
# never auto-fail a validator on their own — see the agent-judgment loop.
#
# SEMANTIC_UNCERTAINTY — a validator encountered ambiguous Oracle behaviour
#     that cannot be reliably resolved automatically. The SQL may well be
#     correct; only something with semantic understanding (the Agent, or a
#     human) can tell.
ESCALATE_TYPES = {"SEMANTIC_UNCERTAINTY"}

# Issue types that indicate an infrastructure problem — do NOT treat as SQL
# bugs, and do NOT let an agent-judgment file downgrade these.
ENV_ERROR_TYPES = {"VALIDATION_ENVIRONMENT_ERROR"}

# Issue types that are hard FAILs (drive correction retries). These can
# never be waived by an agent-judgment file.
HARD_FAIL_TYPES = {
    "STRUCTURE_LOST",
    "ORACLE_CONSTRUCT_REMAINS",
    "MISSING_PLACEHOLDER",
    "SQL_ERROR",
    "INCOMPLETE_SELECT_LIST",
    "INCOMPLETE_GROUP_BY",
    "INCOMPLETE_ORDER_BY",
    "ORDER_BY_DIRECTION_CHANGED",
    "MISSING_ROW_LIMIT",
    "ROW_LIMIT_CHANGED",
    "MISSING_CONDITION",
    "CONDITION_VALUE_CHANGED",
    "MISSING_JOIN",
    "JOIN_TYPE_MISMATCH",
    "JOIN_CONDITION_MISMATCH",
    "SYNTAX",
    "EMPTY_OUTPUT",
    "MARKDOWN_FENCE_IN_OUTPUT",
    "CONVERSATIONAL_TEXT_IN_OUTPUT",
    "UNEXPECTED_OUTPUT_START",
    "POSTGRESQL_ERROR",
    # The Agent's own analysis-result.json says this migration is BLOCKED or
    # has unreconciled UNRESOLVED dependencies — Python will not silently
    # approve a query the Agent itself flagged. See dependency_validator.py.
    "UNRESOLVED_DEPENDENCY",
}

# Purely informational — never affects status.
INFORMATIONAL_TYPES = {"MISSING_ANALYSIS"}

STATE_FILE             = ".runtime/attempt-state.json"
VALIDATION_REPORT_FILE = "output/validation-report.json"
FINAL_STATUS_FILE      = "output/final-status.json"
JUDGMENT_FILE           = "output/agent-judgment.json"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _issue_id(issue: dict) -> str:
    """Stable short id for an issue, derived from its type+message, so the
    Agent can reference a *specific* issue in agent-judgment.json without
    Python needing to hand out incrementing counters across runs."""
    basis = f"{issue.get('type', '')}|{issue.get('message', '')}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:10]


def run_validators(oracle_sql: str, postgres_sql: str, postgres_dsn: str | None) -> dict:
    """Run every validator; return a per-validator status map and a capped issue list."""
    per_validator_status: dict[str, str] = {}
    all_issues: list[dict] = []

    for name, validator in STATIC_VALIDATORS:
        result = validator.validate(oracle_sql, postgres_sql)
        per_validator_status[name] = result["status"]
        for issue in result.get("issues", []):
            issue = dict(issue)
            issue.setdefault("source", name)
            all_issues.append(issue)

    db_result = postgres_validator.validate(oracle_sql, postgres_sql, dsn=postgres_dsn)
    per_validator_status["postgresql_live"] = db_result["status"]
    for issue in db_result.get("issues", []):
        issue = dict(issue)
        issue.setdefault("source", "postgresql_live")
        all_issues.append(issue)

    return {
        "validators": per_validator_status,
        "issues":     all_issues,
        "db_note":    db_result.get("note"),
    }


def _read_judgment(judgment_path: Path) -> dict:
    """Return {issue_id: justification} for issues the Agent has explicitly
    reviewed and judged not to be a real problem. Only entries with a
    non-empty justification count — an id with no reasoning is ignored."""
    if not judgment_path.exists():
        return {}
    try:
        data = json.loads(judgment_path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    acknowledged = {}
    for entry in data.get("acknowledged", []):
        issue_id = entry.get("issue_id")
        justification = (entry.get("justification") or "").strip()
        if issue_id and justification:
            acknowledged[issue_id] = justification
    return acknowledged


def overall_status(per_validator_status: dict, issues: list, acknowledged: dict) -> str:
    """Derive the aggregate status.

    Priority order (highest to lowest):
      REVIEW_REQUIRED    — VALIDATION_ENVIRONMENT_ERROR found. The SQL may be
                           correct; human inspection of the environment is needed.
      FAIL               — At least one hard-fail issue type found (including
                           UNRESOLVED_DEPENDENCY); automated correction should
                           be attempted.
      AGENT_REVIEW_NEEDED — Only SEMANTIC_UNCERTAINTY issue(s) remain, and at
                           least one is NOT yet acknowledged with a reasoned
                           justification. The Agent must look at the original
                           Oracle query and decide.
      PASS               — No hard fails, no env errors, and every
                           SEMANTIC_UNCERTAINTY issue (if any) has been
                           acknowledged with a justification.

    SKIPPED is never promoted to PASS, but it does not block a PASS either —
    it means the check simply did not run.
    """
    issue_types = {i.get("type") for i in issues}

    if issue_types & ENV_ERROR_TYPES:
        return "REVIEW_REQUIRED"

    if any(status == "FAIL" for status in per_validator_status.values()):
        return "FAIL"

    unacknowledged_semantic = [
        i for i in issues
        if i.get("type") in ESCALATE_TYPES and _issue_id(i) not in acknowledged
    ]
    if unacknowledged_semantic:
        return "AGENT_REVIEW_NEEDED"

    return "PASS"


def _read_state(state_path: Path) -> dict:
    """Return persisted state; defaults to query_version=1, correction_number=0."""
    if not state_path.exists():
        return {"query_version": 1, "correction_number": 0}
    try:
        return json.loads(state_path.read_text())
    except (json.JSONDecodeError, ValueError):
        return {"query_version": 1, "correction_number": 0}


def _write_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2))


def _write_json(path: str, data: dict) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2))


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_reset(state_file: str, judgment_file: str) -> int:
    """Clear attempt state and all prior output reports for a fresh migration run."""
    targets = [
        state_file,
        VALIDATION_REPORT_FILE,
        FINAL_STATUS_FILE,
        judgment_file,
    ]
    for f in targets:
        p = Path(f)
        if p.exists():
            p.unlink()
    runtime_dir = Path(state_file).parent
    if runtime_dir.exists() and not any(runtime_dir.iterdir()):
        runtime_dir.rmdir()
    print("State reset: attempt counter, prior reports, and agent-judgment cleared.")
    return 0


def cmd_validate(
    oracle_path_str: str,
    postgres_path_str: str,
    postgres_dsn: str | None,
    state_file: str,
    judgment_file: str,
) -> int:
    oracle_path   = Path(oracle_path_str)
    postgres_path = Path(postgres_path_str)
    state_path    = Path(state_file)
    judgment_path = Path(judgment_file)

    # Exit 3 = VALIDATION_ERROR: infrastructure/IO problems — do NOT modify SQL.
    if not oracle_path.exists():
        print(f"VALIDATION_ERROR: Oracle source file not found: {oracle_path}", file=sys.stderr)
        return 3
    if not postgres_path.exists():
        print(f"VALIDATION_ERROR: PostgreSQL target file not found: {postgres_path}", file=sys.stderr)
        return 3

    oracle_sql   = oracle_path.read_text()
    postgres_sql = postgres_path.read_text()

    state = _read_state(state_path)
    query_version     = state.get("query_version", 1)
    correction_number = state.get("correction_number", 0)

    acknowledged = _read_judgment(judgment_path)

    # Run all validators.
    try:
        result = run_validators(oracle_sql, postgres_sql, postgres_dsn)
    except Exception as exc:  # noqa: BLE001
        print(f"VALIDATION_ERROR: validator raised an unexpected exception: {exc}", file=sys.stderr)
        return 3

    status = overall_status(result["validators"], result["issues"], acknowledged)

    # Classify every issue for the report so the Agent can act on types directly.
    classified_issues = []
    for issue in result["issues"]:
        classified = dict(issue)
        itype = classified.get("type", "")
        issue_id = _issue_id(issue)
        classified["id"] = issue_id
        if itype in ENV_ERROR_TYPES:
            classified["severity"] = "REVIEW_REQUIRED"
        elif itype in HARD_FAIL_TYPES:
            classified["severity"] = "FAIL"
        elif itype in ESCALATE_TYPES:
            if issue_id in acknowledged:
                classified["severity"] = "ACKNOWLEDGED"
                classified["justification"] = acknowledged[issue_id]
            else:
                classified["severity"] = "AGENT_REVIEW_NEEDED"
        elif itype in INFORMATIONAL_TYPES:
            classified["severity"] = "INFORMATIONAL"
        else:
            classified["severity"] = "INFORMATIONAL"
        classified_issues.append(classified)

    # Build the compact validation report (what the Agent reads).
    report: dict = {
        "status":             status,
        "query_version":      query_version,
        "correction_number":  correction_number,
        "validators":         result["validators"],
        "issues":             classified_issues[:MAX_ISSUES_IN_REPORT],
    }
    if len(classified_issues) > MAX_ISSUES_IN_REPORT:
        report["truncated_issue_count"] = len(classified_issues) - MAX_ISSUES_IN_REPORT
    if result.get("db_note"):
        report["note"] = result["db_note"]

    _write_json(VALIDATION_REPORT_FILE, report)

    # ---- PASS → APPROVED ------------------------------------------------
    if status == "PASS":
        final = {
            "status":            "APPROVED",
            "validation":        "PASS",
            "query_version":     query_version,
            "correction_number": correction_number,
        }
        acknowledged_in_play = [
            i for i in classified_issues if i.get("severity") == "ACKNOWLEDGED"
        ]
        if acknowledged_in_play:
            final["acknowledged_uncertainties"] = [
                {"id": i["id"], "message": i["message"], "justification": i["justification"]}
                for i in acknowledged_in_play
            ]
        _write_json(FINAL_STATUS_FILE, final)
        print(json.dumps(final, indent=2))
        return 0

    # ---- Environment error: immediate escalation -------------------------
    if status == "REVIEW_REQUIRED":
        final = {
            "status":            "REVIEW_REQUIRED",
            "validation":        status,
            "query_version":     query_version,
            "correction_number": correction_number,
            "reason": (
                "A VALIDATION_ENVIRONMENT_ERROR was encountered. The validation "
                "infrastructure appears to be broken. Do NOT modify the SQL — "
                "fix the environment and re-run."
            ),
        }
        _write_json(FINAL_STATUS_FILE, final)
        print(json.dumps(final, indent=2))
        return 2

    # ---- Only SEMANTIC_UNCERTAINTY remains, unacknowledged --------------
    # This is NOT a final state: the run is still in progress. The Agent
    # must read validation-report.json, re-read the original Oracle query,
    # and either:
    #   (a) judge each issue safe and write output/agent-judgment.json with
    #       a reasoned justification per issue id, then re-run this command
    #       (a fully-acknowledged report becomes PASS/APPROVED), or
    #   (b) judge an issue genuinely unresolved and write REVIEW_REQUIRED to
    #       output/final-status.json itself, explaining why.
    # Python deliberately does NOT write final-status.json here.
    if status == "AGENT_REVIEW_NEEDED":
        unacknowledged = [
            i for i in classified_issues if i.get("severity") == "AGENT_REVIEW_NEEDED"
        ]
        summary = {
            "status":            "AGENT_REVIEW_NEEDED",
            "validation":        status,
            "query_version":     query_version,
            "correction_number": correction_number,
            "unacknowledged_issue_count": len(unacknowledged),
            "report_file":       VALIDATION_REPORT_FILE,
            "instructions": (
                "Re-read the original Oracle query for each SEMANTIC_UNCERTAINTY "
                f"issue. If genuinely safe, write {JUDGMENT_FILE} with a "
                "justification per issue id and re-run validate.py. If not "
                "safe, write REVIEW_REQUIRED to output/final-status.json yourself."
            ),
        }
        print(json.dumps(summary, indent=2))
        return 4

    # ---- FAIL: check whether correction attempts remain -----------------
    if correction_number >= MAX_CORRECTION_ATTEMPTS:
        final = {
            "status":            "REVIEW_REQUIRED",
            "validation":        "FAIL",
            "query_version":     query_version,
            "correction_number": correction_number,
            "reason": (
                f"Validation failed after {MAX_CORRECTION_ATTEMPTS} correction attempt(s) "
                f"({MAX_CORRECTION_ATTEMPTS + 1} total versions generated)."
            ),
        }
        _write_json(FINAL_STATUS_FILE, final)
        print(json.dumps(final, indent=2))
        return 2

    # Retries remain: increment both counters for the next call.
    new_state = {
        "query_version":     query_version + 1,
        "correction_number": correction_number + 1,
    }
    _write_state(state_path, new_state)

    # Remove stale final-status so it's unambiguous that the run is still in progress.
    if Path(FINAL_STATUS_FILE).exists():
        Path(FINAL_STATUS_FILE).unlink()

    summary = {
        "status":                 "CORRECTION_NEEDED",
        "validation":             "FAIL",
        "query_version":          query_version,
        "correction_number":      correction_number,
        "next_query_version":     query_version + 1,
        "next_correction_number": correction_number + 1,
        "corrections_remaining":  MAX_CORRECTION_ATTEMPTS - correction_number,
        "issue_count":            len(classified_issues),
        "report_file":            VALIDATION_REPORT_FILE,
    }
    print(json.dumps(summary, indent=2))
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Oracle → PostgreSQL migration validator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exit codes:
  0  APPROVED            All required checks passed.
  1  CORRECTION_NEEDED   Hard-fail issue(s); correction attempts remain.
  2  REVIEW_REQUIRED     Retry limit reached, or VALIDATION_ENVIRONMENT_ERROR detected.
  3  VALIDATION_ERROR    Infrastructure/IO error; do NOT modify the SQL.
  4  AGENT_REVIEW_NEEDED Only SEMANTIC_UNCERTAINTY issue(s) remain; the Agent
                        must judge them (see output/agent-judgment.json).
""",
    )
    parser.add_argument("--oracle",       help="Path to the Oracle source .txt/.sql")
    parser.add_argument("--postgres",     help="Path to the migrated PostgreSQL .sql")
    parser.add_argument(
        "--postgres-dsn", default=None,
        help="Optional live PostgreSQL DSN for the strongest validation tier",
    )
    parser.add_argument(
        "--state-file", default=STATE_FILE,
        help="Where the attempt counter is stored (default: .runtime/attempt-state.json)",
    )
    parser.add_argument(
        "--judgment-file", default=JUDGMENT_FILE,
        help="Where Agent judgments on SEMANTIC_UNCERTAINTY issues are read from "
             f"(default: {JUDGMENT_FILE})",
    )
    parser.add_argument(
        "--reset", action="store_true",
        help="Reset the attempt counter and prior reports for a fresh migration run",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.reset:
        return cmd_reset(args.state_file, args.judgment_file)

    if not args.oracle or not args.postgres:
        print(
            "VALIDATION_ERROR: --oracle and --postgres are required (or use --reset)",
            file=sys.stderr,
        )
        return 3

    return cmd_validate(
        args.oracle, args.postgres, args.postgres_dsn, args.state_file, args.judgment_file
    )


if __name__ == "__main__":
    sys.exit(main())
