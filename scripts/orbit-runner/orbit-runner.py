#!/usr/bin/env python3
"""Orbit runner: turns agent-ready Orbit tasks into headless Claude Code sessions.

Polls Orbit's `get_next_task` MCP tool and, for each dispatchable task, creates
a fresh git worktree off the default branch and spawns `claude -p` in it on the
developer's own subscription (no API key). Zero dependencies beyond python3,
git, and the claude CLI.

Usage:
    orbit-runner start [--project ORB] [--model opus] [--effort high] [--foreground] [--follow]
    orbit-runner status [--project ORB]
    orbit-runner stop [--project ORB]
    orbit-runner logs [ORB-42] [-f] [--project ORB]
    orbit-runner init
    orbit-runner install [--project ORB]   /   orbit-runner uninstall [--project ORB]

Config defaults to ~/.config/orbit-runner/config.json — see
orbit-runner.example.json next to this script. Run `orbit-runner init` to
create one interactively.
"""

import argparse
import glob
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

DEFAULT_CONFIG_PATH = "~/.config/orbit-runner/config.json"

DEFAULTS = {
    "poll_interval_seconds": 30,
    "max_slots": 2,
    "rate_limit_pause_minutes": 30,
    "task_retry_cooldown_minutes": 10,
    "work_dir": "~/.orbit-runner",
    "claude_bin": "claude",
    "claude_args": [],
    "model": None,
    "effort": None,
    "projects": {},
}

# Claude Code prints one of these when the subscription usage limit is hit.
# The first form carries the reset time as a unix epoch.
EPOCH_LIMIT_RE = re.compile(r"usage limit reached\|(\d{10,13})")
TEXT_LIMIT_RE = re.compile(
    r"(?i)(usage limit reached|reached your usage limit|hit your usage limit"
    r"|limit reached ∙ resets|out of usage)"
)
RESETS_AT_RE = re.compile(r"(?i)resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)")

# A task can override its target repo with a `repo: <name-or-path>` line in its
# description, resolved against the project's named repos / other project keys /
# a filesystem path (see resolve_repo).
REPO_HINT_RE = re.compile(r"(?im)^\s*repo:\s*(\S+)\s*$")


def log(msg):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


class RunnerError(Exception):
    pass


class Orbit:
    """Minimal client for Orbit's stateless MCP endpoint (bare tools/call)."""

    def __init__(self, api_url, api_key):
        self.endpoint = api_url.rstrip("/") + "/mcp"
        self.api_key = api_key
        self._id = 0

    def call(self, tool, arguments=None):
        self._id += 1
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": self._id,
                "method": "tools/call",
                "params": {"name": tool, "arguments": arguments or {}},
            }
        ).encode()
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "x-api-key": self.api_key,
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as err:
            raise RunnerError(f"orbit api unreachable: {err}") from err
        msg = self._parse(raw)
        if "error" in msg:
            raise RunnerError(f"{tool}: {msg['error'].get('message', msg['error'])}")
        content = msg.get("result", {}).get("content", [])
        text = next((c.get("text") for c in content if c.get("type") == "text"), "")
        try:
            payload = json.loads(text)
        except (json.JSONDecodeError, TypeError):
            payload = {"text": text}
        if msg.get("result", {}).get("isError"):
            raise RunnerError(f"{tool}: {payload.get('error', text)}")
        return payload

    @staticmethod
    def _parse(raw):
        """The endpoint answers plain JSON or a single-message SSE stream."""
        raw = raw.strip()
        if raw.startswith("{"):
            return json.loads(raw)
        last = None
        for line in raw.splitlines():
            if line.startswith("data:"):
                last = line[len("data:") :].strip()
        if last is None:
            raise RunnerError(f"unrecognized MCP response: {raw[:200]}")
        return json.loads(last)


class Slot:
    def __init__(self, task, proc, log_path, worktree, branch):
        self.task = task  # identifier, e.g. ORB-42
        self.proc = proc
        self.log_path = log_path
        self.offset = 0  # how far we have scanned the log for limit markers
        self.follow_offset = 0  # independent cursor for live-log streaming
        self.worktree = worktree
        self.branch = branch
        self.started = time.time()

    def read_new_output(self):
        return self._read("offset")

    def read_follow(self):
        return self._read("follow_offset")

    def _read(self, attr):
        try:
            with open(self.log_path, "r", errors="replace") as f:
                f.seek(getattr(self, attr))
                chunk = f.read()
                setattr(self, attr, f.tell())
            return chunk
        except OSError:
            return ""

    def as_state(self):
        return {
            "task": self.task,
            "pid": self.proc.pid,
            "log_path": self.log_path,
            "branch": self.branch,
            "worktree": self.worktree,
            "started": self.started,
        }


