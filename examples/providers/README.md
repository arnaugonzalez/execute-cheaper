# Provider presets

Each file is one entry for the `providers` object of `config.json`, using the generic `kind: "cli"`
adapter (see the README's configuration table). opencode and Cursor (`cursor-agent`) are built-in
presets now (`execute-cheaper init --chain opencode` or `cursor`). The coding-plan presets below use the same
opencode flags as the built-in one, which was run end to end with opencode 2.0.29 on 2026-10-18.
**The others have not been run**: the flags come
from each tool's `--help` or documentation on 2026-09-24, and model names change often. Try one,
then open an issue (template "provider preset") saying what worked, with the CLI version.

| File | Tool | Notes |
|---|---|---|
| `gemini-cli.json` | [Gemini CLI](https://github.com/google-gemini/gemini-cli) | Needs `GEMINI_API_KEY` (AI Studio). Personal Google-account login is no longer accepted by the CLI; Google points it to Antigravity, which is the built-in `agy` provider |
| `opencode-zai.json` | opencode + Z.ai GLM Coding Plan | Flags as the built-in `opencode` preset (`run --standalone --auto --format json`; opencode 2.x has no `--pure`); `opencode auth login` → Z.AI Coding Plan first. opencode reports $0 for plan models, so `prices` holds Z.ai's pay-as-you-go rates and the ledger shows the subscription-equivalent cost. Models not yet run with a real plan |
| `opencode-minimax.json` | opencode + MiniMax Coding Plan | As above with `minimax-coding-plan/*`. Models not yet run with a real plan |

To use one: copy the object into `config.json` under `providers`, add its name to `chain`, and run
`execute-cheaper status` to check the binary and the key are found.
