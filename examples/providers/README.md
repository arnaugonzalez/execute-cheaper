# Provider presets (untested)

Each file is one entry for the `providers` object of `config.json`, using the generic `kind: "cli"`
adapter (see the README's configuration table). **The author has not run these**: the flags come
from each tool's `--help` or documentation on 2026-09-24, and model names change often. Try one,
then open an issue (template "provider preset") saying what worked, with the CLI version.

| File | Tool | Notes |
|---|---|---|
| `gemini-cli.json` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) | Needs `GEMINI_API_KEY` (AI Studio). Personal Google-account login is no longer accepted by the CLI; Google points it to Antigravity, which is the built-in `agy` provider |
| `cursor-agent.json` | [Cursor CLI](https://cursor.com/cli) | `cursor-agent -p` with `--force` so it can edit files headless. `auto` picks a model under your Cursor plan |
| `opencode.json` | [opencode](https://opencode.ai) | `opencode run`. Models are `provider/model`; `opencode models` lists what your setup has |

To use one: copy the object into `config.json` under `providers`, add its name to `chain`, and run
`execute-cheaper status` to check the binary and the key are found.
