#!/usr/bin/env python3
"""Block sqlalchemy query primitive imports in routers.

Query construction belongs in crud/. Routers call services, services call crud.
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

    if "/app/api/v1/routers/" not in file_path and "/app/web/routers/" not in file_path:
        sys.exit(0)

    if re.search(r"#\s*noqa:\s*router-sqlalchemy\s*\([^)]+\)", new_string):
        sys.exit(0)

    if SQLALCHEMY_QUERY_IMPORT.search(new_string):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Routers must not import sqlalchemy query primitives. "
                    "Query construction belongs in crud/. Routers call "
                    "services, services call crud."
                )
            }
        }))
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