class Runner:
    def __init__(self, config):
        self.config = config
        self.orbit = Orbit(config["api_url"], config["api_key"])
        self.slots = []
        self.paused_until = 0.0
        self.cooldowns = {}  # identifier -> don't redispatch before this time
        self.rr = 0  # round-robin cursor over project keys
        self.work_dir = os.path.abspath(os.path.expanduser(config["work_dir"]))
        os.makedirs(os.path.join(self.work_dir, "logs"), exist_ok=True)
        os.makedirs(os.path.join(self.work_dir, "worktrees"), exist_ok=True)
        self.stopping = False
        self.started_at = time.time()
        self.scope = config.get("_project_scope")  # None = poll all mapped projects
        self.follow = bool(config.get("_follow"))
        self.poll_keys = [self.scope] if self.scope else list(config["projects"])
        self.pid_file = pidfile_path(self.work_dir, self.scope)
        self.state_file = state_path(self.work_dir, self.scope)

    # ---- polling ---------------------------------------------------------

    def run(self):
        self.write_pidfile()
        model, effort = self.config.get("_cli_model") or self.config.get("model"), (
            self.config.get("_cli_effort") or self.config.get("effort")
        )
        log(
            f"orbit-runner up: scope={self.scope or 'all'}, "
            f"{len(self.poll_keys)} project(s), {self.config['max_slots']} slot(s), "
            f"polling every {self.config['poll_interval_seconds']}s, "
            f"model={model or 'default'}, effort={effort or 'default'}"
        )
        try:
            self.write_state()
            while not self.stopping:
                try:
                    self.tick()
                except (RunnerError, OSError, subprocess.SubprocessError) as err:
                    log(f"tick failed (will retry): {err}")
                self._sleep(self.config["poll_interval_seconds"])
            self.shutdown()
        finally:
            self.cleanup_files()

    def _sleep(self, seconds):
        # Wake early to reap children promptly (and to stream logs in follow mode).
        deadline = time.time() + seconds
        while not self.stopping and time.time() < deadline:
            time.sleep(min(1, max(0, deadline - time.time())))
            if self.follow:
                self.pump_follow(self.slots)
            if any(s.proc.poll() is not None for s in self.slots):
                return

    def tick(self):
        self.scan_running_sessions()
        self.reap_finished_sessions()
        if time.time() < self.paused_until:
            self.write_state()
            return
        while len(self.slots) < self.config["max_slots"] and not self.stopping:
            task = self.next_task()
            if not task or not self.spawn(task):
                break
        self.write_state()

    # ---- dispatch --------------------------------------------------------

    def next_task(self):
        keys = self.poll_keys
        if not keys:
            return None
        in_flight = {s.task for s in self.slots}
        for i in range(len(keys)):
            key = keys[(self.rr + i) % len(keys)]
            payload = self.orbit.call("get_next_task", {"project_key": key})
            task = payload.get("task")
            if not task:
                continue
            # get_next_task keeps returning a task until its session claims
            # it; never double-dispatch something we just spawned, and back
            # off tasks whose last session died without completing.
            if task["identifier"] in in_flight:
                continue
            if time.time() < self.cooldowns.get(task["identifier"], 0):
                continue
            self.rr = (self.rr + i + 1) % len(keys)
            return task
        return None

    def spawn(self, task):
        ident = task["identifier"]
        project_key = ident.rsplit("-", 1)[0]
        if project_key not in self.config["projects"]:
            log(f"{ident}: no repo mapping for project {project_key}, skipping")
            return False
        description = self.fetch_description(ident)
        repo, hint = resolve_repo(self.config, project_key, description)
        if hint and hint.startswith("!"):
            log(f"{ident}: unknown repo hint {hint[1:]!r}; using project default")
        elif hint:
            log(f"{ident}: repo hint {hint!r} -> {repo}")
        try:
            worktree, branch = self.make_worktree(repo, ident)
        except subprocess.CalledProcessError as err:
            stderr = (err.stderr or b"").decode("utf-8", "replace").strip()
            log(f"{ident}: worktree setup failed, skipping this poll: {stderr}")
            return False
        log_path = os.path.join(
            self.work_dir, "logs", f"{ident.lower()}-{int(time.time())}.log"
        )
        prompt = (
            f"Work on {ident} using the orbit workflow: claim it with start_task "
            f"(branch {branch}), implement it, open a PR, and complete the task "
            f"with the PR link."
        )
        cmd = [self.config["claude_bin"], "-p", prompt]
        model, effort = resolve_model_effort(self.config, project_key)
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["--effort", effort]
        cmd += self.config["claude_args"]
        logfile = open(log_path, "w")
        proc = subprocess.Popen(
            cmd,
            cwd=worktree,
            stdin=subprocess.DEVNULL,
            stdout=logfile,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        logfile.close()
        self.slots.append(Slot(ident, proc, log_path, worktree, branch))
        extras = " ".join(
            filter(None, [f"model={model}" if model else "", f"effort={effort}" if effort else ""])
        )
        log(
            f"{ident}: spawned claude (pid {proc.pid}) in {worktree} [{branch}]"
            + (f" ({extras})" if extras else "")
        )
        return True

    def fetch_description(self, ident):
        try:
            full = self.orbit.call("get_task", {"task": ident})
        except RunnerError as err:
            log(f"{ident}: could not fetch task detail for repo hint: {err}")
            return ""
        return full.get("description", "") or ""

    def make_worktree(self, repo, ident):
        def git(*args):
            return subprocess.run(
                ["git", "-C", repo, *args], check=True, capture_output=True
            )

        git("fetch", "origin", "--quiet")
        head = subprocess.run(
            ["git", "-C", repo, "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            capture_output=True,
            text=True,
        )
        base = head.stdout.strip() or "origin/main"
        branch = f"orbit/{ident.lower()}"
        path = os.path.join(self.work_dir, "worktrees", ident.lower())
        n = 1
        while (
            os.path.exists(path)
            or subprocess.run(
                ["git", "-C", repo, "rev-parse", "--verify", "--quiet", branch],
                capture_output=True,
            ).returncode
            == 0
        ):
            n += 1
            branch = f"orbit/{ident.lower()}-{n}"
            path = os.path.join(self.work_dir, "worktrees", f"{ident.lower()}-{n}")
        git("worktree", "add", "-b", branch, path, base)
        return path, branch

    # ---- session lifecycle -----------------------------------------------

    def scan_running_sessions(self):
        for slot in self.slots:
            self.check_for_rate_limit(slot, slot.read_new_output())

    def reap_finished_sessions(self):
        for slot in [s for s in self.slots if s.proc.poll() is not None]:
            if self.follow:
                self.pump_follow([slot])
            self.slots.remove(slot)
            rc = slot.proc.returncode
            self.check_for_rate_limit(slot, slot.read_new_output())
            log(f"{slot.task}: session exited with code {rc} after "
                f"{int(time.time() - slot.started)}s (log: {slot.log_path})")
            try:
                self.close_out(slot, rc)
            except RunnerError as err:
                log(f"{slot.task}: could not check/annotate task state: {err}")

    def close_out(self, slot, rc):
        task = self.orbit.call("get_task", {"task": slot.task})
        done = task.get("statusCategory") == "done"
        pr_opened = bool(task.get("github"))
        if done or pr_opened:
            log(f"{slot.task}: completed ({'done' if done else 'PR opened'})")
            return
        cooldown = self.config["task_retry_cooldown_minutes"] * 60
        self.cooldowns[slot.task] = time.time() + cooldown
        log(f"{slot.task}: cooling down for {cooldown // 60}m before re-dispatch")
        note = (
            f"runner: session exited without completing (exit code {rc}); "
            f"releasing the slot. The stale-session sweep will free the claim."
        )
        self.orbit.call("update_progress", {"task": slot.task, "message": note})
        log(f"{slot.task}: posted exit-without-completing note")

    # ---- live logs ---------------------------------------------------------

    def pump_follow(self, slots):
        for slot in slots:
            chunk = slot.read_follow()
            if not chunk:
                continue
            for line in chunk.splitlines():
                print(f"[{slot.task}] {line}", flush=True)

    # ---- rate limiting -----------------------------------------------------

    def check_for_rate_limit(self, slot, output):
        if not output or time.time() < self.paused_until:
            return
        reset = self.parse_reset_time(output)
        if reset is None:
            return
        self.paused_until = reset
        until = datetime.fromtimestamp(reset).strftime("%Y-%m-%d %H:%M")
        log(f"usage limit reported by {slot.task}; pausing all dispatch until {until}")
        try:
            self.orbit.call(
                "update_progress",
                {
                    "task": slot.task,
                    "message": (
                        f"runner: Claude Code usage limit hit; pausing dispatch "
                        f"of new sessions until {until}."
                    ),
                },
            )
        except RunnerError as err:
            log(f"could not post pause note to Orbit: {err}")

    def parse_reset_time(self, output):
        """Return a unix timestamp to pause until, or None if no limit hit."""
        m = EPOCH_LIMIT_RE.search(output)
        if m:
            epoch = int(m.group(1))
            if epoch > 1e12:  # milliseconds
                epoch //= 1000
            return max(epoch, time.time() + 60)
        if not TEXT_LIMIT_RE.search(output):
            return None
        m = RESETS_AT_RE.search(output)
        if m:
            hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
            hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
            reset = datetime.now().replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
            if reset <= datetime.now():
                reset += timedelta(days=1)
            return reset.timestamp()
        return time.time() + self.config["rate_limit_pause_minutes"] * 60

    # ---- state / pidfile ---------------------------------------------------

    def write_pidfile(self):
        with open(self.pid_file, "w") as f:
            f.write(str(os.getpid()))

    def write_state(self):
        state = {
            "pid": os.getpid(),
            "project": self.scope,
            "started": self.started_at,
            "poll_interval_seconds": self.config["poll_interval_seconds"],
            "max_slots": self.config["max_slots"],
            "model": self.config.get("_cli_model") or self.config.get("model"),
            "effort": self.config.get("_cli_effort") or self.config.get("effort"),
            "paused_until": self.paused_until,
            "slots": [s.as_state() for s in self.slots],
        }
        tmp = self.state_file + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(state, f)
            os.replace(tmp, self.state_file)
        except OSError as err:
            log(f"could not write state file: {err}")

    def cleanup_files(self):
        for path in (self.pid_file, self.state_file):
            try:
                os.remove(path)
            except OSError:
                pass

    # ---- shutdown ----------------------------------------------------------

    def shutdown(self):
        for slot in self.slots:
            if slot.proc.poll() is None:
                log(f"{slot.task}: terminating session (pid {slot.proc.pid})")
                try:
                    os.killpg(slot.proc.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.time() + 10
        for slot in self.slots:
            try:
                slot.proc.wait(timeout=max(0.1, deadline - time.time()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(slot.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        log("orbit-runner stopped; Orbit's stale-session sweep frees any held claims")


# ---- config resolution ----------------------------------------------------


def is_git_repo(path):
    return os.path.isdir(os.path.join(path, ".git")) or os.path.isfile(
        os.path.join(path, ".git")
    )


def resolve_model_effort(config, project_key):
    """CLI flag > per-project config > global config > none."""
    proj = config["projects"].get(project_key, {})
    model = config.get("_cli_model") or proj.get("model") or config.get("model")
    effort = config.get("_cli_effort") or proj.get("effort") or config.get("effort")
    return model, effort


def parse_repo_hint(description):
    if not description:
        return None
    m = REPO_HINT_RE.search(description)
    return m.group(1) if m else None


def resolve_repo(config, project_key, description):
    """Return (repo_path, hint). hint is None when no override was requested,
    the matched hint string when honored, or "!<hint>" when unrecognized."""
    proj = config["projects"][project_key]
    default_repo = proj["repo"]
    hint = parse_repo_hint(description)
    if not hint:
        return default_repo, None
    named = proj.get("repos") or {}
    if hint in named:
        return named[hint], hint
    if hint in config["projects"]:
        return config["projects"][hint]["repo"], hint
    path = os.path.abspath(os.path.expanduser(hint))
    if is_git_repo(path):
        return path, hint
    return default_repo, f"!{hint}"


def read_config(path):
    """Merge the config file with DEFAULTS and env overrides, without the strict
    validation `start` needs. A missing file yields defaults (used by
    status/stop/logs, which only need work_dir)."""
    path = os.path.expanduser(path)
    user = {}
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        user = {}
    except json.JSONDecodeError as err:
        sys.exit(f"orbit-runner: invalid JSON in {path}: {err}")
    config = {**DEFAULTS, **user}
    config["api_url"] = os.environ.get(
        "ORBIT_API_URL", config.get("api_url", "http://localhost:3000")
    )
    config["api_key"] = os.environ.get("ORBIT_API_KEY", config.get("api_key", ""))
    config["work_dir"] = os.path.abspath(os.path.expanduser(config["work_dir"]))
    config["_config_path"] = path
    return config


def validate_for_start(config):
    """The strict checks a dispatching daemon needs; exits on any problem."""
    if not config["api_key"]:
        sys.exit(
            "orbit-runner: no API key. Set ORBIT_API_KEY (or \"api_key\" in the "
            "config) to an agent key from Orbit's Settings → Agents, or run "
            "`orbit-runner init`."
        )
    if not config["projects"]:
        sys.exit(
            'orbit-runner: config needs at least one entry under "projects". '
            "Run `orbit-runner init`."
        )
    for key, proj in config["projects"].items():
        repo = os.path.abspath(os.path.expanduser(proj.get("repo", "")))
        if not is_git_repo(repo):
            sys.exit(f"orbit-runner: projects.{key}.repo is not a git repo: {repo!r}")
        proj["repo"] = repo
        resolved = {}
        for name, alt in (proj.get("repos") or {}).items():
            altp = os.path.abspath(os.path.expanduser(alt))
            if not is_git_repo(altp):
                sys.exit(
                    f"orbit-runner: projects.{key}.repos.{name} is not a git repo: "
                    f"{altp!r}"
                )
            resolved[name] = altp
        proj["repos"] = resolved
    scope = config.get("_project_scope")
    if scope and scope not in config["projects"]:
        sys.exit(
            f"orbit-runner: --project {scope} is not in the config's projects "
            f"({', '.join(config['projects']) or 'none'})."
        )
    claude_bin = shutil.which(os.path.expanduser(config["claude_bin"]))
    if not claude_bin:
        sys.exit(f"orbit-runner: claude CLI not found ({config['claude_bin']!r})")
    config["claude_bin"] = os.path.abspath(claude_bin)
    return config


# ---- instance path helpers -------------------------------------------------


def _suffix(project):
    return f"-{project.lower()}" if project else ""


def pidfile_path(work_dir, project):
    return os.path.join(work_dir, f"runner{_suffix(project)}.pid")


def state_path(work_dir, project):
    return os.path.join(work_dir, f"runner{_suffix(project)}.state.json")


def runner_log_path(work_dir, project):
    return os.path.join(work_dir, f"runner{_suffix(project)}.log")


def read_pid(pid_file):
    try:
        with open(pid_file) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_live_pid(pid_file):
    pid = read_pid(pid_file)
    return pid if pid_alive(pid) else None


def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def discover_instances(work_dir, scope=None):
    """Yield (project, pid_file, state) for every runner pidfile in work_dir."""
    out = []
    for pid_file in sorted(glob.glob(os.path.join(work_dir, "runner*.pid"))):
        m = re.match(r"^runner(?:-([^.]+))?\.pid$", os.path.basename(pid_file))
        project = m.group(1).upper() if m and m.group(1) else None
        if scope is not None and project != scope:
            continue
        out.append((project, pid_file, load_state(state_path(work_dir, project))))
    return out


def fmt_duration(seconds):
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m}m"
    if m:
        return f"{m}m{s}s"
    return f"{s}s"


def task_from_log(path):
    base = os.path.basename(path)
    return re.sub(r"-\d+\.log$", "", base).upper()


# ---- daemonize -------------------------------------------------------------


def _redirect_stdio(log_path):
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    out = open(log_path, "a", buffering=1)
    devnull = open(os.devnull, "r")
    os.dup2(devnull.fileno(), sys.stdin.fileno())
    os.dup2(out.fileno(), sys.stdout.fileno())
    os.dup2(out.fileno(), sys.stderr.fileno())


def _run_daemon(config):
    runner = Runner(config)

    def stop(signum, _frame):
        log(f"received {signal.Signals(signum).name}, shutting down")
        runner.stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    runner.run()


def _start_background(config, pid_file):
    scope = config.get("_project_scope")
    log_path = runner_log_path(config["work_dir"], scope)
    os.makedirs(config["work_dir"], exist_ok=True)
    pid = os.fork()
    if pid > 0:
        # Original parent: wait for the daemon to announce itself, then return.
        deadline = time.time() + 5
        while time.time() < deadline:
            live = read_live_pid(pid_file)
            if live:
                print(f"orbit-runner: started (pid {live}); logs at {log_path}")
                return
            time.sleep(0.1)
        print(
            "orbit-runner: started, but the pidfile did not appear in time — "
            f"check {log_path}",
            file=sys.stderr,
        )
        return
    # First child: detach into its own session, then fork the real daemon.
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    _redirect_stdio(log_path)
    _run_daemon(config)
    os._exit(0)


# ---- subcommands -----------------------------------------------------------


def cmd_start(args):
    config = read_config(args.config)
    config["_project_scope"] = args.project.upper() if args.project else None
    config["_cli_model"] = args.model
    config["_cli_effort"] = args.effort
    config["_follow"] = args.follow
    validate_for_start(config)

    pid_file = pidfile_path(config["work_dir"], config["_project_scope"])
    existing = read_live_pid(pid_file)
    if existing:
        scope = config["_project_scope"]
        sys.exit(
            f"orbit-runner: already running for {scope or 'all projects'} "
            f"(pid {existing}); pidfile {pid_file}"
        )

    foreground = args.foreground or args.follow  # --follow only makes sense inline
    if foreground:
        os.makedirs(config["work_dir"], exist_ok=True)
        _run_daemon(config)
    else:
        _start_background(config, pid_file)


def cmd_stop(args):
    config = read_config(args.config)
    work_dir = config["work_dir"]
    scope = args.project.upper() if args.project else None
    instances = discover_instances(work_dir, scope)
    if not instances:
        target = f"for {scope}" if scope else ""
        print(f"orbit-runner: no runner {target} is running".replace("  ", " "))
        return
    for project, pid_file, _state in instances:
        label = project or "all"
        pid = read_pid(pid_file)
        if not pid_alive(pid):
            _safe_remove(pid_file, state_path(work_dir, project))
            print(f"{label}: not running (removed stale pidfile)")
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            _safe_remove(pid_file, state_path(work_dir, project))
            print(f"{label}: not running (removed stale pidfile)")
            continue
        deadline = time.time() + 12
        while pid_alive(pid) and time.time() < deadline:
            time.sleep(0.2)
        if pid_alive(pid):
            print(f"{label}: SIGTERM sent to pid {pid} (still shutting down)")
        else:
            print(f"{label}: stopped (pid {pid})")
        if os.path.exists(plist_path(project)):
            print(
                f"  note: launchd agent {launchd_label(project)} is installed and "
                f"will restart it — run `orbit-runner uninstall"
                + (f" --project {project}" if project else "")
                + "` to remove it."
            )


def cmd_status(args):
    config = read_config(args.config)
    work_dir = config["work_dir"]
    scope = args.project.upper() if args.project else None
    instances = discover_instances(work_dir, scope)
    if not instances:
        where = f" for {scope}" if scope else ""
        print(f"orbit-runner: no runner instances{where} (not running)")
        return
    now = time.time()
    for project, pid_file, state in instances:
        label = project or "all"
        pid = read_pid(pid_file)
        if not pid_alive(pid):
            print(f"● {label:<8} stale     pidfile points to dead pid {pid}")
            continue
        up = fmt_duration(now - state.get("started", now))
        model = state.get("model") or "default"
        effort = state.get("effort") or "default"
        slots = state.get("slots", [])
        max_slots = state.get("max_slots", "?")
        paused = state.get("paused_until", 0) or 0
        pause_note = ""
        if paused > now:
            pause_note = (
                f"  PAUSED until {datetime.fromtimestamp(paused):%Y-%m-%d %H:%M}"
            )
        print(
            f"● {label:<8} running   pid {pid}  up {up}  "
            f"model={model} effort={effort}  slots {len(slots)}/{max_slots}"
            f"{pause_note}"
        )
        for s in slots:
            s_up = fmt_duration(now - s.get("started", now))
            print(
                f"    {s['task']:<10} pid {s.get('pid','?'):<7} up {s_up:<7} "
                f"{s.get('branch','')}"
            )


def cmd_logs(args):
    config = read_config(args.config)
    work_dir = config["work_dir"]
    logs_dir = os.path.join(work_dir, "logs")
    scope = args.project.upper() if args.project else None
    task = args.task

    def collect():
        if task:
            matches = sorted(glob.glob(os.path.join(logs_dir, f"{task.lower()}-*.log")))
            return matches[-1:]
        files = []
        for _project, _pid_file, state in discover_instances(work_dir, scope):
            for s in state.get("slots", []):
                lp = s.get("log_path")
                if lp and os.path.exists(lp) and lp not in files:
                    files.append(lp)
        return files

    files = collect()
    if not files and not args.follow:
        if task:
            sys.exit(f"orbit-runner: no logs for {task} under {logs_dir}")
        print("orbit-runner: no active sessions")
        return
    if not args.follow:
        for i, fp in enumerate(files):
            if i:
                print()
            print(f"==> {os.path.basename(fp)} <==")
            print(_tail_text(fp, 200))
        return
    _follow_logs(files, work_dir, scope, multiplex=(task is None))


def cmd_init(args):
    path = os.path.expanduser(args.config)
    existing = load_state(path) if os.path.exists(path) else {}

    def ask(prompt, default=""):
        shown = f" [{default}]" if default else ""
        try:
            answer = input(f"{prompt}{shown}: ").strip()
        except EOFError:
            answer = ""
        return answer or default

    print("orbit-runner init — press Enter to accept [defaults].\n")
    api_url = ask(
        "Orbit API base URL (daemon appends /mcp)",
        existing.get("api_url") or os.environ.get("ORBIT_API_URL") or "https://orbitagents.dev",
    )
    if api_url.rstrip("/").endswith("/mcp"):
        api_url = api_url.rstrip("/")[: -len("/mcp")].rstrip("/")
        print(f"  (stripped trailing /mcp -> {api_url})")
    env_key = os.environ.get("ORBIT_API_KEY")
    key_prompt = "Orbit API key" + (" (blank = use ORBIT_API_KEY from env)" if env_key else "")
    api_key = ask(key_prompt, existing.get("api_key", ""))
    model = ask("Default model (blank = account default; e.g. opus, fable)", existing.get("model") or "")
    effort = ask("Default effort (blank = default; e.g. low, medium, high)", existing.get("effort") or "")

    projects = dict(existing.get("projects") or {})
    print("\nMap Orbit project keys to repos (blank key to finish):")
    while True:
        key = ask("  Project key (e.g. ORB)")
        if not key:
            break
        key = key.upper()
        prior = projects.get(key, {}) if isinstance(projects.get(key), dict) else {}
        repo = ask(f"  Repo path for {key}", prior.get("repo", ""))
        entry = {"repo": repo}
        pm = ask(f"  Model override for {key} (optional)", prior.get("model", ""))
        pe = ask(f"  Effort override for {key} (optional)", prior.get("effort", ""))
        if pm:
            entry["model"] = pm
        if pe:
            entry["effort"] = pe
        projects[key] = entry

    config = dict(existing)
    config["api_url"] = api_url
    if api_key:
        config["api_key"] = api_key
    if model:
        config["model"] = model
    else:
        config.pop("model", None)
    if effort:
        config["effort"] = effort
    else:
        config.pop("effort", None)
    if projects:
        config["projects"] = projects
    for k in ("poll_interval_seconds", "max_slots", "work_dir"):
        config.setdefault(k, DEFAULTS[k])
    config.pop("_config_path", None)

    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    print(f"\nWrote {path}")
    if not (api_key or env_key):
        print("Note: no API key set — export ORBIT_API_KEY or re-run init before `start`.")


def cmd_install(args):
    config = read_config(args.config)
    scope = args.project.upper() if args.project else None
    if scope and scope not in (config.get("projects") or {}):
        print(
            f"warning: project {scope} is not in {config['_config_path']}",
            file=sys.stderr,
        )
    script = os.path.abspath(__file__)

    link = os.path.expanduser("~/.local/bin/orbit-runner")
    os.makedirs(os.path.dirname(link), exist_ok=True)
    if os.path.islink(link) or os.path.exists(link):
        if os.path.realpath(link) != script:
            try:
                os.remove(link)
                os.symlink(script, link)
            except OSError as err:
                print(f"warning: could not update {link}: {err}", file=sys.stderr)
    else:
        os.symlink(script, link)
    print(f"linked {link} -> {script}")
    if os.path.dirname(link) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"note: add {os.path.dirname(link)} to your PATH to use `orbit-runner`")

    label = launchd_label(scope)
    plist = plist_path(scope)
    log_path = runner_log_path(config["work_dir"], scope)
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    os.makedirs(config["work_dir"], exist_ok=True)

    program = [sys.executable, script, "start", "--foreground", "--config", config["_config_path"]]
    if scope:
        program += ["--project", scope]
    env = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")}
    for k in ("ORBIT_API_URL", "ORBIT_API_KEY"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    plist_data = {
        "Label": label,
        "ProgramArguments": program,
        "RunAtLoad": True,
        "KeepAlive": True,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        "EnvironmentVariables": env,
        "ProcessType": "Background",
    }
    with open(plist, "wb") as f:
        plistlib.dump(plist_data, f)
    print(f"wrote {plist}")
    try:
        _launchctl_load(plist, label)
        print(f"loaded launchd agent {label} (RunAtLoad + KeepAlive)")
    except RunnerError as err:
        print(f"warning: could not load the agent automatically: {err}", file=sys.stderr)
        print(f"load it by hand: launchctl bootstrap gui/{os.getuid()} {plist}")


def cmd_uninstall(args):
    config = read_config(args.config)
    scope = args.project.upper() if args.project else None
    label = launchd_label(scope)
    plist = plist_path(scope)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    subprocess.run(["launchctl", "unload", plist], capture_output=True)
    removed = []
    if os.path.exists(plist):
        os.remove(plist)
        removed.append(plist)
    link = os.path.expanduser("~/.local/bin/orbit-runner")
    if os.path.islink(link) and os.path.realpath(link) == os.path.abspath(__file__):
        os.remove(link)
        removed.append(link)
    if removed:
        print("removed:\n  " + "\n  ".join(removed))
    else:
        print("nothing to remove")
    print(f"note: worktrees and logs under {config['work_dir']} were left in place")


# ---- launchd helpers -------------------------------------------------------


def launchd_label(project):
    return "dev.orbitagents.runner" + (f".{project.lower()}" if project else "")


def plist_path(project):
    return os.path.expanduser(f"~/Library/LaunchAgents/{launchd_label(project)}.plist")


def _launchctl_load(plist, label):
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
    r = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", plist], capture_output=True, text=True
    )
    if r.returncode == 0:
        return
    r2 = subprocess.run(
        ["launchctl", "load", "-w", plist], capture_output=True, text=True
    )
    if r2.returncode != 0:
        raise RunnerError((r.stderr or r2.stderr or "launchctl failed").strip())


# ---- log tailing -----------------------------------------------------------


def _safe_remove(*paths):
    for p in paths:
        try:
            os.remove(p)
        except OSError:
            pass


def _tail_text(path, lines):
    try:
        with open(path, "r", errors="replace") as f:
            data = f.read().splitlines()
    except OSError as err:
        return f"(cannot read {path}: {err})"
    return "\n".join(data[-lines:])


def _follow_logs(initial, work_dir, scope, multiplex):
    handles = {}

    def emit(path, text):
        if not text:
            return
        prefix = f"[{task_from_log(path)}] " if multiplex else ""
        for line in text.splitlines():
            print(prefix + line, flush=True)

    def open_tail(path):
        try:
            f = open(path, "r", errors="replace")
        except OSError:
            return
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 4000))
        tail = f.read()
        if size > 4000:
            tail = tail.split("\n", 1)[-1]  # drop the partial first line
        emit(path, tail)
        handles[path] = f

    for p in initial:
        open_tail(p)
    if not handles:
        print("orbit-runner: waiting for an active session… (Ctrl-C to quit)")
    last_scan = 0.0
    try:
        while True:
            for path, f in list(handles.items()):
                chunk = f.read()
                if chunk:
                    emit(path, chunk)
            if multiplex and time.time() - last_scan > 2:
                last_scan = time.time()
                for _project, _pid_file, state in discover_instances(work_dir, scope):
                    for s in state.get("slots", []):
                        lp = s.get("log_path")
                        if lp and lp not in handles and os.path.exists(lp):
                            open_tail(lp)
            time.sleep(0.4)
    except KeyboardInterrupt:
        pass
    finally:
        for f in handles.values():
            try:
                f.close()
            except OSError:
                pass


# ---- CLI -------------------------------------------------------------------


def build_parser():
    parser = argparse.ArgumentParser(
        prog="orbit-runner",
        description="Turn agent-ready Orbit tasks into headless Claude Code sessions.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def with_config(sp):
        sp.add_argument(
            "--config",
            default=DEFAULT_CONFIG_PATH,
            help=f"config file (default {DEFAULT_CONFIG_PATH})",
        )

    sp = sub.add_parser("start", help="start the runner daemon")
    with_config(sp)
    sp.add_argument("-p", "--project", help="scope this instance to one project key")
    sp.add_argument("-m", "--model", help="model for spawned sessions (e.g. opus, fable)")
    sp.add_argument("-e", "--effort", help="effort for spawned sessions (e.g. low, high)")
    sp.add_argument("-F", "--foreground", action="store_true", help="run in the foreground")
    sp.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="stream every active session's output to the console (implies --foreground)",
    )
    sp.set_defaults(func=cmd_start)

    sp = sub.add_parser("stop", help="stop the runner daemon (SIGTERM via pidfile)")
    with_config(sp)
    sp.add_argument("-p", "--project", help="stop only this project's instance")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("status", help="show daemon state, slots, and rate-limit pause")
    with_config(sp)
    sp.add_argument("-p", "--project", help="show only this project's instance")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("logs", help="tail per-session logs")
    with_config(sp)
    sp.add_argument("task", nargs="?", help="task id, e.g. ORB-42 (default: all active)")
    sp.add_argument("-f", "--follow", action="store_true", help="follow live output")
    sp.add_argument("-p", "--project", help="restrict to one project's instance")
    sp.set_defaults(func=cmd_logs)

    sp = sub.add_parser("init", help="interactively create a config file")
    with_config(sp)
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("install", help="symlink + launchd agent so the runner starts at login")
    with_config(sp)
    sp.add_argument("-p", "--project", help="install a per-project instance")
    sp.set_defaults(func=cmd_install)

    sp = sub.add_parser("uninstall", help="remove the launchd agent and symlink")
    with_config(sp)
    sp.add_argument("-p", "--project", help="uninstall the per-project instance")
    sp.set_defaults(func=cmd_uninstall)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
