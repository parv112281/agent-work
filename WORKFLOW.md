# Logseq Agent Workflow

This document describes the workflow implemented by the local scripts.

## Task selection

A task qualifies when its Logseq block is `TODO` or `LATER` and carries **every tag in the config's `required_tags`** (here `#agent` and `#client`). Identify the Jira ticket from the task or its children, and resolve the repository using an inline or standalone `repo::` property or the project-to-repository map. Implementation context comes from the task and its children, linked local Markdown documents, and fetched Jira details. Parent meeting notes are not collected.

For tickets involving multiple repositories, use comma-separated `repos::` for implementation repositories and `reference-repos::` for context-only repositories. Existing `repo::` remains valid. One agent handles the whole ticket; each implementation repository has its own branch and worktree, and each reference repository has a detached worktree.

Set `base:: epic/PROJ-5000` to start implementation repositories from an epic branch. Use `base-branches:: web-ui=epic/PROJ-5000, web-api=default` for per-repository overrides. References stay on their default branches unless named in `base-branches::`. Gathering explicitly fetches the selected remote branch and records its ref and exact commit. Missing requested branches fail preparation without falling back to another branch. Epic branches are left unchanged.

```text
- TODO Implement add 2 numbers function [[Projects/Example]] #PROJ-5000 #agent #client repo::my-service
  - Modify the calculator code to add the addition function.
  - The numbers should only be integers.
  - Look at [Details](../assets/addition-spec.md) or [[Integer calculator requirements]].
```

## Run flow

1. **Gather and prepare all selected tasks.** The user connects the VPN and launches the gather phase. Reserve the batch using `agent-run::`, snapshot linked Markdown context, and fetch Jira context and download its attachments using the Jira instance named in the config file. Save attachments locally for offline agent access and fail the task if downloads are missing or incomplete. Capture each repository's base commit, create worktrees for all listed repositories with branches for implementation repositories, and prepare implementation dependencies while internal services are accessible. Reserved tasks retain their `TODO` or `LATER` state until execution starts. Gathering exits without starting agents.
2. **Disconnect the VPN manually.** The user disconnects the VPN, then launches the execution command printed by gathering. Scripts never inspect or manage VPN state, including on exit or failure.
3. **Implement tasks sequentially.** Execution loads the prepared batch, marks the current task `DOING`, and runs the agent in its worktree to implement, validate, and commit the change locally.
4. **Record each handoff.** Mark completed work `WAITING` and add each repository's branch, worktree path and role, plus the implementation summary, validation results, and any unresolved concerns to Logseq.
5. **Continue to the next task immediately.** Do not wait for the user to review, push, or create a PR for the previous task.

If a task fails, record the reason, return it to `TODO`, and continue with the remaining tasks. Retrying a failed task must be explicit; it should not be automatically selected again.

If completing a ticket requires a reference repository to change, the agent must stop and report a structured `blocked` outcome naming that repository and the required change. Return the task to `TODO` with `agent-status:: blocked` and `agent-blocked::`, preserve partial work, and continue with the next ticket. Existing implementation commits do not make a blocked ticket ready for review. The agent cannot expand its own repository scope. A missing or invalid structured outcome fails the task rather than marking it `WAITING`.

## User handoff

The user reviews the implementation, pushes the branch, and creates the PR. The runner never pushes branches or creates PRs. Keep branches, worktrees, and run artifacts available for review.

```text
- WAITING Fix retry logic PROJ-2431 #agent #client
  agent-branch:: agent/PROJ-2431-retry-logic
  agent-worktree:: /path/to/worktree
  agent-result:: Added backoff with a maximum of 3 attempts.
  agent-validation:: mvn test passed.
	- **Agent summary**
		- **Changes**
			- retry-service: exponential backoff, capped at 3 attempts
		- **Tests**
			- `mvn test`: 42/42 passed
		- Details: /path/to/run/PROJ-2431/summary.md
```

## Initial scope

Run manually, with one agent task executing at a time. Multiple independent tasks may be processed in one run, each in a separate branch and worktree. Tasks using the same repository and base ref share a captured base commit; tasks may select different base branches for that repository. Defer dependent tasks until their prerequisite changes have been reviewed and integrated. Scheduling and parallel execution are outside the initial scope.

## Running and retrying

Configure paths, the Jira instance, and the agent's allowed test commands in `config.toml` (see `config.example.toml`). With the VPN connected, run `python3 run.py gather`. After manually disconnecting, run `python3 run.py execute <run-directory>` using the command printed by gathering. Artifacts are saved under `<work_root>/<date>/run-<time>-<pid>/<task-id>/`. Only one orchestrator phase may execute at a time.

Successful handoffs require a successful agent result, at least one local implementation commit overall, clean implementation worktrees on their assigned branches, and unchanged reference Git state. The agent is instructed to read reference repositories only; the runner verifies their Git state after execution. Validation details are reported by the agent and should be reviewed by the user.

To retry a failed or blocked task, review its retained work first, then remove its `agent-run::` and old `agent-result::`, `agent-branch::`, `agent-worktree::`, `agent-repositories::`, `agent-validation::`, `agent-status::`, and `agent-blocked::` properties, and the **Agent summary** child block. For a scope blocker, move the affected repository from `reference-repos::` to `repos::` only if you authorize its change, or revise the task to keep it unchanged. Leave the task `TODO` with its tags. The next run creates new branches and worktrees; it does not resume the old implementation.

Runs report each task through `handoff.sh` immediately after execution.
