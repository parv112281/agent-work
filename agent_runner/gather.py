"""Gather phase: Jira context, worktrees and dependencies. The user provides the network."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

from . import jira, journal
from .config import ConfigError, Settings
from .system import executable, git, git_out, write_json

PROMPT = Path(__file__).with_name("prompt.md")


class Skip(Exception):
    """Preparation failed for one task; the message is recorded and reported in Logseq."""


def resolve_repo(settings: Settings, label: str, project_key: str) -> Path | None:
    """A repo:: name, a [repos] alias, a path under repos_dir, or the Jira project default."""
    if label and Path(label).expanduser().is_absolute():
        candidate = Path(label).expanduser()
    else:
        value = settings.repos.get(label or project_key) or label
        if not value:
            return None
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = settings.repos_dir / candidate
    return candidate.resolve() if (candidate / ".git").exists() else None


def base_ref(repo: Path, label: str, requested: str) -> tuple[str, str]:
    """Map a base:: value to (local tracking ref, remote branch)."""
    if requested in ("", "default"):
        ref = git_out("-C", repo, "symbolic-ref", "refs/remotes/origin/HEAD") or "refs/remotes/origin/main"
    elif requested.startswith("refs/remotes/origin/"):
        ref = requested
    elif requested.startswith("origin/"):
        ref = f"refs/remotes/{requested}"
    elif requested.startswith("refs/"):
        raise Skip(f"Unsupported base ref for {label}: {requested}; use an origin branch.")
    else:
        ref = f"refs/remotes/origin/{requested}"
    prefix = "refs/remotes/origin/"
    branch = ref[len(prefix):] if ref.startswith(prefix) else ""
    if not branch or git("check-ref-format", f"refs/heads/{branch}").returncode != 0:
        raise Skip(f"Invalid base branch for {label}: {requested}")
    return ref, branch


def setup_command(worktree: Path) -> list[str] | None:
    if os.name == "nt":
        for name in (".agent-setup.cmd", ".agent-setup.bat"):
            if (worktree / name).is_file():
                return [str(worktree / name)]
        if (worktree / ".agent-setup.ps1").is_file():
            return [executable("powershell"), "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(worktree / ".agent-setup.ps1")]
    elif (worktree / ".agent-setup").is_file() and os.access(worktree / ".agent-setup", os.X_OK):
        return [str(worktree / ".agent-setup")]
    if (worktree / "package-lock.json").is_file():
        return [executable("npm"), "ci"]
    if (worktree / "pom.xml").is_file():
        return [executable("mvn"), "-q", "dependency:go-offline"]
    return None


def prepare_dependencies(worktree: Path, label: str, log: Path) -> None:
    try:
        command = setup_command(worktree)
    except FileNotFoundError as error:
        raise Skip(f"Dependency setup failed for {label}: {error}") from error
    if command is None:
        return
    with open(log, "w", encoding="utf-8") as output:
        rc = subprocess.run(command, cwd=worktree, stdin=subprocess.DEVNULL, stdout=output,
                            stderr=subprocess.STDOUT).returncode
    if rc != 0:
        raise Skip(f"Dependency setup failed for {label} (exit {rc}); see {log}")


class Gatherer:
    def __init__(self, settings: Settings, run_dir: Path):
        self.settings = settings
        self.run_dir = run_dir
        # Cache commits per repository and base ref so tasks with the same base share a snapshot.
        self.bases_file = run_dir / "bases.json"
        self.bases = json.loads(self.bases_file.read_text()) if self.bases_file.exists() else {}

    def base_commit(self, repo: Path, label: str, ref: str, branch: str) -> str:
        cached = self.bases.get(str(repo), {}).get(ref)
        if cached:
            return cached
        # Fetch the requested remote branch explicitly, including with narrow clone refspecs.
        # A deleted or misspelled branch must fail, even if a stale tracking ref exists.
        if git("-C", repo, "fetch", "--no-tags", "origin", f"+refs/heads/{branch}:{ref}", capture=False).returncode:
            raise Skip(f"Could not fetch base branch origin/{branch} for {label}; no default fallback used.")
        commit = git_out("-C", repo, "rev-parse", "--verify", f"{ref}^{{commit}}")
        if not commit:
            raise Skip(f"Could not resolve base commit for {label} ({ref}).")
        self.bases.setdefault(str(repo), {})[ref] = commit
        write_json(self.bases_file, self.bases)
        return commit

    def resolve_repositories(self, task: dict) -> list[dict]:
        implementations = task.get("repos") or ([task["repo"]] if task.get("repo") else [])
        requested = [(name, "implementation") for name in implementations or [""]]
        requested += [(name, "reference") for name in task.get("reference_repos", [])]
        resolved = []
        for label, role in requested:
            repo = resolve_repo(self.settings, label, task["keys"][0].split("-")[0])
            if repo is None:
                raise Skip(f"No repository resolved for {label or 'the Jira project'}; "
                           "check repo properties or [repos] in the config file.")
            label = label or repo.name
            if any(r["path"] == str(repo) for r in resolved):
                raise Skip(f"Repository listed more than once or in conflicting roles: {label}")
            overrides = task.get("base_branches") or {}
            if label in overrides:
                requested_base = overrides[label]
            else:
                requested_base = task.get("base_branch", "") if role == "implementation" else ""
            resolved.append({"name": label, "path": str(repo), "role": role, "requested_base": requested_base})
        unknown = sorted(set(task.get("base_branches") or {}) - {r["name"] for r in resolved})
        if unknown:
            raise Skip(f"base-branches:: names unknown repositories: {', '.join(unknown)}")
        return resolved

    def prepare_repo(self, task_dir: Path, task_id: str, meta: dict, record: dict, ordinal: int) -> None:
        repo, label, role = Path(record["path"]), record["name"], record["role"]
        ref, remote_branch = base_ref(repo, label, record["requested_base"])
        base = self.base_commit(repo, label, ref, remote_branch)
        worktree = task_dir / "wt" if ordinal == 1 else task_dir / "repos" / str(ordinal)
        worktree.parent.mkdir(parents=True, exist_ok=True)
        branch = ""
        if role == "implementation":
            branch = f"agent/{task_id}-{self.run_dir.name}"
            if git("-C", repo, "worktree", "add", "-b", branch, worktree, base, capture=False).returncode:
                raise Skip(f"Could not create worktree/branch for {label} ({branch}).")
        elif git("-C", repo, "worktree", "add", "--detach", worktree, base, capture=False).returncode:
            raise Skip(f"Could not create reference worktree for {label}.")
        # Preserve even partially prepared worktrees for review after a failure.
        entry = {"name": label, "repo": str(repo), "role": role, "base": base,
                 "base_ref": f"origin/{remote_branch}", "branch": branch, "worktree": str(worktree)}
        meta["repositories"].append(entry)
        if "repo" not in meta and role == "implementation":
            meta.update(repo=str(repo), base=base, base_ref=entry["base_ref"], branch=branch)
        write_json(task_dir / "meta.json", meta)
        if role == "implementation":
            prepare_dependencies(worktree, label, task_dir / f"setup-{ordinal}.log")

    def fetch_jira(self, task_dir: Path, keys: list[str]) -> list[dict]:
        attachments = task_dir / "attachments"
        attachments.mkdir(parents=True, exist_ok=True)
        try:
            tickets = jira.fetch_tickets(self.settings.jira, keys, str(attachments))
        except (jira.JiraError, ConfigError, OSError) as error:
            (task_dir / "jira.err").write_text(f"{error}\n", encoding="utf-8")
            raise Skip(f"Jira fetch failed; see {task_dir / 'jira.err'}") from error
        write_json(task_dir / "tickets.json", tickets)
        if not tickets or not all(isinstance(t, dict) and isinstance(t.get("key"), str) for t in tickets):
            raise Skip(f"Invalid or empty Jira JSON; see {task_dir / 'tickets.json'}")
        if any(key not in {t["key"] for t in tickets} for key in keys):
            raise Skip("Jira response is missing a requested ticket.")
        try:
            index = journal.attachment_index(tickets, attachments)
        except journal.JournalError as error:
            (task_dir / "attachments.err").write_text(f"{error}\n", encoding="utf-8")
            raise Skip(f"Attachment preparation failed: {error}") from error
        (task_dir / "attachments.md").write_text(index, encoding="utf-8")
        return tickets

    def prepare(self, task: dict, task_dir: Path) -> None:
        write_json(task_dir / "task.json", task)
        if task.get("base_error"):
            raise Skip(task["base_error"])
        try:
            context = journal.build_context(task, self.settings.logseq_dir)
        except journal.JournalError as error:
            (task_dir / "context.err").write_text(f"{error}\n", encoding="utf-8")
            raise Skip(f"Context preparation failed: {error}") from error
        (task_dir / "context.md").write_text(context, encoding="utf-8")
        if not task["keys"]:
            raise Skip("No Jira ticket key found in the TODO block or its children.")
        tickets = self.fetch_jira(task_dir, task["keys"])
        resolved = self.resolve_repositories(task)
        write_json(task_dir / "resolved-repos.json", resolved)
        meta = {"id": task["id"], "file": task["file"], "repositories": []}
        write_json(task_dir / "meta.json", meta)
        for ordinal, record in enumerate(resolved, start=1):
            self.prepare_repo(task_dir, task["id"], meta, record, ordinal)
        (task_dir / "prompt.md").write_text(build_prompt(task, meta, task_dir, tickets), encoding="utf-8")

    def run(self, tasks: list[dict]) -> None:
        for task in tasks:
            task_dir = self.run_dir / task["id"]
            task_dir.mkdir(parents=True, exist_ok=True)
            if (task_dir / "meta.json").exists() or (task_dir / "skipped").exists():
                print(f"Preparation already recorded for {task['id']}; leaving artifacts intact.")
                continue
            print(f"== {task['id']}", flush=True)
            try:
                self.prepare(task, task_dir)
            except Skip as error:
                (task_dir / "skipped").write_text(f"{error}\n", encoding="utf-8")
                print(f"   skipped: {error}")


def build_prompt(task: dict, meta: dict, task_dir: Path, tickets: list[dict]) -> str:
    """Everything the agent needs, as one prompt."""
    parts = [PROMPT.read_text(encoding="utf-8").rstrip("\n") + "\n", "# Repository workspace"]
    for r in meta["repositories"]:
        branch = r["branch"] or "detached (reference only)"
        parts.append(f"- {r['name']} [{r['role']}]: {r['worktree']}; base: {r['base_ref']} @ {r['base']}; branch: {branch}")
    parts += ["", "# Task", task["block"]]
    for title, name in (("Linked Markdown context", "context.md"), ("Downloaded Jira attachments", "attachments.md")):
        path = task_dir / name
        if path.exists() and path.stat().st_size:
            parts += ["", f"# {title}", path.read_text(encoding="utf-8")]
    if tickets:
        parts += ["", "# Jira tickets"]
        for t in tickets:
            comments = "\n".join(m if isinstance(m, str) else json.dumps(m, ensure_ascii=False)
                                 for m in t.get("messages") or [])
            parts.append(f"## {t['key']}: {t.get('title', '')}\n\n{t.get('description') or ''}\n\n"
                         f"### Acceptance criteria\n{t.get('acceptance_criteria') or 'none given'}\n\n"
                         f"### Comments\n{comments}")
    return "\n".join(parts) + "\n"
