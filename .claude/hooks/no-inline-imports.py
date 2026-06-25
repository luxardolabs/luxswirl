#!/usr/bin/env python3
"""Block inline imports inside functions."""
import json
import re
import sys


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)  # Can't parse, allow

    tool_input = data.get("tool_input", {})
    new_string = tool_input.get("new_string", "") or tool_input.get("content", "")
    if not new_string:
        sys.exit(0)

    # Check for bypass comment: # noqa: inline-import (reason)
    # Bypass requires a reason in parentheses
    if re.search(r"#\s*noqa:\s*inline-import\s*\([^)]+\)", new_string):
        sys.exit(0)

    # Check for indented import/from (inline imports)
    # Pattern: line starts with 4+ spaces (function body level), then 'from' or 'import'
    # Using 4+ spaces avoids false positives from multiline parameter formatting
    # Must match 'from' or 'import' as the first non-space word on the line
    inline_import = re.search(r"^[ ]{4,}(from\s+\w|import\s+\w)", new_string, re.MULTILINE)

    if inline_import:
        # Allow if it's in TYPE_CHECKING block
        if "TYPE_CHECKING" not in new_string:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "No inline imports inside functions. "
                        "Move imports to top of file, or use TYPE_CHECKING for circular deps."
                    )
                }
            }))
            sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
