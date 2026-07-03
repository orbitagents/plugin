#!/bin/sh
# Injects the agent's Orbit context (open tasks, active sessions) at session
# start. Silent no-op when Orbit isn't configured or unreachable.
[ -z "$ORBIT_API_KEY" ] && exit 0
API="${ORBIT_API_URL:-http://localhost:3000}"
DIR="$(dirname "$0")"

curl -sf -m 3 -H "x-api-key: $ORBIT_API_KEY" "$API/api/agent/context" 2>/dev/null |
  python3 "$DIR/format-context.py" 2>/dev/null
exit 0
