# Provider presets

Each file is one entry for the `providers` object of `config.json`, using the generic `kind: "cli"`
adapter (see the README's configuration table). The opencode presets were run end to end on 2026-10-02
(opencode 1.18.34, free model `opencode/mimo-v2.6-flash-free`). **The others have not been run**: the flags come
from each tool's `--help` or documentation on 2026-09-24, and model names change often. Try one,
then open an issue (template "provider preset") saying what worked, with the CLI version.

| File | Tool | Notes |
|---|---|---|
| `gemini-cli.json` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) | Needs `GEMINI_API_KEY` (AI Studio). Personal Google-account login is no longer accepted by the CLI; Google points it to Antigravity, which is the built-in `agy` provider |
| `cursor-agent.json` | [Cursor CLI](https://cursor.com/cli) | `cursor-agent -p` with `--force` so it can edit files headless. `auto` picks a model under your Cursor plan |
| `opencode.json` | [opencode](https://opencode.ai) | **Tested.** `opencode run --pure --auto --format json`: `--pure` skips your global opencode plugins, `--auto` lets it edit headless. opencode exits 0 on errors, so `fail_re` matches its `error` event. Tokens and cost come from its per-step events. Models are `provider/model`; `opencode models` lists what your setup has |
| `opencode-zai.json` | opencode + Z.ai GLM Coding Plan | Same flags; `opencode auth login` → Z.AI Coding Plan first. opencode reports $0 for plan models, so `prices` holds Z.ai's pay-as-you-go rates and the ledger shows the subscription-equivalent cost. Models not yet run with a real plan |
| `opencode-minimax.json` | opencode + MiniMax Coding Plan | As above with `minimax-coding-plan/*`. Models not yet run with a real plan |

To use one: copy the object into `config.json` under `providers`, add its name to `chain`, and run
`execute-cheaper status` to check the binary and the key are found.
