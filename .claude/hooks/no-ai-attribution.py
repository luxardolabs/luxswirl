#!/usr/bin/env python3
"""Block git commits / PRs that carry Claude/AI attribution.

House rule (persistent): never add Co-Authored-By or "Generated with Claude Code"
attribution to commits or PRs. The harness prompt nudges for it; this hook makes
the rule mechanical so it can't slip through.

PreToolUse(Bash): if the command is a `git commit` or `gh pr create|edit` whose
text contains AI attribution, deny it and tell Claude to remove it. Legitimate
human Co-Authored-By trailers (not Claude/Anthropic) are allowed.
"""
import json
import re
import sys


# Only police commit / PR-authoring commands — everything else passes through.
GIT_PR_RE = re.compile(r"\bgit\s+commit\b|\bgh\s+pr\s+(create|edit)\b", re.IGNORECASE)

# AI/Claude attribution signatures (case-insensitive). Deliberately NOT a bare
# "co-authored-by" — a real human co-author is fine; only Claude/Anthropic ones
# and the "Generated with Claude Code" footer are blocked.
ATTRIBUTION_RE = re.compile(
    r"co-authored-by:[^\n]*(claude|anthropic)"
    r"|🤖"
    r"|generated with \[?claude code"
    r"|noreply@anthropic\.com"
    r"|claude\.com/claude-code"
    r"|claude\.ai/code",
    re.IGNORECASE,
)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)  # can't parse → allow

    command = (data.get("tool_input", {}) or {}).get("command", "") or ""
    if not command or not GIT_PR_RE.search(command):
        sys.exit(0)

    if ATTRIBUTION_RE.search(command):
        print(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Blocked: this commit/PR carries Claude/AI attribution "
                    "(Co-Authored-By: Claude, '🤖 Generated with Claude Code', "
                    "noreply@anthropic.com, etc.). House rule: NEVER add AI "
                    "attribution to commits or PRs. Remove the Co-Authored-By "
                    "trailer and any 'Generated with Claude Code' line, then retry."
                ),
            }
        }))
        sys.exit(0)

    sys.exit(0)


if __name__ == "__main__":
    main()
