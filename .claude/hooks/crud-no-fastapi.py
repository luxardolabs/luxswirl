#!/usr/bin/env python3
"""Block fastapi/starlette imports in app/crud/.

CRUD is pure data access — no HTTP concepts.
"""
import json
import re
import sys

FASTAPI_IMPORT = re.compile(
    r"^\s*(from\s+fastapi|from\s+starlette|import\s+fastapi|import\s+starlette)",
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

    if "/app/crud/" not in file_path:
        sys.exit(0)

    if re.search(r"#\s*noqa:\s*crud-fastapi\s*\([^)]+\)", new_string):
        sys.exit(0)

    if FASTAPI_IMPORT.search(new_string):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "CRUD modules (app/crud/) must not import fastapi or "
                    "starlette. CRUD is pure data access — no HTTP concepts. "
                    "Return models / dataclasses / dicts. Let services raise "
                    "domain exceptions; routers translate to HTTP."
                )
            }
        }))
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
