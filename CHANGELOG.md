# Changelog

## 0.1.0 — unreleased

First public release.

- README: social preview on top; new narrated demo in an "Example" section.
- Subscription CLIs first: Codex CLI (`codex exec`), GitHub Copilot CLI, Google Antigravity (`agy`)
  and your own Claude Code login. Any Anthropic Messages-compatible endpoint (Anthropic, OpenRouter,
  DeepSeek, Z.ai, Ollama/LM Studio/LiteLLM proxy) as configurable API fallback.
- Generic `cli` adapter: any headless coding CLI becomes an executor from config alone (argv
  template, text or JSON output, usage paths, failure/quota/auth regexes). Copilot is the tested
  preset; Gemini CLI ships untested in `examples/providers/`.
- `cli` adapter `output: "jsonl"` for event-stream CLIs (usage summed over every event) and `cost_field`
  for the cost a CLI reports itself; built-in `opencode` preset (tested with opencode 2.0.29, which dropped `--pure`: the preset uses
  `--standalone`), plus presets for the Z.ai and MiniMax coding plans in `examples/providers/`.
- Built-in `cursor` preset (Cursor CLI, `cursor-agent`), untested. `init` offers `opencode` and `cursor`
  as opt-in routes: detected and listed, never picked unless named.
- Server-side tool calls (`usage.server_tool_use`) priced per call via `tool_prices`, starting at
  `tool_prices_since`; the DeepSeek preset bills Search/Fetch at $0.00075 from 2026-11-01 (Beijing).
- Provider chain with cooldowns persisted across runs; `agy` watchdog for quota hangs.
- Independent read-only verifier (Claude Code or Codex) with a JSON Schema verdict and automatic repair rounds.
- Cost ledger (`usage`), per-run `report.md`, exit codes 0/1/2/3.
- In a terminal, `run` ends with a four-line summary (verdict, cost, files, report path); pipes, CI
  and agents still get the full report on stdout.
- Executors run without the user's terminal (own session; stdin carries only the prompt, or nothing),
  so an interactive CLI can't wait for input or draw over it.
- `claude` adapter sends the prompt on stdin, so a verifier prompt with a large diff no longer fails
  with "Argument list too long" (Linux caps one argv entry at 128 KiB).
- Claude Code plugin: this repo is its own marketplace; the `execute-cheaper` skill writes the plan,
  launches the run in the background and reads only the verdict.
- Release hardening: Trusted Publishing, maintainer approval for every PyPI upload, GitHub Actions
  pinned by commit SHA with least-privilege permissions, Dependabot for Actions.
- `init` detects the coding CLIs and API keys you have (key names only, never values) and asks which
  routes to use, in what order, and which verifier (default: one from another vendor). The config
  holds only those providers and `.env` only the keys they still need. No fixed chain: Antigravity
  and local servers are opt-in. Non-interactive: `init --chain a,b --verify x`, `--yes`; exits 1
  without writing when nothing usable is found; `--force` to replace a config.
- Agent setup: `init --detect --json`, `init ... --json` and `status --json` (versioned schema, key
  names and where they are set, never values); `AGENTS.md` and a `/execute-cheaper:setup` skill walk
  an agent through detect → ask the user → init → status without a key ever going through the chat.
- `status` names every chain or verifier provider that can't run yet (missing CLI or key).
- `--on-event` hook for notifications and integrations; XDG paths.
