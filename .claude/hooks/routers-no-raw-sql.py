#!/usr/bin/env python3
"""Block raw SQL in routers — routers call services, services call crud.

Mirrors `tests/test_architecture.py` raw-SQL checks.
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

    # Match api/v1/routers/ and web/routers/
    if "/app/api/v1/routers/" not in file_path and "/app/web/routers/" not in file_path:
        sys.exit(0)

    if re.search(r"#\s*noqa:\s*raw-sql\s*\([^)]+\)", new_string):
        sys.exit(0)

    for pattern in RAW_SQL_PATTERNS:
        if pattern.search(new_string):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Raw SQL forbidden in routers (matched `{pattern.pattern}`). "
                        "Routers call services. Services call crud. Move the "
                        "query into a CRUD method and have the service expose it."
                    )
                }
            }))
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
