# Installing execute-cheaper (for Cline and other AI agents)

execute-cheaper is a command-line tool, not an MCP server: there is nothing to add to an MCP settings
file. You install it, run `init` with the routes the user picks, and check `status`. The full flow,
with every JSON field explained, is in [AGENTS.md](AGENTS.md); this page is the short version.

## Requirements

- Python 3.11+ and git.
- At least one route: a coding CLI the user is logged into (`codex`, `copilot`, `claude`, `opencode`,
  `cursor-agent`, `agy`), an API key (`OPENROUTER_API_KEY`, `DEEPSEEK_API_KEY`, `ZAI_API_KEY`,
  `ANTHROPIC_API_KEY`), or a local server that speaks the Anthropic Messages API.

## Steps

1. Install, unless `command -v execute-cheaper` already finds it:

   ```bash
   pipx install execute-cheaper      # or: uv tool install execute-cheaper
   ```

2. Detect what this machine has. This writes nothing:

   ```bash
   execute-cheaper init --detect --json
   ```

3. Show the user the `routes` (ready, or what is missing) and ask, in the chat:
   - which executors to use, **in order** (default: `suggested.chain`);
   - which verifier (a route with `can_verify: true`, ideally another `family`; default:
     `suggested.verify`, or `none`).

   Routes with `opt_in: true` (`agy`, `opencode`, `cursor`, `ollama`) go in only if the user names
   them. Relay their `note` first.

4. Write the config with exactly what the user chose:

   ```bash
   execute-cheaper init --chain <a,b,...> --verify <name|none> --json
   ```

   Exit 1 with `errors` means a CLI is missing, a name is unknown or the verifier can't verify. If a
   config already exists with a different choice, ask the user before adding `--force`.

5. API keys: **never ask for a key in the chat and never write one yourself.** For each name in
   `keys_to_fill`, tell the user the file (`env_path`) and the variable, and let them paste
   `NAME=value` there or export it.

6. Check:

   ```bash
   execute-cheaper status --json
   ```

   `ready: true` means every route in the chain can run. Otherwise `problems` says what is missing.

## Using it

```bash
execute-cheaper run --plan PLAN.md --cwd path/to/repo
```

A plan is plain Markdown: tasks, out of scope, checks (see `examples/plans/`). Exit codes: `0` verified,
`1` config error, `2` executed but not verified, `3` verification pending (the verifier hit a quota).
Each run writes a `report.md`; `status` lists the last runs.

## Where things live

- Config: `$XDG_CONFIG_HOME/execute-cheaper/config.json` (default `~/.config/execute-cheaper/`), keys in
  `.env` next to it (mode 600).
- State, ledger and run reports: `$XDG_STATE_HOME/execute-cheaper/` (default `~/.local/state/`).
