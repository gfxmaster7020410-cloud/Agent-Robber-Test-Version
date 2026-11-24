# Autonomous Git Workflow, Branch Management & PR Generation

## Overview

Agent Robber isolates each swarm task on an `agent/feat-*` branch, writes AST-aware conventional commits, and can open a GitHub pull request or GitLab merge request without pulling libgit2 into the 1.5 GB RAM budget.

Git porcelain uses the local `git` binary. The Rust indexer (`indexer/src/git_engine.rs`) classifies AST diffs (`feat` / `fix` / `refactor` / `test`) and names branches. Python (`robber/tools/git_manager.py`) owns checkout, stash, commit, and restore.

## Commands

```
/git branch          # active agent branches, stash count, porcelain
/git restore         # return to the original default branch and pop the stash
/pr create           # commit, push, synthesize PR body, open remote PR
/pr create --draft
```

## Swarm pipeline

1. **ArchitectWorker** — `create_feature_branch(task_id, slug)` stashes dirty work and checks out `agent/feat-[slug]`.
2. **CodeWorker / TestWorker** — mutations stay on that branch.
3. **TestWorker** (pass) — AST conventional commit. If `ROBBER_AUTO_PR=1`, push + PR. Otherwise TUI hint: `/pr create`.
4. **PRWriterWorker** — Markdown body: summary, AST symbols, tests, Playwright snapshots, breaking changes.

## Protection

Commits skip paths under `robber/`, `gateway/`, `indexer/`, `ui/`, `gui/`, and `data/ssot.db` unless `ROBBER_ALLOW_SELF_EDIT=1`.

Branch rows live in SQLite SSOT table `git_branches` (WAL). Decision history stays in `decisions_ledger`.

## Remote auth

- `gh pr create` when the GitHub CLI is installed
- else `GITHUB_TOKEN` / `GH_TOKEN` (GitHub REST v3)
- else `GITLAB_TOKEN` (GitLab API v4)

Labels: `automated-pr`, `agent-robber`, `needs-review`.
