#!/usr/bin/env python3
"""Block sqlalchemy query primitive imports in view services.

View services orchestrate template assembly — query primitives belong in crud/.
"""
import json
import re
import sys

SQLALCHEMY_QUERY_IMPORT = re.compile(
    r"^\s*from\s+sqlalchemy\s+import\s+.*\b(select|update|delete|insert|text|and_|or_)\b",
    re.MULTILINE,
)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    new_string = tool_input.get("new_string", "") or tool_input.get("content", "")

    if not file_path.endswith("_view_service.py"):
        sys.exit(0)

    if re.search(r"#\s*noqa:\s*view-sqlalchemy\s*\([^)]+\)", new_string):
        sys.exit(0)

    if SQLALCHEMY_QUERY_IMPORT.search(new_string):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "View services must not import sqlalchemy query primitives "
                    "(select / update / delete / insert / text / and_ / or_). "
                    "Move the query into a CRUD method and call it through "
                    "the core service. Pattern: ViewService -> CoreService -> CRUD."
                )
            }
        }))
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
