# Security

## What the executor can do

Executors run a coding agent **with permission prompts disabled** (`--permission-mode
bypassPermissions` for Claude Code, `--mode accept-edits` for agy) in the directory you point them
at. A cheap model can run any shell command your user can. `cheap-exec init` creates an isolated
Claude Code profile that denies reading SSH/cloud credentials, other agents' config and
cheap-exec's own `.env`, and denies `git push` and `sudo` — but a deny-list is not a sandbox.

Recommended:

- Run on a dedicated git worktree or branch, never on a dirty tree you care about.
- For untrusted plans or repos, run cheap-exec inside a container or VM.
- Keep API keys in `$XDG_CONFIG_HOME/cheap-exec/.env` (created with mode 600) or the environment,
  and give each provider a spending limit (`max_budget_usd` and the provider's own dashboard).

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting ("Security" tab → "Report a vulnerability")
rather than a public issue.
