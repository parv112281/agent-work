"""Command line: gather while connected, execute after you disconnect."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shlex
import shutil
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from . import config, jira, journal
from .execute import execute
from .gather import Gatherer
from .system import notify, write_json

RUN_SCRIPT = Path(__file__).resolve().parent.parent / "run.py"


class RunError(Exception):
    pass


@contextmanager
def run_lock(work_root: Path):
    work_root.mkdir(parents=True, exist_ok=True)
    lock = work_root / ".agent-run.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise RunError(f"Another run is active (lock: {lock}). Remove it if no run is in progress.") from None
    try:
        yield
    finally:
        lock.rmdir()


def command_line(*args) -> str:
    args = [sys.executable, str(RUN_SCRIPT), *map(str, args)]
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def tags_text(settings) -> str:
    return " and ".join(f"#{tag}" for tag in settings.required_tags)


def cmd_gather(args, settings):
    settings.jira.token()  # fail before reserving tasks
    days = args.days or settings.days
    stamp = dt.datetime.now()
    run_dir = Path(args.run_dir).expanduser() if args.run_dir else \
        settings.work_root / f"{stamp:%Y-%m-%d}" / f"run-{stamp:%H%M%S}-{os.getpid()}"
    with run_lock(settings.work_root):
        if (run_dir / "tasks.json").exists():
            raise RunError(f"Run directory already contains a run: {run_dir}")
        run_dir.mkdir(parents=True, exist_ok=True)
        run_dir = run_dir.resolve()
        (run_dir / "settings.json").write_text(settings.to_json(), encoding="utf-8")
        tasks = journal.pick(settings.logseq_dir, days, settings.required_tags)
        write_json(run_dir / "tasks.json", tasks)
        if not tasks:
            print(f"No tasks with {tags_text(settings)} found.")
            return 0
        print(f"Picked up {len(tasks)} task(s).")
        Gatherer(settings, run_dir).run(tasks)
        (run_dir / "gathered").touch()
    print(f"Gathering complete. After you disconnect the VPN, run:\n  {command_line('execute', run_dir)}")
    return 0


def cmd_execute(args, _settings):
    run_dir = Path(args.run_dir).expanduser()
    if not run_dir.is_dir():
        raise RunError(f"Run directory not found: {run_dir}")
    run_dir = run_dir.resolve()
    if not (run_dir / "tasks.json").exists() or not (run_dir / "gathered").exists() \
            or not (run_dir / "settings.json").exists():
        raise RunError(f"Run has not completed gathering: {run_dir}")
    # Use the settings captured at gather time, so both phases agree.
    settings = config.Settings.from_json((run_dir / "settings.json").read_text(encoding="utf-8"))
    with run_lock(settings.work_root):
        ok = execute(settings, run_dir, json.loads((run_dir / "tasks.json").read_text(encoding="utf-8")))
    if settings.notify:
        notify("Agent run finished. Review task handoffs in Logseq.")
    return 0 if ok else 1


def cmd_tasks(args, settings):
    tasks = journal.pick(settings.logseq_dir, args.days or settings.days, settings.required_tags, dry_run=True)
    if args.json:
        print(json.dumps(tasks, indent=2))
    elif not tasks:
        print(f"No tasks with {tags_text(settings)} found.")
    for t in tasks if not args.json else []:
        repos = ", ".join(t["repos"]) or "(project default)"
        print(f"{t['id']}: {t['title']}  [repos: {repos}]")
    return 0


def cmd_check(args, settings):
    problems = []
    print(f"Config: {settings.source}")
    for label, path in (("Logseq journals", settings.logseq_dir / "journals"), ("Repositories", settings.repos_dir),
                        ("Work root", settings.work_root)):
        exists = path.is_dir()
        print(f"  {label}: {path}{'' if exists else '  (missing)'}")
        if not exists and label != "Work root":
            problems.append(f"{label} directory not found: {path}")
    for name in ("git", settings.agent_command):
        found = shutil.which(name)
        print(f"  {name}: {found or 'not found on PATH'}")
        if not found:
            problems.append(f"{name} not found on PATH")
    for alias, value in settings.repos.items():
        path = Path(value).expanduser()
        path = path if path.is_absolute() else settings.repos_dir / path
        if not (path / ".git").exists():
            problems.append(f"[repos] {alias} is not a Git repository: {path}")
    print(f"  Jira: {settings.jira.url}")
    try:
        settings.jira.token()
        print("  Jira token: found")
        if args.jira:
            fields = jira.list_fields(settings.jira, "")
            print(f"  Jira connection: ok ({len(fields)} fields visible)")
    except config.ConfigError as error:
        problems.append(str(error))
    except jira.JiraError as error:
        problems.append(f"Jira connection failed: {error}")
    for problem in problems:
        print(f"Problem: {problem}")
    print("OK" if not problems else f"{len(problems)} problem(s) found.")
    return 0 if not problems else 1


def cmd_jira(args, settings):
    if args.jira_command == "fields":
        for field_id, name in jira.list_fields(settings.jira, args.text):
            print(f"{field_id}\t{name}")
    else:
        tickets = jira.fetch_tickets(settings.jira, args.keys, args.attachments_dir)
        print(json.dumps(tickets, indent=2, ensure_ascii=False))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="run.py", description=__doc__)
    parser.add_argument("--config", help="Config file (default: $AGENT_RUNNER_CONFIG or config.toml beside run.py).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("gather", help="Reserve tasks and prepare context, worktrees and dependencies (network on).")
    p.add_argument("--days", type=int, help="Journal days to scan, including today (default from config).")
    p.add_argument("--run-dir", help="Fresh run directory (default: <work_root>/<date>/run-<time>-<pid>).")
    p.set_defaults(fn=cmd_gather)

    p = sub.add_parser("execute", help="Run agents for a gathered run and hand off results (network off).")
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_execute)

    p = sub.add_parser("tasks", help="Preview eligible tasks without reserving them.")
    p.add_argument("--days", type=int)
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_tasks)

    p = sub.add_parser("check", help="Validate the config, paths, tools and Jira token.")
    p.add_argument("--jira", action="store_true", help="Also connect to Jira (needs the network).")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser("jira", help="Query Jira with this config.")
    jira_sub = p.add_subparsers(dest="jira_command", required=True)
    f = jira_sub.add_parser("fields", help="List fields matching TEXT, e.g. to find acceptance_field.")
    f.add_argument("text")
    f = jira_sub.add_parser("fetch", help="Print tickets as JSON and download their attachments.")
    f.add_argument("keys", nargs="+")
    f.add_argument("--attachments-dir", default="jira_attachments")
    p.set_defaults(fn=cmd_jira)
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = None if args.command == "execute" else config.load(args.config)
        return args.fn(args, settings)
    except (config.ConfigError, RunError, journal.JournalError, jira.JiraError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
