# Orbit runner

A small daemon each developer runs on their own laptop that turns queued
Orbit tasks into headless Claude Code sessions — on your own Claude
subscription, no Anthropic API key involved.

Flag a task **agent-ready** in Orbit and, within one poll interval, the
runner picks it up via `get_next_task`, creates a fresh git worktree off
the default branch, and spawns `claude -p "Work on ORB-42 …"` in it. The
session claims the task (`start_task`), streams progress, opens a PR, and
completes the task — all visible live on Orbit's Agents page.

## Requirements

- `python3`, `git`, and the `claude` CLI on your PATH
- the Orbit plugin (or plain Orbit MCP config) installed at **user scope**,
  so spawned sessions get the Orbit tools in any directory
  (see the repo root README)
- `ORBIT_API_URL` / `ORBIT_API_KEY` exported (same env the plugin uses)

## Setup

```sh
orbit-runner init          # interactive: api key, project→repo map, default model/effort
orbit-runner start         # daemonize; add --foreground to keep today's inline behavior
orbit-runner status        # daemon up/down, slots in use, rate-limit pause state
orbit-runner stop
```

`init` writes `~/.config/orbit-runner/config.json`. To set it up by hand
instead, copy `orbit-runner.example.json` there and fill it in. Every
subcommand takes `--config <path>` to point at a different file.

