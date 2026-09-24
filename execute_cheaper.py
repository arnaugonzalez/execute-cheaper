#!/usr/bin/env python3
"""execute-cheaper: plan with an expensive model, execute with cheap ones, verify independently.

A planner (you, or a frontier model) writes a Markdown plan. execute-cheaper hands it to a headless
coding agent running on the cheapest available provider, falls back along a provider chain when
one is out of quota, then asks a separate read-only verifier to check the git diff against the
plan and return a typed verdict. Failed verification triggers a repair round.

Executors and the verifier are coding-agent CLIs:
  kind "claude": Claude Code (`claude -p`) pointed at any Anthropic Messages-compatible endpoint
                 (Anthropic, DeepSeek, Z.ai, OpenRouter, a local proxy...) or at your own login.
  kind "agy":    Google Antigravity CLI (optional; check its terms before scripting it).

Standard library only. Config in $XDG_CONFIG_HOME/execute-cheaper, state in $XDG_STATE_HOME/execute-cheaper.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

__version__ = "0.1.0"

HOME = Path.home()


def _xdg(var: str, fallback: Path) -> Path:
    return Path(os.environ.get(var) or fallback)


CONFIG_DIR = Path(os.environ.get("EXECUTE_CHEAPER_CONFIG") or _xdg("XDG_CONFIG_HOME", HOME / ".config") / "execute-cheaper")
STATE_DIR = Path(os.environ.get("EXECUTE_CHEAPER_STATE") or _xdg("XDG_STATE_HOME", HOME / ".local" / "state") / "execute-cheaper")
CONFIG_PATH = CONFIG_DIR / "config.json"
ENV_PATH = CONFIG_DIR / ".env"
STATE_PATH = STATE_DIR / "state.json"
RUNS_DIR = STATE_DIR / "runs"
LEDGER_PATH = STATE_DIR / "ledger.jsonl"
PROFILE_DIR = STATE_DIR / "claude-profile"

KINDS = ("claude", "codex", "agy")
TIERS = ("flash", "pro")

DEFAULT_CONFIG: dict = {
    "chain": [],
    "verify": {"provider": None, "tier": "pro", "model": None, "timeout_s": 1800},
    "max_rounds": 2,
    "on_event": None,
    "providers": {},
}

# Written by `execute-cheaper init` and kept in sync with config.example.json (a test checks it).
# Prices: USD per 1M tokens [input, cached input, output(, cache write)], as listed by each provider on prices_as_of.
# Cache writes default to the input price; Anthropic models charge 1.25x input for them.
EXAMPLE_CONFIG: dict = {
    "chain": ["codex", "agy", "openrouter"],
    "verify": {"provider": "claude-login", "tier": "pro", "timeout_s": 1800},
    "max_rounds": 2,
    "on_event": None,
    "prices_as_of": "2026-09-24",
    "providers": {
        "codex": {
            "kind": "codex",
            "models": {"flash": "gpt-5.6-luna", "pro": "gpt-5.6-terra"},
            "timeout_s": 3600,
            "billing": "subscription",
        },
        "agy": {
            "kind": "agy",
            "models": {"flash": "gemini-3.8-flash-high", "pro": "gemini-3.1-pro-high"},
            "timeout_s": 3600,
            "inactivity_s": 600,
            "quota_hits_to_abort": 2,
            "billing": "subscription",
        },
        "openrouter": {
            "kind": "claude",
            "base_url": "https://openrouter.ai/api",
            "key_env": "OPENROUTER_API_KEY",
            "env": {"ANTHROPIC_API_KEY": "$KEY"},
            "models": {"flash": "deepseek/deepseek-v4.1-flash", "pro": "z-ai/glm-5.3"},
            "max_budget_usd": 2.0,
            "timeout_s": 3600,
            "billing": "paid",
            "prices": {
                "deepseek/deepseek-v4.1-flash": [0.14, 0.0042, 0.42],
                "z-ai/glm-5.3": [1.40, 0.26, 4.40],
                "anthropic/claude-sonnet-5": [2.0, 0.2, 10.0, 2.5],
            },
        },
        "deepseek": {
            "kind": "claude",
            "base_url": "https://api.deepseek.com/anthropic",
            "key_env": "DEEPSEEK_API_KEY",
            "models": {"flash": "deepseek-flash", "pro": "deepseek-v4-pro"},
            "max_budget_usd": 2.0,
            "timeout_s": 3600,
            "billing": "paid",
            "prices": {"deepseek-flash": [0.15, 0.003, 0.60], "deepseek-v4-pro": [0.66, 0.022, 1.98]},
            "peak": {"multiplier": 2.0, "utc_hours": [[1, 4], [6, 10]], "weekdays_only": True},
        },
        "zai": {
            "kind": "claude",
            "base_url": "https://api.z.ai/api/anthropic",
            "key_env": "ZAI_API_KEY",
            "models": {"flash": "glm-5.3-flash", "pro": "glm-5.3"},
            "max_budget_usd": 2.0,
            "timeout_s": 3600,
            "billing": "paid",
            "prices": {"glm-5.3-flash": [0.15, 0.03, 0.50], "glm-5.3": [1.40, 0.26, 4.40]},
        },
        "anthropic": {
            "kind": "claude",
            "key_env": "ANTHROPIC_API_KEY",
            "models": {"flash": "haiku", "pro": "sonnet"},
            "timeout_s": 3600,
            "billing": "paid",
        },
        "claude-login": {
            "kind": "claude",
            "use_login": True,
            "models": {"flash": "haiku", "pro": "sonnet"},
            "timeout_s": 3600,
            "billing": "subscription",
        },
        "ollama": {
            "kind": "claude",
            "base_url": "http://localhost:11434",
            "env": {"ANTHROPIC_AUTH_TOKEN": "ollama"},
            "models": {"flash": "qwen3-coder", "pro": "qwen3-coder"},
            "timeout_s": 3600,
            "billing": "local",
        },
    },
}

AGY_QUOTA_RE = re.compile(r"RESOURCE_EXHAUSTED \(code 429\)[^\n]*?Resets in ([0-9hms]+)", re.I)
CODEX_QUOTA_RE = re.compile(r"usage limit|rate limit|quota|429|too many requests", re.I)
CODEX_AUTH_RE = re.compile(r"sign in again|log ?in|401 unauthorized|not logged in", re.I)
CLAUDE_QUOTA_RE = re.compile(r"usage limit|rate limit|out of (?:extra )?usage|quota|resets? (?:at|in) ([^\n\"]+)|429", re.I)

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["ok", "incomplete", "out_of_scope", "regression"]},
        "missing": {"type": "array", "items": {"type": "string"}},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
        "regressions": {"type": "array", "items": {"type": "string"}},
        "checks_run": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
    },
    "required": ["verdict", "missing", "out_of_scope", "regressions", "checks_run", "summary"],
    "additionalProperties": False,
}

EXIT_OK, EXIT_CONFIG, EXIT_NOT_VERIFIED, EXIT_VERIFY_PENDING = 0, 1, 2, 3


class ConfigError(Exception):
    pass


# --- utilities --------------------------------------------------------------

def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    if CONFIG_PATH.exists():
        try:
            user = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise ConfigError(f"{CONFIG_PATH}: invalid JSON ({e})") from e
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def validate_config(cfg: dict) -> list[str]:
    """Human-readable problems that would make a run fail. Empty list = runnable."""
    errs = []
    providers = cfg.get("providers") or {}
    if not cfg.get("chain"):
        errs.append(f"'chain' is empty: list at least one provider in {CONFIG_PATH} (run `execute-cheaper init`)")
    for name in cfg.get("chain") or []:
        if name not in providers:
            errs.append(f"chain: unknown provider '{name}' (defined: {', '.join(providers) or 'none'})")
    vp = (cfg.get("verify") or {}).get("provider")
    if vp and vp not in providers:
        errs.append(f"verify.provider: unknown provider '{vp}'")
    if vp and providers.get(vp, {}).get("kind") == "agy":
        errs.append("verify.provider: the verifier must be a 'claude' or 'codex' provider (it needs a JSON schema)")
    for name, p in providers.items():
        if p.get("kind") not in KINDS:
            errs.append(f"providers.{name}.kind must be one of {KINDS}, got {p.get('kind')!r}")
        models = p.get("models") or {}
        for tier in TIERS:
            if not models.get(tier):
                errs.append(f"providers.{name}.models.{tier} is missing")
        if p.get("kind") == "claude" and not (p.get("use_login") or p.get("key_env") or p.get("base_url")):
            errs.append(f"providers.{name}: set 'key_env' (API key variable), 'base_url' or 'use_login': true")
    return errs


def load_env() -> dict:
    """KEY=VALUE lines from $CONFIG_DIR/.env (chmod 600). Never exported to the parent shell."""
    env = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def provider_key(pcfg: dict, secrets: dict) -> str | None:
    k = pcfg.get("key_env")
    return (secrets.get(k) or os.environ.get(k)) if k else None


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"cooldowns": {}}


def save_state(state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
    tmp.replace(STATE_PATH)


def set_cooldown(provider: str, seconds: int, reason: str) -> None:
    state = load_state()
    state["cooldowns"][provider] = {"until": iso(now() + dt.timedelta(seconds=seconds)), "reason": reason}
    save_state(state)


def cooldown_left(provider: str) -> int:
    cd = load_state()["cooldowns"].get(provider)
    if not cd:
        return 0
    until = dt.datetime.strptime(cd["until"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.UTC)
    return max(0, int((until - now()).total_seconds()))


def parse_reset(s: str) -> int:
    """'3h6m37s' -> seconds (+1 min margin). Unparseable -> 1 h."""
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", s.strip())
    if not m or not any(m.groups()):
        return 3600
    h, mi, se = (int(x or 0) for x in m.groups())
    return h * 3600 + mi * 60 + se + 60


def in_peak(peak: dict | None, t: dt.datetime) -> bool:
    if not peak:
        return False
    if peak.get("weekdays_only") and t.weekday() >= 5:
        return False
    return any(lo <= t.hour < hi for lo, hi in peak.get("utc_hours", []))


def norm_usage(usage: dict | None) -> dict:
    """Anthropic-style usage (claude -p) and Gemini-style usage (agy) -> 3 counters."""
    u = usage or {}
    return {
        "input": int(u.get("input_tokens", 0) or 0) + int(u.get("cache_creation_input_tokens", 0) or 0),
        "cached": int(u.get("cache_read_input_tokens", 0) or 0) + int(u.get("cache_read_tokens", 0) or 0),
        "output": int(u.get("output_tokens", 0) or 0) + int(u.get("thinking_tokens", 0) or 0),
    }


def estimate_cost(pcfg: dict, model: str, usage: dict | None, when: dt.datetime | None = None) -> float | None:
    """USD from the provider's price table; None when the model has no listed price."""
    table = (pcfg.get("prices") or {}).get(model)
    if not table:
        return None
    pin, pcached, pout = table[:3]
    pwrite = table[3] if len(table) > 3 else pin
    mult = float(pcfg["peak"].get("multiplier", 1.0)) if in_peak(pcfg.get("peak"), when or now()) else 1.0
    n = norm_usage(usage)
    written = int((usage or {}).get("cache_creation_input_tokens", 0) or 0)
    return mult * (n["input"] * pin + written * (pwrite - pin) + n["cached"] * pcached + n["output"] * pout) / 1e6


