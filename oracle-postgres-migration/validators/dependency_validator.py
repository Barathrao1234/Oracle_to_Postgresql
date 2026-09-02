"""Cross-checks the Agent's own `output/analysis-result.json` against the
migration so Python never silently approves a query the Agent's own STEP 3
analysis flagged as blocked or unreconciled.

This validator does NOT attempt to (re-)determine whether a custom function,
custom type, or placeholder is safe to migrate — that requires semantic
understanding only the Agent has. It only enforces that the Agent's own
stated readiness assessment is honored, so a `migration_readiness: BLOCKED`
verdict (or unresolved dependencies the Agent hasn't reconciled) can never be
overridden by simply having the rest of the SQL look syntactically fine.

Expected shape of output/analysis-result.json (see
.github/agents/oracle-postgres-migrator.agent.md, STEP 3):

    {
      "structural_elements": [...],
      "dependencies": [
        {"name": "FN_X", "kind": "custom_function", "status": "UNRESOLVED"}
      ],
      "application_placeholders": [...],
      "complexity": "HIGH | MEDIUM | LOW",
      "migration_readiness": "READY | READY_WITH_UNRESOLVED | BLOCKED"
    }

`migration_readiness` is optional for backward compatibility, but its
absence alongside UNRESOLVED dependencies is itself flagged.
"""

import json
from pathlib import Path

ANALYSIS_FILE = "output/analysis-result.json"


def validate(oracle_sql: str, postgres_sql: str) -> dict:
    path = Path(ANALYSIS_FILE)

    if not path.exists():
        # STEP 3 of the agent instructions requires this file for every
        # migration. Its absence is worth flagging but shouldn't hard-block
        # ad-hoc/manual validator runs (e.g. during pipeline development).
        return {
            "status": "PASS",
            "issues": [{
                "type": "MISSING_ANALYSIS",
                "message": (
                    f"{ANALYSIS_FILE} not found — STEP 3 analysis was skipped "
                    "or not written for this run."
                ),
            }],
        }

    try:
        analysis = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {
            "status": "PASS",
            "issues": [{
                "type": "MISSING_ANALYSIS",
                "message": f"{ANALYSIS_FILE} exists but could not be parsed as JSON.",
            }],
        }

    issues = []
    readiness = analysis.get("migration_readiness")
    unresolved = [
        d for d in analysis.get("dependencies", [])
        if isinstance(d, dict) and d.get("status") == "UNRESOLVED"
    ]

    if readiness == "BLOCKED":
        issues.append({
            "type": "UNRESOLVED_DEPENDENCY",
            "message": (
                "The Agent's own analysis-result.json sets "
                "migration_readiness=BLOCKED. Python will not approve this "
                "migration regardless of other validator results."
            ),
        })
    elif unresolved and readiness != "READY_WITH_UNRESOLVED":
        names = ", ".join(sorted({d.get("name", "?") for d in unresolved}))
        issues.append({
            "type": "UNRESOLVED_DEPENDENCY",
            "message": (
                f"{len(unresolved)} dependency/dependencies are UNRESOLVED "
                f"({names}) but migration_readiness is "
                f"'{readiness}', not READY_WITH_UNRESOLVED. The Agent should "
                "either resolve these, explicitly set migration_readiness to "
                "READY_WITH_UNRESOLVED (if partial migration with preserved "
                "calls/types is intentional), or set it to BLOCKED."
            ),
        })

    status = "FAIL" if any(i["type"] == "UNRESOLVED_DEPENDENCY" for i in issues) else "PASS"
    return {"status": status, "issues": issues}
