"""Execute phase: run agents one at a time and hand off each result before the next."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from . import journal
from .config import Settings
from .system import executable, git_out, write_json


def read(path: Path, default: str = "") -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return default


def repository_records(meta: dict, task_dir: Path) -> list[dict]:
    return meta.get("repositories") or [{"name": meta.get("repo"), "repo": meta.get("repo"), "role": "implementation",
                                         "base": meta.get("base"), "branch": meta.get("branch"),
                                         "worktree": str(task_dir / "wt")}]


def valid_outcome(result: dict, repositories: list[dict]) -> dict | None:
    """The agent's final JSON outcome, or None when it breaks the contract."""
    if not isinstance(result, dict) or result.get("is_error") is not False:
        return None
    try:
        outcome = json.loads(result.get("result") or "")
    except (TypeError, ValueError):
        return None
    if not isinstance(outcome, dict):
        return None
    summary, blockers = outcome.get("summary"), outcome.get("blocked_repositories")
    if not isinstance(summary, str) or not summary or not isinstance(blockers, list):
        return None
    if outcome.get("status") in ("complete", "no_change"):
        return outcome if not blockers else None
    references = {r["name"] for r in repositories if r["role"] == "reference"}
    if outcome.get("status") == "blocked" and blockers and all(
            isinstance(b, dict) and isinstance(b.get("name"), str) and b["name"]
            and isinstance(b.get("reason"), str) and b["reason"] and b["name"] in references
            for b in blockers):
        return outcome
    return None


def run_agent(settings: Settings, task_dir: Path, meta: dict) -> None:
    """Run the agent for one prepared task and record status and summary.md."""
    repositories = repository_records(meta, task_dir)
    extra_dirs = [r["worktree"] for r in repositories[1:]]
    if (task_dir / "attachments").is_dir():
        extra_dirs.append(str(task_dir / "attachments"))
    command = [executable(settings.agent_command), "-p", "--output-format", "json",
               "--max-turns", str(settings.max_turns), "--allowedTools", ",".join(settings.allowed_tools)]
    if extra_dirs:
        command += ["--add-dir", *extra_dirs]
    # The prompt goes through stdin: Windows caps command lines near 32K characters.
    with open(task_dir / "prompt.md", "rb") as prompt, open(task_dir / "result.json", "wb") as out, \
            open(task_dir / "claude.err", "wb") as err:
        rc = subprocess.run(command, cwd=task_dir / "wt", stdin=prompt, stdout=out, stderr=err).returncode

    try:
        result = json.loads(read(task_dir / "result.json"))
    except ValueError:
        result = None
    summary = [result.get("result") or ""] if isinstance(result, dict) and isinstance(result.get("result"), str) else [""]
    outcome = valid_outcome(result, repositories) if rc == 0 else None
    if outcome:
        write_json(task_dir / "outcome.json", outcome)
        summary = [outcome["summary"]]

    total_ahead, repositories_ok, scope_ok = 0, True, True
    for r in repositories:
        worktree = r["worktree"]
        branch = git_out("-C", worktree, "symbolic-ref", "--short", "HEAD") or "detached"
        status = git_out("-C", worktree, "status", "--porcelain")
        dirty = "status failed" if status is None else status
        head = git_out("-C", worktree, "rev-parse", "HEAD") or "unknown"
        if r["role"] == "reference":
            if head != r["base"] or branch != "detached" or dirty:
                repositories_ok = scope_ok = False
                summary.append(f"Reference repository changed: {r['name']} ({worktree}). Review retained work.")
        else:
            total_ahead += int(git_out("-C", worktree, "rev-list", "--count", f"{r['base']}..HEAD") or 0)
            if branch != r["branch"]:
                scope_ok = False
            if branch != r["branch"] or dirty:
                repositories_ok = False
                summary.append(f"Handoff incomplete for {r['name']}: expected branch {r['branch']} and a clean "
                               "worktree. Review retained work.")

    if outcome and outcome["status"] == "blocked" and scope_ok:
        status = "blocked"
        summary += [f"Blocked: reference repository {b['name']} requires a change: {b['reason']}"
                    for b in outcome["blocked_repositories"]]
    elif outcome and outcome["status"] == "complete" and total_ahead > 0 and repositories_ok:
        status = "ok"
    elif outcome and outcome["status"] == "no_change" and total_ahead == 0 and repositories_ok:
        status = "no-change"
    elif outcome and outcome["status"] == "no_change":
        status = "fail"
        summary.append("Handoff incomplete: the agent reported that no code change is needed, so expected no "
                       "implementation commits, clean assigned worktrees, and unchanged reference repositories. "
                       f"Found {total_ahead} implementation commit(s). Review retained work.")
    elif outcome:
        status = "fail"
        summary.append("Handoff incomplete: expected committed implementation changes, clean assigned worktrees, "
                       f"and unchanged reference repositories. Found {total_ahead} implementation commit(s). "
                       "Review retained work.")
    else:
        status = "fail"
        if not summary[0]:
            summary[0] = f"Agent failed (exit {rc}). See {task_dir / 'claude.err'}."
        if rc == 0:
            summary.append("Agent outcome missing or invalid: expected complete, no_change or blocked JSON with a summary and "
                           f"valid reference-repository blockers. Review {task_dir / 'result.json'}.")
    (task_dir / "summary.md").write_text("\n".join(filter(None, summary)) + "\n", encoding="utf-8")
    (task_dir / "status").write_text(f"{status}\n", encoding="utf-8")


