---
description: Summarize what my Orbit agent sessions did recently
---

Use the `orbit` MCP server to produce a standup-style summary:

1. Call `list_tasks` with `mine=true` (all categories).
2. For each task that changed recently, call `get_task` and look at the
   progress/summary comments and session history.
3. Write a short standup update with three sections:
   - **Done** — tasks completed, one line each with the outcome
   - **In progress** — active work and its latest progress note
   - **Blocked / abandoned** — anything released with its reason
4. Keep it under ~15 lines, plain language, no tool-speak.

$ARGUMENTS
