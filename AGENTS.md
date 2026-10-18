# Setting up execute-cheaper for a user (instructions for agents)

You are a coding agent (Claude Code, Codex, or anything with a shell) and the user wants to spend less
of their expensive model's quota by handing plans to cheaper executors. Set execute-cheaper up in this
conversation. The user picks the routes; you run the commands. Contributors: see CONTRIBUTING.md.

## 1. Install

```bash
command -v execute-cheaper || pipx install execute-cheaper   # or: uv tool install execute-cheaper
```

## 2. Detect (writes nothing)

```bash
execute-cheaper init --detect --json
```

Stable JSON:

| Field | Meaning |
|---|---|
| `schema` | `1`. A breaking change to these fields bumps it |
| `routes` | Every built-in route: `name`, `kind`, `family` (vendor), `billing`, `about`, `can_verify`, `opt_in`, `note`, `cli` (binary it needs), `key_env` (variable it needs, or `null`), `ready`, `missing` (what is missing, e.g. ``"no `copilot` CLI"``, `"no OPENROUTER_API_KEY"`) |
| `suggested` | `{"chain": [...], "verify": name or null}`: the ready routes in order, minus opt-in ones, and a verifier from another family |
| `clis` | binary name → found on PATH |
| `keys` | variable name → `"env"`, `".env"` or `null`. Never the value |
| `warnings` | Opt-in routes and why (Antigravity's terms, local servers) |
| `config_path`, `env_path`, `config_exists` | Where the config and the keys file live |

## 3. Ask the user, in the chat

Show the routes as a short list (ready / missing what), then ask:

1. Which executors to use, **in order** (the chain falls back to the next one on quota or error).
   Offer `suggested.chain` as the default.
2. Which verifier: a route with `can_verify: true`, ideally from another `family` than the executors.
   Offer `suggested.verify` as the default, or `none`.

`agy` (Antigravity) only goes in if the user names it: relay its `note` (Google's terms restrict
use with third-party tools) before you add it. Same for `ollama` (it needs a local server).

## 4. Write the config

```bash
execute-cheaper init --chain <a,b,...> --verify <name|none> --json
```

- Exit 0 and `{"ok": true, "written": true, "chain", "verify", "keys_to_fill", "warnings"}`.
- Running it again with the same choice returns `"written": false` and exit 0. A different choice on
  an existing config fails with exit 1 and asks for `--force`: ask the user before adding it.
- Exit 1 with `"errors"` when a route's CLI is missing, a name is unknown, or the verifier can't verify.

## 5. Keys: never in the chat

If `keys_to_fill` is not empty, **do not ask for the key and do not write it yourself**. Tell the user
the file (`env_path`) and the variable name, and that they can paste `NAME=value` there or export it.
Then check:

```bash
execute-cheaper status --json
```

`ready: true` means every route in the chain can run. `problems` lists routes that can't yet (missing
CLI or key); `providers[].key_source` says where each key was found, never its value.

## 6. Hand off

Tell the user the chain and the verifier, and that `execute-cheaper run --plan PLAN.md --cwd <repo>`
runs a plan (in Claude Code, the `execute-cheaper` skill does that part).
