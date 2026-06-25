#!/usr/bin/env python3
"""Block reserved Python logging attribute names in logger extra dicts.

Using reserved names like 'message', 'name', 'msg', etc. as keys in the
extra={} dict passed to logger.info/error/warning/debug will cause:
    KeyError: "Attempt to overwrite 'X' in LogRecord"
"""
import json
import re
import sys

# Reserved LogRecord attributes that cannot be used as keys in extra={}
RESERVED_KEYS = {
    "name",
    "msg",
    "args",
    "created",
    "filename",
    "funcName",
    "levelname",
    "levelno",
    "lineno",
    "module",
    "msecs",
    "pathname",
    "process",
    "processName",
    "relativeCreated",
    "thread",
    "threadName",
    "exc_info",
    "exc_text",
    "stack_info",
    "message",
}


def main():
    try:
        data = json.load(sys.stdin)
    except json.JSONDecodeError:
        sys.exit(0)  # Can't parse, allow

    tool_input = data.get("tool_input", {})
    new_string = tool_input.get("new_string", "") or tool_input.get("content", "")
    if not new_string:
        sys.exit(0)

    # Only check Python files
    file_path = tool_input.get("file_path", "")
    if not file_path.endswith(".py"):
        sys.exit(0)

    # Look for logger calls with extra= containing reserved keys
    # Pattern: logger.(info|error|warning|debug|critical|exception)(
    #          followed by extra={ or extra = {
    #          containing "reserved_key":

    # Find all potential logger calls with extra dicts
    logger_pattern = r'logger\.(info|error|warning|debug|critical|exception)\s*\([^)]*extra\s*=\s*\{'

    for match in re.finditer(logger_pattern, new_string, re.DOTALL):
        # Get the content after extra={
        start = match.end() - 1  # Start at the {
        brace_count = 1
        pos = start + 1

        # Find the matching closing brace
        while pos < len(new_string) and brace_count > 0:
            if new_string[pos] == '{':
                brace_count += 1
            elif new_string[pos] == '}':
                brace_count -= 1
            pos += 1

        extra_content = new_string[start:pos]

        # Check for reserved keys in the extra dict
        for key in RESERVED_KEYS:
            # Match "key": or 'key': at the start of a dict entry
            if re.search(rf'[{{\s,]"{key}"\s*:', extra_content) or \
               re.search(rf"[{{\s,]'{key}'\s*:", extra_content):
                print(json.dumps({
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"Reserved logging key '{key}' used in logger extra dict. "
                            f"This will cause KeyError at runtime. "
                            f"Use a different key name like '{key}_value' or 'log_{key}'."
                        )
                    }
                }))
                sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
