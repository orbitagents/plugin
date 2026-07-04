#!/usr/bin/env python3
"""Orbit runner: turns agent-ready Orbit tasks into headless Claude Code sessions.

Polls Orbit's `get_next_task` MCP tool and, for each dispatchable task, creates
a fresh git worktree off the default branch and spawns `claude -p` in it on the
developer's own subscription (no API key). Zero dependencies beyond python3,
git, and the claude CLI.

Usage:
    python3 orbit-runner.py [path/to/config.json]

Config defaults to ~/.config/orbit-runner/config.json — see
orbit-runner.example.json next to this script.
"""

import json
import os
import re
import shlex
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
        self.worktree = worktree
        self.branch = branch
        self.started = time.time()

    def read_new_output(self):
        try:
            with open(self.log_path, "r", errors="replace") as f:
                f.seek(self.offset)
                chunk = f.read()
                self.offset = f.tell()
            return chunk
        except OSError:
            return ""


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

    # ---- polling ---------------------------------------------------------

    def run(self):
        log(
            f"orbit-runner up: {len(self.config['projects'])} project(s), "
            f"{self.config['max_slots']} slot(s), polling every "
            f"{self.config['poll_interval_seconds']}s"
        )
        while not self.stopping:
            try:
                self.tick()
            except (RunnerError, OSError, subprocess.SubprocessError) as err:
                log(f"tick failed (will retry): {err}")
            self._sleep(self.config["poll_interval_seconds"])
        self.shutdown()

    def _sleep(self, seconds):
        # Wake early to reap children promptly.
        deadline = time.time() + seconds
        while not self.stopping and time.time() < deadline:
            time.sleep(min(1, max(0, deadline - time.time())))
            if any(s.proc.poll() is not None for s in self.slots):
                return

    def tick(self):
        self.scan_running_sessions()
        self.reap_finished_sessions()
        if time.time() < self.paused_until:
            return
        while len(self.slots) < self.config["max_slots"] and not self.stopping:
            task = self.next_task()
            if not task or not self.spawn(task):
                return

    # ---- dispatch --------------------------------------------------------

    def next_task(self):
        keys = list(self.config["projects"])
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
        repo = self.config["projects"][project_key]["repo"]
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
        cmd = [self.config["claude_bin"], "-p", prompt, *self.config["claude_args"]]
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
        log(f"{ident}: spawned claude (pid {proc.pid}) in {worktree} [{branch}]")
        return True

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


def load_config(path):
    path = os.path.expanduser(path)
    try:
        with open(path) as f:
            user = json.load(f)
    except FileNotFoundError:
        sys.exit(
            f"orbit-runner: no config at {path}. Copy orbit-runner.example.json "
            f"there (or pass a path as the first argument) and fill it in."
        )
    except json.JSONDecodeError as err:
        sys.exit(f"orbit-runner: invalid JSON in {path}: {err}")
    config = {**DEFAULTS, **user}
    config["api_url"] = os.environ.get(
        "ORBIT_API_URL", config.get("api_url", "http://localhost:3000")
    )
    config["api_key"] = os.environ.get("ORBIT_API_KEY", config.get("api_key", ""))
    if not config["api_key"]:
        sys.exit(
            "orbit-runner: no API key. Set ORBIT_API_KEY (or \"api_key\" in the "
            "config) to an agent key from Orbit's Settings → Agents."
        )
    if not config["projects"]:
        sys.exit('orbit-runner: config needs at least one entry under "projects".')
    for key, proj in config["projects"].items():
        repo = os.path.abspath(os.path.expanduser(proj.get("repo", "")))
        if not os.path.isdir(os.path.join(repo, ".git")) and not os.path.isfile(
            os.path.join(repo, ".git")
        ):
            sys.exit(f'orbit-runner: projects.{key}.repo is not a git repo: {repo!r}')
        proj["repo"] = repo
    claude_bin = shutil.which(os.path.expanduser(config["claude_bin"]))
    if not claude_bin:
        sys.exit(f"orbit-runner: claude CLI not found ({config['claude_bin']!r})")
    config["claude_bin"] = os.path.abspath(claude_bin)
    return config


def main():
    config = load_config(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH)
    runner = Runner(config)

    def stop(signum, _frame):
        log(f"received {signal.Signals(signum).name}, shutting down")
        runner.stopping = True

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    runner.run()


if __name__ == "__main__":
    main()
