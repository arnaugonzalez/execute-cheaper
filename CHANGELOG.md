# Changelog

## 0.1.0 — unreleased

First public release.

- Subscription CLIs first: Codex CLI (`codex exec`), GitHub Copilot CLI, Google Antigravity (`agy`)
  and your own Claude Code login. Any Anthropic Messages-compatible endpoint (Anthropic, OpenRouter,
  DeepSeek, Z.ai, Ollama/LM Studio/LiteLLM proxy) as configurable API fallback.
- Generic `cli` adapter: any headless coding CLI becomes an executor from config alone (argv
  template, text or JSON output, usage paths, failure/quota/auth regexes). Copilot is the tested
  preset; Gemini CLI, `cursor-agent` and opencode ship untested in `examples/providers/`.
- Provider chain with cooldowns persisted across runs; `agy` watchdog for quota hangs.
- Independent read-only verifier (Claude Code or Codex) with a JSON Schema verdict and automatic repair rounds.
- Cost ledger (`usage`), per-run `report.md`, exit codes 0/1/2/3.
- `--on-event` hook for notifications and integrations; `init` command; XDG paths.
