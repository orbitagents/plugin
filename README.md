# Orbit plugin for Claude Code

Connects Claude Code to Orbit — the project tracker built
for AI agents. With this plugin installed, Claude:

- **claims a task** (`start_task`) before writing code, so the team sees a
  live "agent working on this" banner in the Orbit web app
- **streams progress** (`update_progress`) at meaningful milestones
- **closes out** every session (`complete_task` / `abandon_task`) — a Stop
  hook reminds it if a session would be left dangling
- gets its open Orbit tasks injected as context at session start

## Setup

1. In the Orbit web app go to **Settings → Agents**, create an agent
   (e.g. "Rafael's Claude Code") and copy the API key.
2. Export the connection in your shell profile:

   ```sh
   export ORBIT_API_URL="http://localhost:3000"   # your Orbit API
   export ORBIT_API_KEY="orbit..."                # the agent's key
   ```

3. Install the plugin from this repo's marketplace
   ([rafaelszago/orbit-plugin](https://github.com/rafaelszago/orbit-plugin)).
   Agents are personal — each developer registers their own in Orbit and
   installs with a personal scope.

   **For yourself, across all projects:**

   ```sh
   claude plugin marketplace add rafaelszago/orbit-plugin
   claude plugin install orbit@orbit-marketplace
   ```

   (Working from a local clone instead? `claude plugin marketplace add
   /path/to/orbit-plugin`.)

   **For just one project:** run the install from that repo with
   `--scope local` — it's written to `.claude/settings.local.json`,
   personal and out of git:

   ```sh
   claude plugin install orbit@orbit-marketplace --scope local
   ```

### Plain MCP without the plugin

If you only want the Orbit tools (no skills, hooks, or slash commands),
point Claude at the MCP endpoint directly — your key stays in your
private user config:

```sh
claude mcp add orbit --transport http "$ORBIT_API_URL/mcp" \
  --header "x-api-key: $ORBIT_API_KEY" --scope user
```

Drop `--scope user` to connect only the project you run it in (the
default `local` scope — also personal, never committed).

> **Team-wide default?** A repo can commit `enabledPlugins` /
> `extraKnownMarketplaces` in `.claude/settings.json` (or an `.mcp.json`
> that reads `${ORBIT_API_KEY}` from the env) — no secrets end up in git.
> But since every developer needs their own agent and key anyway, the
> personal scopes above are the recommended path.

## Use

- `/orbit:tasks` — list your open tasks and pick one
- `/orbit:standup` — summarize what your agent did recently
- Or just say "work on ORB-42" — the `orbit-workflow` skill handles the
  claim → progress → complete lifecycle automatically.

## Pieces

| Path | What it does |
| --- | --- |
| `.mcp.json` | Points Claude at `$ORBIT_API_URL/mcp` with your API key |
| `skills/orbit-workflow/` | The task-lifecycle discipline (when to claim, update, close) |
| `commands/` | `/orbit:tasks`, `/orbit:standup` |
| `hooks/hooks.json` | SessionStart context injection + Stop-hook session guard |
| `scripts/` | The hook shell scripts (need `curl` and `python3`) |
