#!/usr/bin/env python3
"""Block fastapi/starlette imports in app/services/core/.

Core services must be HTTP-agnostic so they can be reused by web routers,
API routers, and scheduled jobs alike.
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

    if "/app/services/core/" not in file_path:
        sys.exit(0)

    if re.search(r"#\s*noqa:\s*core-fastapi\s*\([^)]+\)", new_string):
        sys.exit(0)

    if FASTAPI_IMPORT.search(new_string):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Core services (services/core/) must not import fastapi or "
                    "starlette. They are HTTP-agnostic by design — used by web "
                    "routers, API routers, and scheduled jobs alike. Raise "
                    "domain exceptions (LuxSwirlException subclasses) and let the "
                    "router or view service translate them to HTTP."
                )
            }
        }))
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