def handoff(task: dict, task_dir: Path) -> None:
    """Write one local result to Logseq. No network access or Git publishing."""
    if (task_dir / "reported").exists():
        return
    branch = worktree = validation = repositories = blocked = details = ""
    meta_file = task_dir / "meta.json"
    if meta_file.exists():
        meta = json.loads(read(meta_file))
        branch, worktree = meta.get("branch") or "", str(task_dir / "wt")
        repos = meta.get("repositories") or []
        if repos:
            repositories = " | ".join(
                f"{r['name']} [{r['role']}]: {r['worktree']}; base: {r.get('base_ref') or r['base']}; "
                f"branch: {r['branch'] or 'detached'}" for r in repos)
            if len(repos) > 1:
                branch = ", ".join(f"{r['name']}={r['branch']}" for r in repos if r["role"] == "implementation")
                worktree = ", ".join(f"{r['name']}={r['worktree']}" for r in repos)
    if (task_dir / "skipped").exists():
        status, text = "fail", f"Preparation failed: {read(task_dir / 'skipped').strip()}"
    else:
        status = read(task_dir / "status", "fail").strip() or "fail"
        if status == "blocked":
            outcome = json.loads(read(task_dir / "outcome.json"))
            blocked = " | ".join(f"{b['name']}: {b['reason']}" for b in outcome["blocked_repositories"])
        text = read(task_dir / "summary.md").strip() or "No agent summary available."
        details = str(task_dir / "summary.md")
        if status == "no-change":
            validation = f"No code change needed and no commits made; review the findings in {details}."
        else:
            validation = f"See agent-reported test results and unresolved concerns in {details}; review before pushing."
    journal.report(task["file"], task["id"], status, text, branch=branch, worktree=worktree, validation=validation,
                   repositories=repositories, blocked=blocked, details=details)
    (task_dir / "reported").touch()


def execute(settings: Settings, run_dir: Path, tasks: list[dict]) -> bool:
    """Process tasks in order. Returns False if any handoff could not be written."""
    ok = True
    for task in tasks:
        task_dir = run_dir / task["id"]
        if (task_dir / "reported").exists():
            continue
        if not (task_dir / "status").exists() and not (task_dir / "skipped").exists():
            task_dir.mkdir(parents=True, exist_ok=True)
            prompt = task_dir / "prompt.md"
            if not (task_dir / "meta.json").exists() or not prompt.exists() or not prompt.stat().st_size:
                (task_dir / "skipped").write_text("Task preparation incomplete.\n", encoding="utf-8")
            else:
                try:
                    journal.start(task["file"], task["id"])
                except (journal.JournalError, OSError) as error:
                    print(f"{task['id']}: {error}")
                    (task_dir / "skipped").write_text("Could not mark task DOING.\n", encoding="utf-8")
                else:
                    print(f"== {task['id']}", flush=True)
                    try:
                        run_agent(settings, task_dir, json.loads(read(task_dir / "meta.json")))
                    except FileNotFoundError as error:
                        (task_dir / "summary.md").write_text(f"Agent could not start: {error}\n", encoding="utf-8")
                        (task_dir / "status").write_text("fail\n", encoding="utf-8")
        try:
            handoff(task, task_dir)
        except (journal.JournalError, OSError, ValueError) as error:
            print(f"Handoff failed for {task['id']}: {error}")
            ok = False
    return ok
