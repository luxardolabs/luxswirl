#!/usr/bin/env python3
"""Block raw SQL in app/services/ — services must delegate to crud/.

Mirrors `tests/test_architecture.py` raw-SQL checks so the rule fires at edit
time, not only when pytest runs.
"""
import json
import re
import sys

RAW_SQL_PATTERNS = [
    re.compile(r"\bsession\.execute\("),
    re.compile(r"\bdb\.execute\("),
    re.compile(r"(?<!\.)\bselect\("),
    re.compile(r"(?<!\.)\bupdate\(\s*\w+\s*\)"),
    re.compile(r"(?<!\.)\bdelete\(\s*\w+\s*\)"),
]


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    new_string = tool_input.get("new_string", "") or tool_input.get("content", "")

    # Only check files under app/services/
    if "/app/services/" not in file_path:
        sys.exit(0)

    # Bypass: # noqa: raw-sql (reason)
    if re.search(r"#\s*noqa:\s*raw-sql\s*\([^)]+\)", new_string):
        sys.exit(0)

    for pattern in RAW_SQL_PATTERNS:
        if pattern.search(new_string):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Raw SQL forbidden in services/ (matched `{pattern.pattern}`). "
                        "Services must delegate to crud/. Add a CRUD method that "
                        "wraps the query, then call it from the service. Pattern: "
                        "Service -> CRUD -> Model."
                    )
                }
            }))
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
