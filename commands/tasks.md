---
description: List my open Orbit tasks and pick one to work on
---

Use the `orbit` MCP server:

1. Call `list_tasks` with `mine=true`. If that returns nothing, call
   `list_tasks` with `status_category="todo"`.
2. Show the tasks as a compact table: identifier, title, status, priority,
   whether an agent is already working on it.
3. Recommend which task to pick next (highest priority, not claimed) and
   ask the user to confirm.
4. When the user picks one, follow the orbit-workflow skill: `get_task`
   first, then `start_task` before touching code.

$ARGUMENTS
