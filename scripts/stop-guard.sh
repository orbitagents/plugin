#!/bin/sh
# Blocks stopping once if an Orbit agent session is still active, reminding
# Claude to complete/abandon it. Passes through on the second attempt
# (stop_hook_active) to avoid loops.
[ -z "$ORBIT_API_KEY" ] && exit 0
API="${ORBIT_API_URL:-http://localhost:3000}"
DIR="$(dirname "$0")"

input=$(cat)
case "$input" in
  *'"stop_hook_active":true'* | *'"stop_hook_active": true'*) exit 0 ;;
esac

curl -sf -m 3 -H "x-api-key: $ORBIT_API_KEY" "$API/api/agent/context" 2>/dev/null |
  python3 "$DIR/stop-check.py" 2>/dev/null
exit 0
