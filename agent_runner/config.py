"""Runner settings, loaded from a TOML file. One file targets one Jira instance.

Lookup order: --config, then $AGENT_RUNNER_CONFIG, then config.toml in the
project directory. Relative paths resolve against the config file's directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_ALLOWED_TOOLS = [
    "Read", "Edit", "Write", "Glob", "Grep",
    "Bash(git add:*)", "Bash(git commit:*)", "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)",
    "Bash(git -C * add:*)", "Bash(git -C * commit:*)", "Bash(git -C * status:*)",
    "Bash(git -C * diff:*)", "Bash(git -C * log:*)",
]


class ConfigError(Exception):
    pass


@dataclass
class JiraSettings:
    url: str
    token_env: str = "JIRA_AUTH_TOKEN"
    token_file: str = ""
    acceptance_field: str = ""
    timeout: int = 30

    def token(self) -> str:
        if self.token_env and os.environ.get(self.token_env, "").strip():
            return os.environ[self.token_env].strip()
        if self.token_file:
            try:
                token = Path(self.token_file).read_text(encoding="utf-8").strip()
            except OSError as error:
                raise ConfigError(f"Cannot read Jira token file {self.token_file}: {error}") from error
            if token:
                return token
        sources = " or ".join(filter(None, [self.token_env and f"${self.token_env}",
                                            self.token_file and f"token_file {self.token_file}"]))
        raise ConfigError(f"No Jira token found; set {sources or 'jira.token_env or jira.token_file'}.")


@dataclass
class Settings:
    source: str
    logseq_dir: Path
    repos_dir: Path
    work_root: Path
    jira: JiraSettings
    required_tags: list[str] = field(default_factory=lambda: ["agent"])
    days: int = 1
    agent_command: str = "claude"
    max_turns: int = 100
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS))
    repos: dict[str, str] = field(default_factory=dict)
    notify: bool = True

    def to_json(self) -> str:
        """Snapshot for a run directory, so execute uses the settings gather used."""
        data = asdict(self)
        for key in ("logseq_dir", "repos_dir", "work_root"):
            data[key] = str(data[key])
        return json.dumps(data, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "Settings":
        data = json.loads(text)
        data["jira"] = JiraSettings(**data["jira"])
        for key in ("logseq_dir", "repos_dir", "work_root"):
            data[key] = Path(data[key])
        return cls(**data)


SCHEMA = {
    "paths": {"logseq_dir", "repos_dir", "work_root"},
    "tasks": {"required_tags", "days"},
    "jira": {"url", "token_env", "token_file", "acceptance_field", "timeout"},
    "agent": {"command", "max_turns", "allowed_tools", "extra_allowed_tools"},
    "notify": {"enabled"},
    "repos": None,  # free-form alias = "path"
}


def find_config(explicit: str | None) -> Path:
    candidate = explicit or os.environ.get("AGENT_RUNNER_CONFIG") or str(PROJECT_DIR / "config.toml")
    path = Path(candidate).expanduser()
    if not path.is_file():
        raise ConfigError(f"Config file not found: {path}. Copy config.example.toml to config.toml "
                          "or pass --config.")
    return path.resolve()


def load(explicit: str | None = None) -> Settings:
    if tomllib is None:
        raise ConfigError("Python 3.11 or newer is required to read TOML config files.")
    path = find_config(explicit)
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ConfigError(f"Cannot read {path}: {error}") from error

    for section, value in data.items():
        if section not in SCHEMA:
            raise ConfigError(f"{path}: unknown section [{section}]")
        if not isinstance(value, dict):
            raise ConfigError(f"{path}: [{section}] must be a table")
        unknown = set(value) - SCHEMA[section] if SCHEMA[section] is not None else set()
        if unknown:
            raise ConfigError(f"{path}: unknown key(s) in [{section}]: {', '.join(sorted(unknown))}")

    def table(name):
        return data.get(name, {})

    def path_value(section, key, default=None):
        value = table(section).get(key, default)
        if value is None:
            raise ConfigError(f"{path}: [{section}] {key} is required")
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{path}: [{section}] {key} must be a non-empty string")
        resolved = Path(value).expanduser()
        return resolved if resolved.is_absolute() else (path.parent / resolved).resolve()

    def typed(section, key, kind, default):
        value = table(section).get(key, default)
        if kind is int and isinstance(value, bool) or not isinstance(value, kind):
            raise ConfigError(f"{path}: [{section}] {key} must be {kind.__name__}")
        return value

    def string_list(section, key, default):
        value = table(section).get(key, default)
        if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
            raise ConfigError(f"{path}: [{section}] {key} must be a list of non-empty strings")
        return value

    jira = table("jira")
    if not isinstance(jira.get("url"), str) or not jira["url"].strip():
        raise ConfigError(f"{path}: [jira] url is required")
    token_file = typed("jira", "token_file", str, "")
    jira_settings = JiraSettings(
        url=jira["url"].strip(),
        token_env=typed("jira", "token_env", str, "JIRA_AUTH_TOKEN"),
        token_file=str(path_value("jira", "token_file")) if token_file else "",
        acceptance_field=typed("jira", "acceptance_field", str, ""),
        timeout=typed("jira", "timeout", int, 30),
    )

    repos = {}
    for alias, value in table("repos").items():
        if not isinstance(value, str) or not value:
            raise ConfigError(f"{path}: [repos] {alias} must be a path string")
        repos[alias] = value

    tags = [tag.lstrip("#") for tag in string_list("tasks", "required_tags", ["agent"])]
    if not tags:
        raise ConfigError(f"{path}: [tasks] required_tags must name at least one tag")
    days = typed("tasks", "days", int, 1)
    max_turns = typed("agent", "max_turns", int, 100)
    if days < 1 or max_turns < 1:
        raise ConfigError(f"{path}: [tasks] days and [agent] max_turns must be at least 1")

    return Settings(
        source=str(path),
        logseq_dir=path_value("paths", "logseq_dir"),
        repos_dir=path_value("paths", "repos_dir"),
        work_root=path_value("paths", "work_root", str(PROJECT_DIR)),
        jira=jira_settings,
        required_tags=tags,
        days=days,
        agent_command=typed("agent", "command", str, "claude"),
        max_turns=max_turns,
        allowed_tools=string_list("agent", "allowed_tools", DEFAULT_ALLOWED_TOOLS)
        + string_list("agent", "extra_allowed_tools", []),
        repos=repos,
        notify=typed("notify", "enabled", bool, True),
    )