If you have not installed the CLI onto your PATH yet (see
[Install](#install-launchd)), invoke it as `python3 orbit-runner.py <subcommand>`.

### Subcommands

| Command | What it does |
| --- | --- |
| `start` | Daemonize (pidfile in `~/.orbit-runner/`). `--foreground`/`-F` runs inline; `--follow`/`-f` streams live logs (implies foreground); `--project`/`-p`, `--model`/`-m`, `--effort`/`-e` as below. |
| `stop` | SIGTERM the daemon via its pidfile. `--project` targets one instance. |
| `status` | Daemon up/down, model/effort, slots in use with task ids, and rate-limit pause state. |
| `logs [task-id] [-f]` | Tail per-session logs. With no task id and `-f`, follows **all** active sessions multiplexed. |
| `init` | Interactive config bootstrap. |
| `install` / `uninstall` | launchd agent so the runner starts at login (see below). |

Config fields:

| Field | Default | Meaning |
| --- | --- | --- |
| `poll_interval_seconds` | `30` | How often to ask Orbit for the next task |
| `max_slots` | `2` | Max concurrent Claude Code sessions |
| `rate_limit_pause_minutes` | `30` | Fallback pause when the usage-limit message has no parseable reset time |
| `task_retry_cooldown_minutes` | `10` | How long to wait before re-dispatching a task whose session exited without completing |
| `work_dir` | `~/.orbit-runner` | Where worktrees, per-session logs, pidfiles, and state live |
| `claude_bin` / `claude_args` | `claude` / `[]` | CLI binary and extra args for spawned sessions |
| `model` / `effort` | `null` / `null` | Default `--model` / `--effort` for spawned sessions (see below) |
| `projects` | — | Map of Orbit project key → `{ "repo": path, "repos": {…}, "model": …, "effort": … }`. Only mapped projects are polled. |

`api_url` / `api_key` may also be set in the config; the `ORBIT_API_URL` /
`ORBIT_API_KEY` environment variables win when both are present.

## Running per project

By default `start` polls every mapped project from one process. Pass
`--project <KEY>` to scope an instance to a single Orbit project:

```sh
orbit-runner start --project ORB --model opus --effort high
orbit-runner start --project WEB --model fable          # a second, independent instance
orbit-runner status                                     # shows both
orbit-runner stop --project WEB
```

Each scoped instance gets its own pidfile (`~/.orbit-runner/runner-<key>.pid`),
state file, and log, so several can run side by side. `stop` / `status` /
`logs` accept `--project`; with a single instance running they default to it.

## Model and effort

Spawned sessions run `claude -p …`; the runner injects `--model` and
`--effort` when set. Resolution order, highest first:

1. `--model` / `--effort` on `orbit-runner start`
2. per-project `projects.<KEY>.model` / `.effort`
3. top-level `model` / `effort`
4. unset → the claude CLI's own default

`status` shows what each instance is running with. (Setting these here is
preferred over stuffing `--model` into `claude_args`.)

## Live logs

Each session already streams to `~/.orbit-runner/logs/<task>-<ts>.log`.

- `orbit-runner logs ORB-42 -f` follows one session.
- `orbit-runner logs -f` (no task id) multiplexes **every** active session to
  the console, each line prefixed with its task id, and picks up new sessions
  as they spawn.
- `orbit-runner start --foreground --follow` runs the daemon inline and streams
  the same multiplexed output as it dispatches.

<a id="install-launchd"></a>
## Install (start at login)

```sh
orbit-runner install                 # symlink + launchd agent for all projects
orbit-runner install --project ORB   # a per-project agent (namespaced label)
orbit-runner uninstall
```

`install` symlinks the entrypoint into `~/.local/bin/orbit-runner` and writes a
launchd agent at `~/Library/LaunchAgents/dev.orbitagents.runner[.<key>].plist`
(`RunAtLoad` + `KeepAlive`, running `start --foreground`) so the runner comes up
at login without a terminal. Because `KeepAlive` restarts the job, `stop` only
pauses an installed runner until launchd relaunches it — use `uninstall` to
remove it for good. The agent bakes in your current `PATH` and any
`ORBIT_API_URL` / `ORBIT_API_KEY` from the environment; keep the API key in the
config file if you would rather not have it in the plist.

## Dispatching a task into a different repo

`projects` maps one project key to one default `repo`, but a task can target a
different checkout with a `repo:` line anywhere in its description:

```
repo: plugin
```

The hint resolves, in order, to: a name under that project's `repos` map, another
top-level project key, or a filesystem path to a git repo. Anything unrecognized
falls back to the project's default repo (and is logged). This lets a task in the
`ORB` project be worked in the plugin checkout instead of the app checkout.

## How a slot lives and dies

1. `get_next_task` returns the highest-priority agent-ready, unclaimed,
   unblocked, conflict-free task (or nothing). The runner never steals
   claims — `start_task` inside the spawned session is the race tiebreaker.
2. The runner runs `git fetch` and creates a worktree under
   `<work_dir>/worktrees/` on a new branch `orbit/orb-42` off
   `origin/HEAD`, so Orbit's GitHub integration links the branch and PR to
   the task.
3. Session output goes to `<work_dir>/logs/orb-42-<ts>.log`.
4. When the process exits, the runner checks the task: if it isn't Done and
   has no linked PR, it posts "runner: session exited without completing"
   to the task and frees the slot. Orbit's stale-session sweep releases the
   claim, so the task becomes dispatchable again — after a local
   `task_retry_cooldown_minutes` backoff, so a doomed task can't respawn
   sessions in a tight loop.
5. If the runner itself is killed, spawned sessions are terminated with it;
   the sweep frees their claims the same way.

Worktrees are left in place after a session ends so partial work is never
destroyed. Clean them up occasionally with:

```sh
rm -rf ~/.orbit-runner/worktrees/<name> && git -C <repo> worktree prune
```

## Usage limits

When a session hits your Claude subscription's usage limit, the runner
pauses **all** dispatch until the reset time reported by Claude Code (or
`rate_limit_pause_minutes` when none is parseable), logs the pause, and
posts a note to the affected task so the team sees why dispatch stopped.
Running sessions are left alone; only new spawns are held back.

## Permissions

Spawned sessions run headless `claude -p` **without**
`--dangerously-skip-permissions`. Two things make that workable:

**1. The repo must be trusted.** Claude Code ignores a repo's
`.claude/settings.json` allowlist until you have accepted the trust
dialog for that repo — run `claude` interactively in each target repo
once. Worktrees inherit the main repo's trust, so the runner's throwaway
worktrees are covered by that one-time step. An untrusted repo doesn't
fail loudly: the session just gets every tool denied and exits having
done nothing.

**2. Commit an allowlist.** Add a `.claude/settings.json` to each target
repo so the agent can do its routine work non-interactively, e.g.:

```json
{
  "permissions": {
    "allow": [
      "Edit",
      "Bash(git add:*)",
      "Bash(git commit:*)",
      "Bash(git push:*)",
      "Bash(gh pr create:*)",
      "Bash(bun test:*)",
      "mcp__orbit"
    ]
  }
}
```

Anything not allowlisted is simply denied in headless mode — the session
works with what it has and reports what it couldn't do. Widen the list
deliberately per repo rather than reaching for the skip flag: the
allowlist is the security boundary that makes unattended sessions safe.
Never allowlist broad shells (`Bash(*)`), package publishing, or deploy
commands for unattended runs.
