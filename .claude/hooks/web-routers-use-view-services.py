#!/usr/bin/env python3
"""Enforce web routers use ViewServices, not core services directly."""
import json
import re
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    new_string = tool_input.get("new_string", "") or tool_input.get("content", "")

    # Only check web router files
    if not re.search(r"web/routers/.*\.py$", file_path):
        sys.exit(0)

    # Check for bypass comment: # noqa: core-service (reason)
    # Bypass requires a reason in parentheses
    if re.search(r"#\s*noqa:\s*core-service\s*\([^)]+\)", new_string):
        sys.exit(0)

    # Web routers may import only the views layer of services, never core.
    # luxswirl imports are app-prefixed and split into subpackages:
    #   from app.services.core.user_core_service import ...    (BLOCK)
    #   from app.services.views.x_view_service import ...      (ALLOW)
    # Capture the first segment after "services." and block anything but "views".
    # The optional "app." prefix also matches a flat "services.x" style.
    service_imports = re.findall(
        r"from\s+(?:app\.)?services\.(\w+)",
        new_string
    )

    for segment in service_imports:
        if segment != "views":
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"Web routers must import app.services.views.*, not "
                        f"app.services.{segment}.*. "
                        "Pattern: WebRouter -> ViewService -> CoreService -> CRUD"
                    )
                }
            }))
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
