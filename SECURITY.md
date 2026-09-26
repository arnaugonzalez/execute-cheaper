# Security

## What the executor can do

Executors run a coding agent **with permission prompts disabled** in the directory you point them
at: `--permission-mode bypassPermissions` for Claude Code, `--mode accept-edits` for agy, Codex's
`workspace-write` sandbox (or no sandbox with `"sandbox": "none"`), and whatever flags a `cli` preset
passes (the Copilot preset uses `--yolo`). A cheap model can run any shell command your user can. `execute-cheaper init` creates an isolated
Claude Code profile that denies reading SSH/cloud credentials, other agents' config and
execute-cheaper's own `.env`, and denies `git push` and `sudo` — but a deny-list is not a sandbox.

Recommended:

- Run on a dedicated git worktree or branch, never on a dirty tree you care about.
- For untrusted plans or repos, run execute-cheaper inside a container or VM.
- Keep API keys in `$XDG_CONFIG_HOME/execute-cheaper/.env` (created with mode 600) or the environment,
  and give each provider a spending limit (`max_budget_usd` and the provider's own dashboard).

## What execute-cheaper itself does

- It opens no network connections and sends no telemetry. Network traffic comes from the agent CLIs
  it launches, to the providers you configured.
- It reads its configuration only from `$XDG_CONFIG_HOME/execute-cheaper/`, never from the repository
  it works on, so a repository can't add hooks or commands to your setup. The `on_event` hook and
  the `cmd` of `cli` providers run as your user: treat that config like a shell script.
- Executors never get your terminal: their stdin is either empty or the prompt, and they run in
  their own session without a controlling terminal.

## How releases are published

Releases are built and uploaded by the `Release` workflow in this repository, triggered by a `v*`
tag. PyPI accepts them through Trusted Publishing (no long-lived upload token exists), the `pypi`
environment requires the maintainer's approval for every upload, and every GitHub Action is pinned
by commit SHA. The wheel is a single file, `execute_cheaper.py`, with no dependencies: you can read
all of it before installing.

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Security" tab → "Report a vulnerability")
rather than a public issue.
