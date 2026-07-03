#!/usr/bin/env python3
"""Emits a Stop-hook block decision if any Orbit agent session is active."""
import json
import sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)

sessions = d.get("activeSessions") or []
if not sessions:
    sys.exit(0)

tasks = ", ".join(s.get("task", "?") for s in sessions)
print(
    json.dumps(
        {
            "decision": "block",
            "reason": (
                "You still hold active Orbit session(s) on: %s. "
                "If the work is done and verified, call complete_task with a summary. "
                "If you are stopping without finishing, call abandon_task with a reason. "
                "If you are intentionally pausing mid-task, post update_progress so the "
                "team knows the state, then stop." % tasks
            ),
        }
    )
)
