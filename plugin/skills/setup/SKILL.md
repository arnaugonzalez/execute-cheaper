---
name: setup
description: Set up execute-cheaper from this chat - detect the user's coding CLIs and API keys, ask which executors to use, in what order, and which verifier, then write the config with flags and check it. Use when the user says "set up execute-cheaper", "configure execute-cheaper", "I want to spend less of my Claude quota", "use my ChatGPT/Copilot/Antigravity plan for the typing", or when `execute-cheaper status` says there is no config.
argument-hint: "[routes, e.g. codex,openrouter --verify claude-login]"
allowed-tools:
  - Bash
---

# execute-cheaper setup

The user chooses the routes; you run the commands. Never ask for an API key in the chat.

## 1. Install and detect

```bash
command -v execute-cheaper || pipx install execute-cheaper
execute-cheaper init --detect --json
```

The JSON (`"schema": 1`) has `routes[]` (`name`, `about`, `ready`, `missing`, `can_verify`,
`family`, `opt_in`, `note`), `suggested` (`chain`, `verify`), `keys` (names → `env`, `.env` or
null, never values), `env_path` and `warnings`.

## 2. Ask

If `$ARGUMENTS` already names the routes, use them. Otherwise show a short list (route: ready, or
what is missing) and ask in one message:

- which executors, in order (default: `suggested.chain`);
- which verifier (default: `suggested.verify`; it must have `can_verify: true`, ideally another
  `family` than the executors; `none` is allowed).

Add `agy` only if the user names it, and relay its `note` about Google's terms first. Same for
`ollama` (needs a local server).

## 3. Write and check

```bash
execute-cheaper init --chain <a,b> --verify <name|none> --json
execute-cheaper status --json
```

- `"written": false` with exit 0: the same config was already there. Exit 1 asking for `--force`:
  a different config exists; ask the user before re-running with `--force`.
- `keys_to_fill` not empty: tell the user the file (`env_path`) and the variable name to paste the
  key into themselves (`NAME=value`), then run `status --json` again once they say it is done.
- `status --json` → `ready: true` means every route in the chain can run; otherwise relay `problems`.

End with one line: the chain, the verifier, and that the `execute-cheaper` skill runs plans with it.
