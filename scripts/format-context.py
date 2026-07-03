#!/usr/bin/env python3
"""Formats /api/agent/context JSON (stdin) into SessionStart context lines."""
import json
import sys

try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)

agent = d.get("agent") or {}
org = d.get("org") or {}
lines = [
    '[Orbit] Connected as agent "%s" in workspace "%s".'
    % (agent.get("name", "?"), org.get("name", "?"))
]
sessions = d.get("activeSessions") or []
if sessions:
    lines.append("ACTIVE SESSIONS (you claimed these — finish or abandon them):")
    for s in sessions:
        lines.append("  - %s: %s" % (s.get("task"), s.get("title")))
tasks = d.get("myOpenTasks") or []
if tasks:
    lines.append("My open Orbit tasks:")
    for t in tasks[:10]:
        lines.append(
            "  - %s [%s] %s"
            % (t.get("identifier"), t.get("status"), t.get("title"))
        )
print("\n".join(lines))
