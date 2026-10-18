---
name: execute-cheaper
description: Hand a written implementation plan to cheaper executors (Codex, GitHub Copilot, Antigravity, cheap APIs or local models) through the execute-cheaper CLI, then read an independent read-only verdict, instead of typing the code in this session. Use when the user says "execute cheaper", "run this plan cheaply", "offload this", "hand this off", "save my Claude usage/limits", "let Codex/Copilot implement it", or when a plan is ready and the remaining work is mechanical.
argument-hint: "[path/to/PLAN.md] [--tier pro] [--verify none]"
allowed-tools:
  - Bash
  - Read
  - Write
---

# execute-cheaper

This session plans; execute-cheaper executes and verifies in other processes. Your job is to write
the plan, launch one command, and read one short report. Do not implement the plan yourself and do
not spawn subagents: the whole point is that the typing and the checking happen outside this session.

## 1. Make sure the tool is there

```bash
command -v execute-cheaper || echo missing
execute-cheaper status
```

- Missing: tell the user to run `pipx install execute-cheaper` (or `uv tool install execute-cheaper`).
  For a one-off run, `uvx execute-cheaper ...` works without installing.
- `status` says there is no config: follow the `setup` skill (`init --detect --json`, ask the user
  which routes and verifier, `init --chain ... --verify ... --json`). Never ask for a key in the chat.
- A provider marked NOT FOUND or MISSING is skipped at run time; that is fine as long as one is ready.

## 2. Write the plan as a contract

If `$ARGUMENTS` has a path, use that plan. Otherwise write one in the target repo (for example
`PLAN.md`, or `.planning/<slug>.md` if the repo already keeps plans) with this shape:

```markdown
# <title>
Goal: one or two sentences.
## Tasks
1. <file>: exactly what changes, and why.
2. ...
## Out of scope
Files, routes or behaviour that must not change.
## Checks
- <exact command> → expected result (tests, lint, build)
```

Write it so a fast, cheap model can follow it without making decisions: concrete paths, no "improve
X". Use `--tier pro` only when a task needs real design judgement or the plan touches many files.

## 3. Launch it in the background

The target must be a git repository (the verifier reviews `git diff`). Prefer a clean tree or a
dedicated worktree: executors run with permission prompts off.

```bash
execute-cheaper run --plan <plan.md> --cwd <repo> [--tier pro] [--verify none]
```

Run it with `run_in_background`, and do not edit those files while it runs.

## 4. Read only the report

When it finishes, read the report printed at the end of the output (also saved as `report.md`;
the path is on the `Artifacts:` line). Read raw executor logs only if the report says something
failed. Exit codes:

| Code | Meaning | What to do |
|---|---|---|
| 0 | Verified OK | Summarise files changed and checks run for the user |
| 2 | Not verified, or no executor available | Show the verifier's findings; for "no executor", show what `execute-cheaper status` says is missing |
| 3 | Executed, verification pending (verifier out of quota) | Later: `execute-cheaper verify --run <id>` |
| 1 | Config error | Show the message; it names the field to fix |

After two failed repair rounds, either fix a tiny finding yourself (a few lines) or rewrite the plan
more concretely and launch again. If no executor is available, tell the user; do not fall back to
implementing it in this session unless they ask.
