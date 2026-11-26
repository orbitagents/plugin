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

### Quick setup (recommended)

1. In the Orbit web app go to **Settings → Agents**, create an agent
   (e.g. "Rafael's Claude Code") and copy the API key.
2. Run the `orbit-runner` setup wizard:

   ```sh
   bunx orbit-runner@latest init
   ```

   It verifies your key, maps your Orbit projects to local repos, offers a
   starter headless allowlist for each repo, then wires up Claude Code for
   you: it adds this marketplace, installs the plugin, and registers the
   Orbit MCP server with your key (user scope, or one per repo if you serve
   several orgs). It ends with a `doctor` check. Pass `--no-plugin` to skip
   the Claude Code wiring.

3. For the SessionStart and Stop hooks, also export the connection in your
   shell profile. `init` keeps the key in the runner config, which the hooks
   don't read, and without `ORBIT_API_URL` they fall back to
   `http://localhost:3000`:

   ```sh
   export ORBIT_API_URL="https://api.orbitagents.dev"   # your Orbit API
   export ORBIT_API_KEY="orbit..."                      # the agent's key
   ```

### Manual setup

1. Create an agent and copy its key as above, then export the connection in
   your shell profile:

   ```sh
   export ORBIT_API_URL="https://api.orbitagents.dev"   # your Orbit API
   export ORBIT_API_KEY="orbit..."                      # the agent's key
   ```

   Self-hosting or running Orbit locally? Point `ORBIT_API_URL` at your API
   instead (local dev defaults to `http://localhost:3100`).

2. Install the plugin from this repo's marketplace
   ([orbitagents/plugin](https://github.com/orbitagents/plugin)).
   Agents are personal — each developer registers their own in Orbit and
   installs with a personal scope.

   **For yourself, across all projects:**

   ```sh
   claude plugin marketplace add orbitagents/plugin
   claude plugin install orbit@orbit-marketplace
   ```

   (Working from a local clone instead? `claude plugin marketplace add
   /path/to/plugin`.)

   **For just one project:** run the install from that repo with
   `--scope local` — it's written to `.claude/settings.local.json`,
   personal and out of git:

   ```sh
   claude plugin install orbit@orbit-marketplace --scope local
   ```

3. Register the Orbit MCP server. The plugin ships the skills, hooks, and
   slash commands; the tools themselves come from the MCP server, which you
   wire up once with your key baked into your private user config:

   ```sh
   claude mcp add orbit --transport http "$ORBIT_API_URL/mcp" \
     --header "x-api-key: $ORBIT_API_KEY" --scope user
   ```

   Drop `--scope user` to connect only the project you run it in (the
   default `local` scope — also personal, never committed).

### Plain MCP without the plugin

Want the Orbit tools with no skills, hooks, or slash commands? Skip the
plugin install above and run only the `claude mcp add` from step 3 of the
manual setup.

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

> **Autonomous dispatch** — after `bunx orbit-runner@latest init`, run
> `bunx orbit-runner@latest start` to have agent-ready tasks picked up and
> finished as headless Claude Code sessions, with no one at the keyboard.

## Pieces

| Path | What it does |
| --- | --- |
| `skills/orbit-workflow/` | The task-lifecycle discipline (when to claim, update, close) |
| `commands/` | `/orbit:tasks`, `/orbit:standup` |
| `hooks/hooks.json` | SessionStart context injection + Stop-hook session guard |
| `scripts/` | The hook shell scripts (need `curl` and `python3`) |