def attempt_cost(pcfg: dict, model: str, res: dict) -> float | None:
    """Priced from config when possible; Claude Code's own figure is only right against Anthropic itself."""
    if pcfg.get("billing") == "local":
        return 0.0
    est = estimate_cost(pcfg, model, res.get("usage"))
    if est is not None:
        return est
    if pcfg.get("kind") == "claude" and not pcfg.get("base_url"):
        return res.get("cost_usd")
    return None


def ledger_append(entry: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def ledger_read(days: int) -> list[dict]:
    if not LEDGER_PATH.exists():
        return []
    since = iso(now() - dt.timedelta(days=days))
    out = []
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("ts", "") >= since:
            out.append(e)
    return out


def emit(hook: str | None, event: str, **data) -> None:
    """Run the user's on_event command with the event as JSON on stdin. Failures never stop a run."""
    if not hook:
        return
    payload = json.dumps({"event": event, "ts": iso(now()), **data}, ensure_ascii=False)
    try:
        r = subprocess.run(hook, shell=True, input=payload, text=True, capture_output=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        log(f"on_event hook timed out on '{event}'")
        return
    if r.returncode != 0:
        log(f"on_event hook exited {r.returncode} on '{event}': {(r.stderr or '').strip()[:200]}")


def git(cwd: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    return r.stdout


def is_git_repo(cwd: Path) -> bool:
    return git(cwd, "rev-parse", "--is-inside-work-tree").strip() == "true"


# --- prompts ----------------------------------------------------------------

EXEC_PREAMBLE = """You are the EXECUTOR of a plan written by a senior planner. The plan is the contract.

Rules:
1. Implement EXACTLY the plan below, task by task, in order. Do not redesign, do not add features,
   do not refactor anything the plan does not name.
2. Touch only the files the plan names (plus tests it asks for). If a task is impossible as written,
   skip it and say why in your final report — never improvise a different change.
3. Run the checks/commands the plan lists (tests, lint, build) before finishing. If they fail because
   of your change, fix your change; if they fail for unrelated reasons, report it.
4. Do NOT commit or push unless the plan says so explicitly.
5. Keep the final report under 25 lines: files changed (path — what), checks run (command → result),
   tasks skipped (why). No prose beyond that.
"""

REPAIR_PREAMBLE = """A reviewer compared your previous implementation against the plan and found problems.
Fix ONLY what is listed under "Reviewer findings" — nothing else. Re-run the plan's checks. Same
report format as before (files changed, checks run, still-unresolved items)."""

VERIFY_PROMPT = """You are an independent REVIEWER. A cheap executor implemented the plan below in this repo.
You must decide whether the plan was implemented faithfully. You may run the checks the plan lists
(tests/lint/build) and read any file. You must NOT edit files.

Report strictly in the JSON schema you were given:
- verdict: "ok" only if every plan task is implemented, nothing outside the plan's scope changed, and
  the plan's checks pass. "incomplete" if tasks are missing/partial, "out_of_scope" if unrelated
  files/behaviour changed, "regression" if checks fail or behaviour broke.
- missing / out_of_scope / regressions: one concrete item per line (file:line or command output).
- checks_run: the exact commands you executed and their outcome.
- summary: ≤ 3 sentences.
"""

EXECUTOR_PROFILE_CLAUDE_MD = """# execute-cheaper executor profile
You execute plans. The plan you receive is the contract: implement it as written, do not redesign,
do not touch files the plan does not name, do not commit or push unless the plan says so.
Answer in the report format the prompt asks for (≤ 25 lines). Never read or write other agents'
configuration, SSH keys or cloud credentials.
"""


def executor_profile_settings() -> dict:
    home = "~"
    secret_dirs = [f"{home}/.ssh/**", f"{home}/.aws/**", f"{home}/.gnupg/**", f"{home}/.config/gcloud/**",
                   f"{home}/.claude/**", f"{home}/.gemini/**", f"{CONFIG_DIR}/**"]
    deny = [f"{op}({p})" for op in ("Read", "Edit", "Write") for p in secret_dirs]
    deny += ["Bash(git push*)", "Bash(sudo *)"]
    return {
        "permissions": {"deny": deny},
        "env": {"DISABLE_TELEMETRY": "1", "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
        "includeCoAuthoredBy": False,
    }


def build_exec_prompt(plan: str, findings: dict | None) -> str:
    if findings:
        return (REPAIR_PREAMBLE + "\n\n## Reviewer findings\n" +
                json.dumps({k: findings[k] for k in ("missing", "out_of_scope", "regressions", "summary")},
                           indent=1, ensure_ascii=False) + "\n\n## Plan (contract)\n" + plan)
    return EXEC_PREAMBLE + "\n## Plan (contract)\n" + plan


def build_verify_prompt(plan: str, diff: str, untracked: str) -> str:
    return (VERIFY_PROMPT + "\n## Plan\n" + plan + "\n\n## git diff (executor's changes)\n```diff\n" +
            diff[:120_000] + "\n```\n\n## Untracked files\n" + (untracked or "(none)"))


# --- adapter: agy -----------------------------------------------------------

def run_agy(prompt: str, cwd: Path, run_dir: Path, model: str, pcfg: dict) -> dict:
    """Runs agy in print+JSON mode with its own log file and a watchdog that kills it when:
    - the log accumulates N '429 ... quota reached' lines (agy retries forever and hangs),
    - the log stops growing for inactivity_s,
    - timeout_s is exceeded.
    Returns {"ok", "text", "reason", "reset_s", "usage"}."""
    timeout_s = pcfg.get("timeout_s", 3600)
    log_path = run_dir / "agy.log"
    (run_dir / "agy-prompt.md").write_text(prompt, encoding="utf-8")
    cmd = [pcfg.get("bin", "agy"), "--print", prompt, "--output-format", "json",
           "--mode", "accept-edits", "--model", model, "--log-file", str(log_path),
           "--print-timeout", f"{timeout_s}s"]
    log(f"agy > {model} (log: {log_path.name})")
    try:
        # Own process group: on abort, agy's children must die too, or an orphan keeps the
        # pipe open and communicate() never returns.
        proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
    except FileNotFoundError:
        return {"ok": False, "text": "", "reason": f"binary not found: {cmd[0]}"}
    verdict = {"reason": None, "reset_s": 0}
    stop = threading.Event()

    def watchdog() -> None:
        last_size, last_change = -1, time.monotonic()
        started = time.monotonic()
        while not stop.wait(2):
            if time.monotonic() - started > timeout_s:
                verdict["reason"] = "timeout"
            elif log_path.exists():
                size = log_path.stat().st_size
                if size != last_size:
                    last_size, last_change = size, time.monotonic()
                    hits = AGY_QUOTA_RE.findall(log_path.read_text(encoding="utf-8", errors="replace"))
                    if len(hits) >= pcfg.get("quota_hits_to_abort", 2):
                        verdict["reason"] = "quota"
                        verdict["reset_s"] = parse_reset(hits[-1])
                elif time.monotonic() - last_change > pcfg.get("inactivity_s", 600):
                    verdict["reason"] = "inactivity"
            if verdict["reason"]:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
                return

    t = threading.Thread(target=watchdog, daemon=True)
    t.start()
    out, err = proc.communicate()
    stop.set()
    t.join(timeout=1)
    (run_dir / "agy-stdout.json").write_text(out or "", encoding="utf-8")
    if err:
        (run_dir / "agy-stderr.txt").write_text(err, encoding="utf-8")
    if verdict["reason"]:
        return {"ok": False, "text": "", "reason": verdict["reason"], "reset_s": verdict["reset_s"]}
    data = {}
    for line in reversed((out or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                data = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    ok = proc.returncode == 0 and data.get("status", "SUCCESS") == "SUCCESS"
    m = AGY_QUOTA_RE.search((out or "") + (err or ""))
    if not ok and m:
        return {"ok": False, "text": "", "reason": "quota", "reset_s": parse_reset(m.group(1))}
    return {"ok": ok, "text": data.get("response", out or ""),
            "reason": None if ok else f"status {data.get('status', proc.returncode)}: {str(data.get('error', ''))[:200]}",
            "usage": data.get("usage", {})}


# --- adapter: claude (Claude Code headless) ---------------------------------

def claude_env(pcfg: dict, key: str | None, model: str) -> tuple[dict, str | None]:
    """Environment for `claude -p` against this provider, and the CLAUDE_CONFIG_DIR to use
    (None = the user's own Claude Code login/profile)."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANTHROPIC_") and k != "CLAUDE_CODE_OAUTH_TOKEN"}
    env.update({"DISABLE_TELEMETRY": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
    if pcfg.get("use_login"):
        return env, pcfg.get("config_dir")
    if pcfg.get("base_url"):
        env.update({
            "ANTHROPIC_BASE_URL": pcfg["base_url"],
            "ANTHROPIC_AUTH_TOKEN": key or "",
            # Claude Code picks internal helper models by family name; route them all to this provider.
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": pcfg["models"]["pro"],
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": pcfg["models"]["flash"],
            "API_TIMEOUT_MS": "3000000",
        })
    else:
        env["ANTHROPIC_API_KEY"] = key or ""
    for k, v in (pcfg.get("env") or {}).items():
        env[k] = str(v).replace("$KEY", key or "")
    return env, pcfg.get("config_dir") or str(PROFILE_DIR)


def run_claude(prompt: str, cwd: Path, run_dir: Path, label: str, *, model: str, env: dict,
               config_dir: str | None, timeout_s: int, disallowed: list[str] | None = None,
               json_schema: dict | None = None, max_budget: float | None = None,
               setting_sources: str = "user", bin_name: str = "claude") -> dict:
    if config_dir:
        env = dict(env, CLAUDE_CONFIG_DIR=config_dir)
    # No --bare: bare mode ignores subscription logins and trims the executor's tools. Instead:
    # no MCP servers, no slash commands, and only the settings sources we choose.
    cmd = [bin_name, "-p", prompt, "--model", model, "--output-format", "json",
           "--permission-mode", "bypassPermissions", "--no-session-persistence",
           "--strict-mcp-config", "--disable-slash-commands", "--setting-sources", setting_sources]
    if disallowed:
        cmd += ["--disallowedTools", *disallowed]
    if json_schema:
        cmd += ["--json-schema", json.dumps(json_schema)]
    if max_budget:
        cmd += ["--max-budget-usd", str(max_budget)]
    log(f"{label} > claude -p --model {model}")
    try:
        proc = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout_s, check=False)
    except FileNotFoundError:
        return {"ok": False, "text": "", "reason": f"binary not found: {bin_name}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "reason": "timeout"}
    (run_dir / f"{label}-stdout.json").write_text(proc.stdout or "", encoding="utf-8")
    if proc.stderr:
        (run_dir / f"{label}-stderr.txt").write_text(proc.stderr, encoding="utf-8")
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    text = data.get("result", "")
    is_err = proc.returncode != 0 or bool(data.get("is_error"))
    if is_err and CLAUDE_QUOTA_RE.search((proc.stdout or "") + (proc.stderr or "")):
        return {"ok": False, "text": text, "reason": "quota", "reset_s": 3600}
    if not is_err and not data:
        is_err, text = True, ""
        reason = f"unparseable output: {(proc.stdout or '')[:200]!r}"
    else:
        reason = None if not is_err else f"exit {proc.returncode}: {(text or proc.stderr or '')[:300]}"
    return {"ok": not is_err, "text": text, "structured": data.get("structured_output"), "reason": reason,
            "cost_usd": data.get("total_cost_usd"), "usage": data.get("usage")}


# --- adapter: codex (OpenAI Codex CLI headless) -----------------------------

def run_codex(prompt: str, cwd: Path, run_dir: Path, label: str, *, model: str, pcfg: dict, timeout_s: int,
              read_only: bool = False, json_schema: dict | None = None) -> dict:
    """`codex exec --json`: uses the user's own Codex login (ChatGPT plan) or whatever Codex is set up with.
    The prompt goes through stdin, so large diffs don't hit the argv size limit.
    pcfg "sandbox": "on" (default: Codex's own sandbox) or "none", for hosts where it can't start."""
    last = run_dir / f"{label}-last.txt"
    if pcfg.get("sandbox", "on") == "none":
        sandbox = ["--dangerously-bypass-approvals-and-sandbox"]
    else:
        sandbox = ["--sandbox", "read-only" if read_only else "workspace-write"]
    cmd = [pcfg.get("bin", "codex"), "exec", "--json", "--ephemeral", "--skip-git-repo-check",
           "--ignore-user-config", "--ignore-rules", "-m", model, "-o", str(last), *sandbox, *pcfg.get("args", [])]
    if json_schema:
        schema_path = run_dir / f"{label}-schema.json"
        schema_path.write_text(json.dumps(json_schema), encoding="utf-8")
        cmd += ["--output-schema", str(schema_path)]
    cmd.append("-")
    log(f"{label} > codex exec -m {model}")
    try:
        proc = subprocess.run(cmd, cwd=cwd, input=prompt, capture_output=True, text=True, timeout=timeout_s,
                              check=False)
    except FileNotFoundError:
        return {"ok": False, "text": "", "reason": f"binary not found: {cmd[0]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "text": "", "reason": "timeout"}
    (run_dir / f"{label}-events.jsonl").write_text(proc.stdout or "", encoding="utf-8")
    if proc.stderr:
        (run_dir / f"{label}-stderr.txt").write_text(proc.stderr, encoding="utf-8")
    text, error, completed = "", None, False
    edits = {"completed": 0, "failed": 0}
    usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0}
    for line in (proc.stdout or "").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        kind, item = ev.get("type"), ev.get("item") or {}
        if kind == "item.completed" and item.get("type") == "agent_message":
            text = item.get("text", "")
        elif kind == "item.completed" and item.get("type") == "file_change":
            edits["failed" if item.get("status") == "failed" else "completed"] += 1
        elif kind == "turn.completed":
            completed = True
            u = ev.get("usage") or {}
            cached = int(u.get("cached_input_tokens", 0) or 0)
            # OpenAI counts cached tokens inside input_tokens; split them like Anthropic does.
            usage["input_tokens"] += int(u.get("input_tokens", 0) or 0) - cached
            usage["cache_read_input_tokens"] += cached
            usage["output_tokens"] += int(u.get("output_tokens", 0) or 0)
        elif kind == "turn.failed":
            error = (ev.get("error") or {}).get("message", "turn failed")
        elif kind == "error":
            error = ev.get("message", "error")
    if last.exists() and last.read_text(encoding="utf-8").strip():
        text = last.read_text(encoding="utf-8")
    ok = proc.returncode == 0 and completed and not error
    if not ok:
        msg = error or (proc.stderr or "")[-300:]
        if CODEX_AUTH_RE.search(msg):
            return {"ok": False, "text": "", "reason": f"not logged in: run `{cmd[0]} login` ({msg[:120]})"}
        if CODEX_QUOTA_RE.search(msg):
            return {"ok": False, "text": "", "reason": "quota", "reset_s": 3600}
        return {"ok": False, "text": text, "reason": f"exit {proc.returncode}: {msg[:300]}", "usage": usage}
    if edits["failed"] and not edits["completed"]:
        return {"ok": False, "text": text, "usage": usage,
                "reason": "codex sandbox rejected every file edit (on Ubuntu, AppArmor blocks the user namespaces "
                          "it needs): fix the host or set \"sandbox\": \"none\" for this provider"}
    structured = None
    if json_schema:
        try:
            structured = json.loads(text)
        except json.JSONDecodeError:
            return {"ok": False, "text": text, "reason": f"unparseable verdict: {text[:200]!r}", "usage": usage}
    return {"ok": True, "text": text, "structured": structured, "reason": None, "usage": usage}


def run_executor(name: str, pcfg: dict, prompt: str, cwd: Path, run_dir: Path, tier: str, secrets: dict) -> dict:
    model = pcfg["models"][tier]
    if pcfg["kind"] == "agy":
        return run_agy(prompt, cwd, run_dir, model, pcfg)
    if pcfg["kind"] == "codex":
        return run_codex(prompt, cwd, run_dir, name, model=model, pcfg=pcfg, timeout_s=pcfg.get("timeout_s", 3600))
    key = provider_key(pcfg, secrets)
    if pcfg.get("key_env") and not key:
        return {"ok": False, "text": "", "reason": f"missing key {pcfg['key_env']} (env or {ENV_PATH})"}
    env, config_dir = claude_env(pcfg, key, model)
    return run_claude(prompt, cwd, run_dir, name, model=model, env=env, config_dir=config_dir,
                      timeout_s=pcfg.get("timeout_s", 3600), max_budget=pcfg.get("max_budget_usd"),
                      disallowed=["Agent", "Task"], setting_sources="user", bin_name=pcfg.get("bin", "claude"))


def verifier_model(cfg: dict) -> str:
    v = cfg["verify"]
    return v.get("model") or cfg["providers"][v["provider"]]["models"][v.get("tier") or "pro"]


def run_verifier(plan: str, cwd: Path, run_dir: Path, cfg: dict, secrets: dict, label: str = "verify") -> dict:
    diff = git(cwd, "diff") + git(cwd, "diff", "--cached")
    untracked = git(cwd, "ls-files", "--others", "--exclude-standard")
    if not diff.strip() and not untracked.strip():
        return {"ok": True, "structured": {"verdict": "incomplete", "missing": ["the repo has no changes"],
                                           "out_of_scope": [], "regressions": [], "checks_run": [],
                                           "summary": "The executor left no diff."}}
    name = cfg["verify"]["provider"]
    pcfg = cfg["providers"][name]
    key = provider_key(pcfg, secrets)
    if pcfg.get("key_env") and not key:
        return {"ok": False, "reason": f"missing key {pcfg['key_env']} for verifier '{name}'"}
    model = verifier_model(cfg)
    prompt = build_verify_prompt(plan, diff, untracked)
    (run_dir / f"{label}-prompt.md").write_text(prompt, encoding="utf-8")
    if pcfg["kind"] == "codex":
        return run_codex(prompt, cwd, run_dir, label, model=model, pcfg=pcfg, read_only=True,
                         timeout_s=cfg["verify"].get("timeout_s", 1800), json_schema=VERIFY_SCHEMA)
    env, config_dir = claude_env(pcfg, key, model)
    # setting_sources "" = no user hooks/permissions: the verifier gets a minimal, read-only context.
    return run_claude(prompt, cwd, run_dir, label, model=model, env=env, config_dir=config_dir,
                      timeout_s=cfg["verify"].get("timeout_s", 1800),
                      disallowed=["Edit", "Write", "MultiEdit", "NotebookEdit", "Agent", "Task", "Skill"],
                      json_schema=VERIFY_SCHEMA, setting_sources="", bin_name=pcfg.get("bin", "claude"))


# --- orchestration ----------------------------------------------------------

def execute_chain(prompt: str, cwd: Path, run_dir: Path, tier: str, cfg: dict, secrets: dict,
                  chain: list[str], run_id: str, round_no: int, hook: str | None) -> dict:
    attempts = []
    for prov in chain:
        left = cooldown_left(prov)
        if left:
            attempts.append({"provider": prov, "reason": f"cooldown {left // 60} min"})
            log(f"{prov} skipped: cooldown {left // 60} min")
            continue
        pcfg = cfg["providers"][prov]
        rdir = run_dir / f"r{round_no}"
        rdir.mkdir(parents=True, exist_ok=True)
        before = git(cwd, "status", "--porcelain")
        t0 = time.monotonic()
        res = run_executor(prov, pcfg, prompt, cwd, rdir, tier, secrets)
        res["provider"], res["seconds"] = prov, int(time.monotonic() - t0)
        # Some executors report an error on their LAST message after doing all the work (e.g. a
        # content filter). If the tree changed, the verifier decides, not the exit code.
        if not res["ok"] and res.get("reason") not in ("quota", "inactivity", "timeout") \
                and git(cwd, "status", "--porcelain") != before:
            res["ok"], res["reason"] = True, f"accepted with warning: {res.get('reason')}"
            log(f"{prov} ended with an error but left changes; passing to verification")
        model = pcfg["models"][tier]
        res["tokens"] = norm_usage(res.get("usage"))
        res["cost_usd"] = attempt_cost(pcfg, model, res)
        ledger_append({"ts": iso(now()), "run_id": run_id, "role": "executor", "provider": prov, "model": model,
                       "billing": pcfg.get("billing", "paid"), "round": round_no, "ok": res["ok"],
                       **res["tokens"], "cost_usd": res["cost_usd"], "seconds": res["seconds"]})
        attempt = {k: res.get(k) for k in ("provider", "ok", "reason", "seconds", "cost_usd", "tokens")}
        attempts.append(attempt)
        emit(hook, "attempt", run_id=run_id, round=round_no, **attempt)
        if res["ok"]:
            log(f"{prov} ok {res['seconds']} s · {res['tokens']['input'] + res['tokens']['cached']} in / "
                f"{res['tokens']['output']} out" + (f" · ${res['cost_usd']:.4f}" if res["cost_usd"] else ""))
            return {"result": res, "attempts": attempts}
        if res.get("reason") in ("quota", "inactivity"):
            reset = res.get("reset_s") or 3600
            set_cooldown(prov, reset, res["reason"])
            log(f"{prov} failed: {res['reason']} -> cooldown {reset // 60} min")
        else:
            log(f"{prov} failed: {res.get('reason')}")
    return {"result": None, "attempts": attempts}


def write_report(run_dir: Path, summary: dict) -> Path:
    lines = [f"# execute-cheaper — {summary['run_id']}", "",
             f"- cwd: {summary['cwd']} · tier: {summary['tier']}",
             f"- plan: {summary['plan']}", f"- result: **{summary['status']}**", ""]
    for r in summary["rounds"]:
        lines.append(f"## Round {r['round']}")
        for a in r["attempts"]:
            extra = f" · {a['seconds']} s" if a.get("seconds") is not None else ""
            t = a.get("tokens") or {}
            if t and any(t.values()):
                extra += f" · {t['input'] + t['cached']:,} in ({t['cached']:,} cached) / {t['output']:,} out"
            extra += f" · ${a['cost_usd']:.4f}" if a.get("cost_usd") else ""
            status = "OK" if a.get("ok") else str(a.get("reason"))
            if a.get("ok") and a.get("reason"):
                status += f" ({a['reason'][:160]})"
            lines.append(f"- {a['provider']}: {status}{extra}")
        if r.get("executor_report"):
            lines += ["", "Executor report:", "```", r["executor_report"][:3000], "```"]
        v = r.get("verify")
        if v:
            vt = r.get("verifier_tokens") or {}
            vtxt = (f" · {vt.get('input', 0) + vt.get('cached', 0):,} in ({vt.get('cached', 0):,} cached)"
                    f" / {vt.get('output', 0):,} out"
                    f" · ${r.get('verifier_cost') or 0:.4f}") if vt else ""
            lines += ["", f"Verification ({v.get('by', '?')}): **{v.get('verdict', '?')}** — {v.get('summary', '')}{vtxt}"]
            for k in ("missing", "out_of_scope", "regressions"):
                for item in v.get(k, []) or []:
                    lines.append(f"- {k}: {item}")
            for c in v.get("checks_run", []) or []:
                lines.append(f"- check: {c}")
        lines.append("")
    lines.append(f"Cost: ${summary['cost']['paid']:.4f} paid · ${summary['cost']['subscription']:.4f} "
                 f"subscription-equivalent · {summary['cost']['unpriced']} unpriced attempts")
    lines.append(f"Artifacts: {run_dir}")
    path = run_dir / "report.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def run_costs(run_id: str) -> dict:
    paid = sub = 0.0
    unpriced = 0
    for e in ledger_read(3650):
        if e.get("run_id") != run_id:
            continue
        c = e.get("cost_usd")
        if c is None:
            unpriced += 1
        elif e.get("billing") == "subscription":
            sub += c
        else:
            paid += c
    return {"paid": paid, "subscription": sub, "unpriced": unpriced}


def cmd_run(args: argparse.Namespace) -> int:
    if not CONFIG_PATH.exists():
        raise ConfigError(f"no config at {CONFIG_PATH}: run `execute-cheaper init` first")
    cfg = load_config()
    if args.chain:
        cfg["chain"] = [c.strip() for c in args.chain.split(",") if c.strip()]
    verify_off = args.verify == "none"
    if args.verify and not verify_off and args.verify != cfg["verify"].get("provider"):
        # A model pinned for the configured verifier means nothing to another provider.
        cfg["verify"].update(provider=args.verify, model=None)
    if not verify_off and not cfg["verify"].get("provider"):
        raise ConfigError("no verifier configured: set verify.provider in config or pass --verify none")
    errs = validate_config(cfg)
    if errs:
        raise ConfigError("\n".join(errs))
    secrets = load_env()
    cwd = Path(args.cwd or os.getcwd()).resolve()
    if not is_git_repo(cwd):
        raise ConfigError(f"{cwd} is not a git repository: the verifier reviews `git diff`. Run `git init` first.")
    plan_path = Path(args.plan).resolve()
    plan = plan_path.read_text(encoding="utf-8")
    hook = args.on_event or cfg.get("on_event")
    run_id = now().strftime("%Y%m%d-%H%M%S") + f"-{re.sub(r'[^A-Za-z0-9_.-]', '_', cwd.name)}"
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(plan_path, run_dir / "plan.md")
    tier = args.tier
    max_rounds = args.max_rounds or cfg["max_rounds"]
    t_start = time.monotonic()
    log(f"run {run_id} · tier={tier} · chain={cfg['chain']} · verify={'none' if verify_off else cfg['verify']['provider']}")
    summary = {"run_id": run_id, "cwd": str(cwd), "tier": tier, "plan": str(plan_path),
               "status": "pending", "rounds": []}
    findings = None
    if git(cwd, "status", "--porcelain").strip():
        log("warning: the working tree already had changes; the verifier will see them as part of the diff")
    emit(hook, "run_start", run_id=run_id, cwd=str(cwd), plan=str(plan_path), chain=cfg["chain"], run_dir=str(run_dir))

    for round_no in range(1, max_rounds + 1):
        prompt = build_exec_prompt(plan, findings)
        ex = execute_chain(prompt, cwd, run_dir, tier, cfg, secrets, cfg["chain"], run_id, round_no, hook)
        rnd = {"round": round_no, "attempts": ex["attempts"]}
        if not ex["result"]:
            summary["status"] = "no executor available"
            summary["rounds"].append(rnd)
            break
        rnd["executor_report"] = ex["result"].get("text", "")
        if verify_off:
            summary["status"] = "executed (not verified)"
            summary["rounds"].append(rnd)
            break
        vname = cfg["verify"]["provider"]
        if cooldown_left(vname):
            summary["status"] = f"executed, verification pending ({vname} in cooldown)"
            rnd["verify"] = {"verdict": "pending", "summary": "verifier in cooldown", "by": vname}
            summary["rounds"].append(rnd)
            break
        vpcfg, vmodel = cfg["providers"][vname], verifier_model(cfg)
        vr = run_verifier(plan, cwd, run_dir / f"r{round_no}", cfg, secrets)
        vr["tokens"] = norm_usage(vr.get("usage"))
        vcost = attempt_cost(vpcfg, vmodel, vr) if vr.get("usage") else (0.0 if vr.get("structured") else None)
        ledger_append({"ts": iso(now()), "run_id": run_id, "role": "verifier", "provider": vname, "model": vmodel,
                       "billing": vpcfg.get("billing", "paid"), "round": round_no, "ok": bool(vr.get("structured")),
                       **vr["tokens"], "cost_usd": vcost})
        rnd["verifier_tokens"], rnd["verifier_cost"] = vr["tokens"], vcost
        if not vr.get("ok") or not vr.get("structured"):
            reason = vr.get("reason") or "verifier returned no structured verdict"
            if reason == "quota":
                set_cooldown(vname, vr.get("reset_s", 3600), "quota")
                summary["status"] = f"executed, verification pending ({vname} out of quota)"
            else:
                summary["status"] = f"executed, verification failed: {reason}"
            rnd["verify"] = {"verdict": "pending", "summary": str(reason), "by": vname}
            summary["rounds"].append(rnd)
            break
        v = vr["structured"]
        v["by"] = f"{vname}/{vmodel}"
        rnd["verify"] = v
        summary["rounds"].append(rnd)
        emit(hook, "verify", run_id=run_id, round=round_no, verdict=v["verdict"], summary=v.get("summary", ""))
        if v["verdict"] == "ok":
            summary["status"] = "verified OK"
            break
        findings = v
        summary["status"] = f"verification: {v['verdict']}"
        log(f"verification {v['verdict']} -> {'repair round' if round_no < max_rounds else 'no rounds left'}")

    summary["elapsed_s"] = int(time.monotonic() - t_start)
    summary["cost"] = run_costs(run_id)
    (run_dir / "run.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False), encoding="utf-8")
    report = write_report(run_dir, summary)
    if summary["status"] == "verified OK":
        code = EXIT_OK
    elif "pending" in summary["status"]:
        code = EXIT_VERIFY_PENDING
    else:
        code = EXIT_NOT_VERIFIED
    changed = (git(cwd, "diff", "--name-only") + git(cwd, "ls-files", "--others", "--exclude-standard")).split()
    emit(hook, "run_end", run_id=run_id, status=summary["status"], exit_code=code, report=str(report),
         files=changed, elapsed_s=summary["elapsed_s"], cost=summary["cost"])
    print()
    print(report.read_text(encoding="utf-8"))
    return code


def cmd_verify(args: argparse.Namespace) -> int:
    """Retry only the verification of a previous run (e.g. after the verifier's cooldown)."""
    cfg = load_config()
    run_dir = RUNS_DIR / args.run
    if not (run_dir / "run.json").exists():
        raise ConfigError(f"no such run: {args.run} (see `execute-cheaper status`)")
    if not cfg["verify"].get("provider"):
        raise ConfigError("no verifier configured: set verify.provider in config")
    errs = validate_config(cfg)
    if errs:
        raise ConfigError("\n".join(errs))
    plan = (run_dir / "plan.md").read_text(encoding="utf-8")
    meta = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    cwd = Path(args.cwd or meta["cwd"])
    vname = cfg["verify"]["provider"]
    if cooldown_left(vname):
        log(f"{vname} in cooldown {cooldown_left(vname) // 60} min")
        return EXIT_VERIFY_PENDING
    vr = run_verifier(plan, cwd, run_dir, cfg, load_env(), label="verify-retry")
    print(json.dumps(vr.get("structured") or {"reason": vr.get("reason")}, indent=1, ensure_ascii=False))
    return EXIT_OK if (vr.get("structured") or {}).get("verdict") == "ok" else EXIT_NOT_VERIFIED


def cmd_status(_: argparse.Namespace) -> int:
    cfg = load_config()
    secrets = load_env()
    print(f"config: {CONFIG_PATH} ({'found' if CONFIG_PATH.exists() else 'missing — run `execute-cheaper init`'})")
    print(f"keys:   {ENV_PATH} ({'found' if ENV_PATH.exists() else 'missing'}) + environment")
    print(f"state:  {STATE_DIR}")
    print(f"chain: {cfg['chain']} · verifier: {cfg['verify'].get('provider') or 'none'} · rounds: {cfg['max_rounds']}")
    used = set(cfg["chain"]) | ({cfg["verify"]["provider"]} if cfg["verify"].get("provider") else set())
    for name, p in (cfg.get("providers") or {}).items():
        binary = p.get("bin") or p.get("kind")
        if p.get("use_login"):
            auth = "own Claude Code login"
        elif p.get("kind") in ("codex", "agy"):
            auth = f"own {p['kind']} login"
        elif p.get("key_env"):
            auth = f"{p['key_env']} {'present' if provider_key(p, secrets) else 'MISSING'}"
        elif p.get("base_url"):
            auth = f"no key needed ({p['base_url']})"
        else:
            auth = "no auth configured"
        left = cooldown_left(name)
        mark = "*" if name in used else " "
        print(f" {mark} {name} [{p.get('kind')}]: {binary} {'ok' if shutil.which(binary) else 'NOT FOUND'} · {auth}"
              + (f" · cooldown {left // 60} min" if left else ""))
    errs = validate_config(cfg)
    for e in errs:
        print(f"config error: {e}")
    print(usage_summary(7)[0])
    runs = sorted(RUNS_DIR.glob("*")) if RUNS_DIR.exists() else []
    for r in runs[-5:]:
        rep = r / "report.md"
        line = next((ln for ln in rep.read_text(encoding="utf-8").splitlines() if ln.startswith("- result")), "") \
            if rep.exists() else "(no report)"
        print(f"run {r.name}: {line}")
    return EXIT_CONFIG if errs else EXIT_OK


def usage_summary(days: int) -> tuple[str, dict]:
    rows = ledger_read(days)
    agg: dict[str, dict] = {}
    for e in rows:
        a = agg.setdefault(e["provider"], {"calls": 0, "input": 0, "cached": 0, "output": 0, "cost": 0.0,
                                           "billing": e.get("billing", "paid"), "runs": set()})
        a["calls"] += 1
        a["input"] += e.get("input", 0)
        a["cached"] += e.get("cached", 0)
        a["output"] += e.get("output", 0)
        a["cost"] += e.get("cost_usd") or 0.0
        a["runs"].add(e.get("run_id"))
    lines = [f"execute-cheaper · last {days} days · {len({e.get('run_id') for e in rows})} runs"]
    paid = 0.0
    for prov, a in sorted(agg.items()):
        unit = "subscription-equivalent" if a["billing"] == "subscription" else "paid"
        if a["billing"] != "subscription":
            paid += a["cost"]
        lines.append(f"- {prov}: {a['calls']} calls · {a['input'] + a['cached']:,} in ({a['cached']:,} cached) / "
                     f"{a['output']:,} out · ${a['cost']:.4f} {unit}")
    lines.append(f"Total paid: ${paid:.4f}")
    return "\n".join(lines), {k: {**v, "runs": len(v["runs"])} for k, v in agg.items()}


def cmd_usage(args: argparse.Namespace) -> int:
    text, agg = usage_summary(args.days)
    print(json.dumps(agg, indent=1) if args.json else text)
    return EXIT_OK


def cmd_cooldown(args: argparse.Namespace) -> int:
    state = load_state()
    if args.clear:
        state["cooldowns"].pop(args.provider, None)
        save_state(state)
        print(f"cooldown {args.provider} cleared")
    else:
        set_cooldown(args.provider, args.minutes * 60, "manual")
        print(f"cooldown {args.provider} {args.minutes} min")
    return EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    """Create config, key template and the isolated Claude Code profile used by executors. Idempotent."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not args.force:
        print(f"kept {CONFIG_PATH} (use --force to overwrite)")
    else:
        CONFIG_PATH.write_text(json.dumps(EXAMPLE_CONFIG, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {CONFIG_PATH}")
    if not ENV_PATH.exists():
        keys = sorted({p["key_env"] for p in EXAMPLE_CONFIG["providers"].values() if p.get("key_env")})
        ENV_PATH.write_text("# API keys for execute-cheaper providers (KEY=value, no export).\n"
                            + "".join(f"{k}=\n" for k in keys), encoding="utf-8")
        print(f"wrote {ENV_PATH} — fill in the key(s) for the providers in your chain")
    ENV_PATH.chmod(0o600)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    settings = PROFILE_DIR / "settings.json"
    if not settings.exists() or args.force:
        settings.write_text(json.dumps(executor_profile_settings(), indent=2) + "\n", encoding="utf-8")
    onboarding = PROFILE_DIR / ".claude.json"
    if not onboarding.exists():
        # Skip Claude Code's interactive first-run screens in -p mode.
        onboarding.write_text('{"hasCompletedOnboarding": true}\n', encoding="utf-8")
    (PROFILE_DIR / "CLAUDE.md").write_text(EXECUTOR_PROFILE_CLAUDE_MD, encoding="utf-8")
    print(f"executor profile: {PROFILE_DIR}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="execute-cheaper", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"execute-cheaper {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    i = sub.add_parser("init", help="create config, .env template and the executor profile")
    i.add_argument("--force", action="store_true", help="overwrite an existing config.json")
    i.set_defaults(func=cmd_init)
    r = sub.add_parser("run", help="execute a plan along the provider chain, then verify")
    r.add_argument("--plan", required=True, help="Markdown file with the plan (the contract)")
    r.add_argument("--cwd", default=None, help="target git repo (default: current directory)")
    r.add_argument("--tier", choices=TIERS, default="flash", help="which model of each provider to use")
    r.add_argument("--chain", default=None, help="comma-separated provider order, overrides config")
    r.add_argument("--verify", default=None, help="verifier provider name, or 'none'")
    r.add_argument("--max-rounds", type=int, default=None, help="execution+verification rounds (default 2)")
    r.add_argument("--on-event", default=None, help="shell command that receives each event as JSON on stdin")
    r.set_defaults(func=cmd_run)
    v = sub.add_parser("verify", help="re-run only the verification of a previous run")
    v.add_argument("--run", required=True, help="run id (see `status`)")
    v.add_argument("--cwd", default=None)
    v.set_defaults(func=cmd_verify)
    s = sub.add_parser("status", help="providers, keys, cooldowns and recent runs")
    s.set_defaults(func=cmd_status)
    u = sub.add_parser("usage", help="tokens and cost per provider from the ledger")
    u.add_argument("--days", type=int, default=7)
    u.add_argument("--json", action="store_true")
    u.set_defaults(func=cmd_usage)
    c = sub.add_parser("cooldown", help="set or clear a provider cooldown by hand")
    c.add_argument("provider")
    c.add_argument("--minutes", type=int, default=60)
    c.add_argument("--clear", action="store_true")
    c.set_defaults(func=cmd_cooldown)
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        sys.exit(args.func(args))
    except ConfigError as e:
        print(f"execute-cheaper: {e}", file=sys.stderr)
        sys.exit(EXIT_CONFIG)


if __name__ == "__main__":
    main()
