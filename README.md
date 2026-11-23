# Logseq Agent Runner

Turn selected Logseq journal tasks into local, committed implementations using Claude Code. You connect the VPN and start gathering, then disconnect it and start execution. The runner processes tasks one at a time, each with separate worktrees for its implementation and reference repositories, followed by a handoff in Logseq. The runner never inspects, connects, disconnects, or restores the VPN. It runs on Windows, macOS, and Linux.

The next task starts without waiting for your review. You handle reviewing, pushing branches, and creating PRs. See [WORKFLOW.md](WORKFLOW.md) for the agreed workflow.

## Prerequisites

- Python 3.11 or newer and Git on `PATH`. Nothing else needs installing; the runner, including its Jira client, uses only the Python standard library.
- Claude Code (`claude`) installed and authenticated for unattended `claude -p` runs.
- A Jira bearer token for each Jira instance you use (see [Jira](#jira)).
- Access to Jira, repository remotes, and dependency registries during gathering; connect your VPN manually when required.
- Local repositories with an `origin` remote and Git author identity configured for commits.
- Repository-specific tools such as npm or Maven when needed.

On Windows, run the commands below with `py` or `python` in place of `python3`.

## Setup

Copy the example configuration and edit it:

```bash
cp config.example.toml config.toml
python3 run.py check
```

`check` validates the file and reports missing directories, commands, repository aliases, and Jira tokens. Add `--jira` to also test the Jira connection while on the VPN.

The runner reads `--config FILE` when given, otherwise `$AGENT_RUNNER_CONFIG`, otherwise `config.toml` beside `run.py`. `config.toml` is git-ignored because it describes one machine. Paths may use `~`; relative paths resolve against the config file's directory. In TOML, write Windows paths with forward slashes or in single quotes (`'C:\Users\you\repos'`) so backslashes are not read as escapes.

| Section | Key | Default | Purpose |
| --- | --- | --- | --- |
| `[paths]` | `logseq_dir` | required | Logseq graph containing `journals/` and `pages/` |
| | `repos_dir` | required | Parent directory for repository names |
| | `work_root` | project directory | Run artifacts and worktrees |
| `[tasks]` | `required_tags` | `["agent"]` | Tags every task's first line must carry |
| | `days` | `1` | Journal days to scan, including today; `gather --days N` overrides |
| `[jira]` | `url` | required | Jira REST API v2 base URL |
| | `token_env` | `JIRA_AUTH_TOKEN` | Environment variable holding the bearer token |
| | `token_file` | none | File holding the token, used when the variable is unset |
| | `acceptance_field` | discovered | Acceptance criteria field id, e.g. `customfield_12345` |
| | `timeout` | `30` | Seconds per HTTP request |
| `[agent]` | `command` | `claude` | Claude Code command |
| | `max_turns` | `100` | Agent turn limit per task |
| | `allowed_tools` | Read, Edit, Write, Glob, Grep, local Git | Replaces the default tool list |
| | `extra_allowed_tools` | `[]` | Added to the tool list; put test commands here |
| `[notify]` | `enabled` | `true` | Desktop notification when execution finishes |
| `[repos]` | *alias* | none | Repository aliases and Jira project defaults |

Add each repository's validation commands to `extra_allowed_tools`, for example `"Bash(npm test:*)"`, `"Bash(mvn test:*)"`, or `"Bash(dotnet test:*)"`. A custom `allowed_tools` list replaces the defaults, so keep the `git -C` forms in it.

`[repos]` maps names to repositories. Relative values resolve under `repos_dir`, so the same file works on machines with different home directories:

```toml
[repos]
api-service = "projects/api-service"
web-app = "projects/web-app"
PROJ = "projects/api-service"        # default for PROJ tickets that name no repository
```

### Jira

Each config file targets one Jira instance. Keep one file per instance and pick it per run:

```bash
python3 run.py --config client-jira.toml gather
```

The token is read from the environment variable named by `token_env`. When that variable is unset, the runner reads `token_file` instead, which suits machines where setting environment variables is awkward. Keep token files outside the project and readable only by you. Gathering stops before reserving any tasks if no token is found.

Without `acceptance_field`, the runner looks for a field named like "Acceptance Criteria" and falls back to an "Acceptance Criteria" section of the description. To find a field id:

```bash
python3 run.py jira fields acceptance
python3 run.py jira fetch PROJ-123 --attachments-dir /tmp/jira-check   # inspect one ticket's JSON
```

Each requested key must appear in the response. Prompt generation uses each ticket's `key`, `title`, `description`, `acceptance_criteria`, and `messages`.

Gathering downloads attachments into each task's `attachments/<ticket-key>/` directory, validates the local files, and lists their paths in `attachments.md` and the agent prompt. Attachment names are made safe for every platform. The agent receives access to that directory for offline reading; attachments are context and must not be executed or edited. Missing files or failed downloads stop that task rather than silently omitting attachment context. Tickets without attachments continue normally.

## Add tasks in Logseq

Eligible blocks must be `TODO` or `LATER` and carry **every tag in `required_tags` on the task's first line**; the examples below use `#agent` and `#client`. Linked tags such as `#[[agent]]` are also supported. `agent:: ready` alone does not qualify a task.

```text
- TODO Implement add 2 numbers function [[Projects/Example]] #PROJ-5000 #agent #client repo::my-service
  - Modify the calculator code to add the addition function.
  - The numbers should only be integers.
  - Look at [Details](../assets/addition-spec.md) for more information.
  - Follow [[Integer calculator requirements]].
```

Write the implementation context in the TODO and its child blocks, or link a separate Markdown document. Parent meeting notes are not collected, and ticket keys are read only from the TODO block and its children. Both `#PROJ-5000` and `PROJ-5000` identify the Jira ticket. Jira details are still fetched during gathering.

`repo::my-service` may appear inline as shown above, or on its own property line as `repo:: my-service`. Named repositories first use an exact alias in `[repos]`, then fall back to a path under `repos_dir` if no alias exists. Without a repository property, the runner uses the Jira project entry in `[repos]`.

### Multiple repositories

Use `repos::` to list repositories the agent may change and `reference-repos::` for repositories it may inspect for context. Separate names with commas:

```text
- TODO Add integer addition across the stack #PROJ-5000 #agent #client
  repos:: web-ui, web-api, calculator-service
  reference-repos:: shared-library, legacy-calculator
  - Update the UI, API endpoint, and calculator service.
  - Use the shared library and legacy implementation for context only.
```

Existing `repo::` tasks remain supported. Names in both repository roles resolve through exact aliases in `[repos]`, or under `repos_dir` when no alias exists; absolute repository paths also work. Relative alias values resolve under `repos_dir`. When no implementation repository is specified, `[repos]` supplies one from the Jira project key. A named repository never falls back to an unrelated Jira project default, and an alias pointing to a missing repository fails preparation. Listing the same repository in both roles fails preparation.

Gathering creates a branch and worktree for each implementation repository and a detached worktree for each reference repository. It prepares dependencies only for implementation repositories. One agent handles the entire ticket, with access to all listed worktrees through Claude's documented [additional-directory option](https://code.claude.com/docs/en/cli-reference). The default allowed tools include `git -C` forms for local add, commit, status, diff, and log commands; include those forms when supplying your own `allowed_tools` list.

The agent is instructed to leave reference repositories unchanged. After execution, the runner verifies their Git state and checks every implementation worktree for the assigned branch and uncommitted changes. This is a task contract with post-run checks, not a filesystem sandbox. A ticket requires at least one implementation commit overall; listed implementation repositories that need no changes may remain untouched. Each touched repository must have its changes committed. A failure in any repository fails the ticket, preserves all worktrees, and allows the next ticket to proceed.

If the agent determines that a reference repository must change, it must stop and report a **blocked** outcome naming that repository and the required change. It cannot promote a reference repository into implementation scope. The task returns to `TODO` with `agent-status:: blocked` and `agent-blocked::` explaining the required scope change, even if other implementation commits already exist. Partial work is retained, and the next ticket proceeds. Changing a reference worktree anyway is a failure rather than an authorized scope expansion.

### Base branches

Use `base::` when implementation work should start from a long-running epic branch:

```text
- TODO Add integer addition #PROJ-5000 #agent #client repo::web-api
  base:: epic/PROJ-5000
```

For multiple repositories, `base-branches::` overrides the task-wide base by repository name. Use the same names or aliases as in `repos::` and `reference-repos::`. The special value `default` selects that repository's normal default branch:

```text
- TODO Full stack addition #PROJ-5000 #agent #client
  repos:: web-ui, web-api
  reference-repos:: shared-library
  base:: epic/PROJ-5000
  base-branches:: web-api=default, shared-library=release/integer-contract
```

Here, the UI starts from the epic branch, the API starts from its default branch, and the reference library is read from the specified release branch. The task-wide `base::` applies only to implementation repositories; references use their default branch unless explicitly overridden.

Branch values may be bare names such as `epic/PROJ-5000` or prefixed with `origin/`. Without a requested base, gathering uses `origin/HEAD`, falling back to `origin/main` only when the default-branch pointer is absent. It explicitly fetches the selected remote branch and fails the task if that branch is missing, invalid, or cannot be fetched. It never substitutes the default for a missing requested epic branch. Unknown repository names or malformed entries in `base-branches::` also fail preparation.

Gathering records the base ref and exact commit in `meta.json` and the prompt. Snapshots in `bases.json` are cached per repository and base ref, so different tasks can use different bases in the same repository. The agent commits only to its new task branch; the epic branch remains unchanged. Ticket-like strings inside base properties are not treated as additional Jira tickets. These settings apply to newly gathered runs; existing worktrees retain their captured bases.

### Markdown context documents

Standard Markdown file links anywhere in the task block are supported: `[Details](../assets/addition-spec.md)`, `[Details](file:///absolute/path/addition-spec.md)`, or `[Details](</absolute/path/details with spaces.md>)`. Relative paths resolve from the journal file's directory. URL-encoded spaces and link fragments are supported. Only local `.md` and `.markdown` documents are included; remote URLs are not fetched.

Use `[[page references]]` in child blocks to include Markdown pages from the graph's `pages/` directory. Pages resolve by matching filenames or an explicit `title::` property; namespace filenames using `___` also match slash-separated page names. Page references on the TODO's first line, such as `[[Projects/Example]]`, organize the task and do not load page contents.

Linked documents are copied into `context.md` and included in `prompt.md` before the agent runs. Links inside those documents are not followed recursively. Missing, unreadable, or ambiguous context documents stop that task with an explanation, while the remaining tasks continue.

Journals are read from `journals/YYYY_MM_DD.md`. Tasks already carrying `agent-run::` are excluded, including previously failed tasks.

## Preview and run

Preview eligible tasks without modifying the journal or using the network:

```bash
python3 run.py tasks --days 1
```

Connect your VPN manually, then gather a batch:

```bash
python3 run.py gather
```

Gathering reserves selected tasks, fetches Jira context and repository updates, and prepares dependencies. It saves the settings it used as `settings.json` in the run directory, exits without starting any agents, and prints the exact execution command for that run.

Disconnect your VPN manually, then run the printed command, for example:

```bash
python3 run.py execute ~/agent-work/2026-10-13/run-120000-12345
```

Execution uses the saved batch and the saved settings, so it needs no `--config`; it does not select new tasks or fetch context again. It marks each task `DOING` when execution starts and runs the agent in its worktree, passing the prompt on standard input. Tasks using the same repository and base ref share the same captured base commit. Execution requires a completed gathering phase. Neither phase checks or changes your connection state, including on failure or interruption.

Dependency preparation runs the first applicable option: a repository setup script, `npm ci` when `package-lock.json` exists, or `mvn -q dependency:go-offline` when `pom.xml` exists. The setup script is an executable `.agent-setup` on macOS and Linux, and `.agent-setup.cmd`, `.agent-setup.bat`, or `.agent-setup.ps1` on Windows. Commit it to the repository so it appears in new worktrees.

Only one orchestrator run can execute at a time. Keep tasks independent within a batch; defer tasks that depend on changes you have not integrated yet. The runner edits journal files directly, keeping each file's existing line endings, so avoid editing the affected page while a run is updating it.

## Review results

Completed implementations become `WAITING` with `agent-branch::`, `agent-worktree::`, `agent-repositories::`, `agent-result::`, and `agent-validation::` properties. For multiple repositories, the branch and worktree properties list each repository by name; `agent-repositories::` also records roles. A successful handoff requires a successful agent response, at least one implementation commit overall, clean implementation worktrees on their assigned branches, and unchanged reference Git state. Test results are agent-reported and need your review.

`agent-result::` holds the summary's one-line headline. The prompt asks the agent to write the rest as Markdown sections with nested bullets, which the handoff writes as an **Agent summary** child block tree directly under the task's properties, ending with a link to `summary.md`. Summary lines that would read as Logseq tasks or properties are neutralized, so a summary can never be picked up as a new task. When retrying, delete the **Agent summary** block along with the `agent-*` properties.

Every handoff includes `agent-status::` (`ok`, `no-change`, `fail`, or `blocked`). The prompt requires a final JSON outcome with `status` (`complete`, `no_change`, or `blocked`), a `summary`, and a `blocked_repositories` array of `{name, reason}` objects. The runner validates that each blocker names a listed reference repository and saves the outcome as `outcome.json`. Missing or invalid outcomes fail the task; they cannot become `WAITING`. Scope blockers are reported by the agent, while repository state is independently checked by the runner.

When the agent finishes a task that needs no code (an investigation that found a false positive, behaviour the code already has, or a cause such as a stale deployment), it reports `no_change`. The runner accepts that only with zero implementation commits, clean implementation worktrees on their assigned branches, and unchanged reference repositories. The task becomes `WAITING` with `agent-status:: no-change`, so you can act on the findings and any follow-up in the summary. A `no_change` outcome with commits or a dirty worktree fails. A `complete` outcome with no commits still fails, because the agent claimed to have implemented something.

Open the recorded worktree to inspect the changes and validation details, then push and create the PR yourself. Branches, worktrees, and artifacts remain available after the run.

Artifacts live under:

```text
<work_root>/<date>/run-<time>-<pid>/
  settings.json
  tasks.json
  bases.json
  gathered
  <task-id>/
    task.json, context.md, tickets.json, prompt.md, meta.json
    wt/
    repos/<ordinal>/
    attachments/<ticket-key>/
    attachments.md
    result.json, outcome.json, summary.md, status, reported
```

`wt/` contains the first implementation worktree; `repos/` contains additional implementation and reference worktrees. `meta.json` records each repository's role, source path, base commit, branch, and worktree. Failed preparation may produce `skipped`, `context.err`, `jira.err`, `attachments.err`, or `setup-<ordinal>.log`; agent errors are recorded in `claude.err`. Some artifacts are absent when a task fails before reaching that stage.

## Failures and retries

A failed task returns to `TODO` with an explanation, and the runner continues with the remaining tasks. Read the Logseq handoffs even when the runner exits successfully: individual task failures are reported there.

To retry, review the retained work, remove the task's `agent-run::` and old result, branch, worktree, repositories, validation, status, and blocked properties, delete its **Agent summary** block, and leave it `TODO` with its tags. For a scope blocker, first decide whether to move the named repository from `reference-repos::` to `repos::` or revise the task so that repository can remain unchanged. The next run creates fresh branches and worktrees. An interrupted run may leave reserved or `DOING` tasks requiring the same manual reset. Blocked tasks retain `agent-run::` and are not retried automatically.

The run lock is removed on normal exit and handled interruptions. If a forced termination leaves `<work_root>/.agent-run.lock`, confirm no runner is active before removing that empty directory.

## Files and verification

| File | Role |
| --- | --- |
| `run.py` | Command line: `gather`, `execute`, `tasks`, `check`, `jira` |
| `config.example.toml` | Documented example configuration |
| `agent_runner/config.py` | Loads and validates config files |
| `agent_runner/journal.py` | Selects, reserves, starts, and reports tasks |
| `agent_runner/jira.py` | Jira REST client: tickets, comments, attachments |
| `agent_runner/gather.py` | Fetches context and prepares branches, worktrees, and dependencies |
| `agent_runner/execute.py` | Runs agents sequentially and hands off each result |
| `agent_runner/prompt.md` | Instructions given to every agent |
| `tests/` | Local unit and integration checks |

Run the checks with:

```bash
python3 -m unittest discover -s tests -v
```

The checks use real local Git repositories and worktrees, a local fake Jira server, and a stubbed Claude command. They simulate user-controlled connection states and verify that the runner never invokes VPN control. Live integration with your Jira response format, repositories, and Claude configuration still needs a supervised first run.
