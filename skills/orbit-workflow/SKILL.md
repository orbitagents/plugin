---
name: orbit-workflow
description: Task lifecycle discipline for Orbit. Use whenever the user asks to work on an Orbit task (by id like ORB-42 or by description), asks "what should I work on", or when starting/finishing any coding work tracked in Orbit. Ensures start_task is called before coding, progress is posted at milestones, and sessions are always closed with complete_task or abandon_task.
---

# Orbit workflow

Orbit is the team's task tracker. You are an agent with your own Orbit
identity; the humans watch your work live in the Orbit web app. The MCP
server `orbit` exposes the tools you need.

## The golden rule

**Never write code for a tracked task without an active session, and never
leave a session dangling.** The lifecycle is:

1. `start_task` — before any code. This claims the task (one agent at a
   time), moves it to In Progress, and shows a live banner to the team.
   Pass `branch` when you know the git branch you'll use. When creating a
   new branch, name it after the task — lowercase identifier plus a short
   slug, e.g. `orb-42-fix-numbering` — so Orbit's GitHub integration links
   the branch and its PR to the task automatically (and moves the task to
   Done when the PR merges). Mention the identifier in the PR title or body
   too.
2. `update_progress` — at meaningful milestones only (plan formed, core
   change done, tests passing, blocked on something). One or two sentences.
   Not every file edit. Pass `files` with the repo-relative files or
   directories you touched since the last update — Orbit uses them to warn
   the team (and you) when another agent is working in the same area. If the
   response contains a conflict `warning`, surface it to the user before
   editing the shared code further.
3. `complete_task` — when the work is done and verified. Include a short
   `summary` of what you did and how you verified it.

   With both `update_progress` and `complete_task`, report usage when you
   know it: `input_tokens`, `output_tokens`, and `cost_usd` (cumulative
   totals for this session, not deltas). This feeds the team's agent cost
   and performance analytics — a rough estimate beats omitting it.
4. `abandon_task` — if you stop without finishing (blocked, out of scope,
   user changed direction). Include the `reason` so the next agent or human
   has context.

## Picking up work

- The user names a task ("work on ORB-42", "fix the numbering bug in
  Orbit"): use `get_task` to read the full description and comments first,
  then `start_task`.
- The user asks what to do: call `list_tasks` with `mine=true`, or
  `status_category="todo"` sorted by priority, and propose the top
  candidates.
- If `start_task` fails because another agent holds the claim, do NOT
  steal or retry. Tell the user who holds it and offer to pick a different
  task.

## While working

- Treat the task description and its comments as requirements. If they
  conflict with the user's instructions, the user wins — but note the
  discrepancy in a progress update.
- If you discover follow-up work that is out of scope, `create_task` for it
  (search `list_tasks` first to avoid duplicates) instead of expanding the
  current task. Pass `parent` when it's really a piece of the current task
  (subtask), and use `add_dependency` when tasks must land in a specific
  order.
- If `start_task` warns that the task is blocked by unfinished tasks, tell
  the user before proceeding — the blocker may make your work moot.

## Finishing

- Before `complete_task`, make sure the change is actually verified (tests
  pass, app runs). The summary should say what changed and how it was
  verified.
- If the user ends the conversation while a session is active, call
  `abandon_task` with reason "session ended" rather than leaving the claim
  held.
