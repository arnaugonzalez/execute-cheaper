"""execute-cheaper tests with fake `agy`, `claude` and `codex` binaries: no network, no keys, no quota spent."""
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "execute_cheaper.py"
sys.path.insert(0, str(ROOT))
import execute_cheaper as ce  # noqa: E402

FAKE_AGY_QUOTA_HANG = textwrap.dedent(r'''
    #!/usr/bin/env bash
    # Quota exhausted: writes 429s to its log and then hangs instead of exiting.
    log=""; while [ $# -gt 0 ]; do [ "$1" = "--log-file" ] && log="$2"; shift; done
    for i in 1 2 3; do
      echo "E0917 08:34:55 RESOURCE_EXHAUSTED (code 429): Individual quota reached. Resets in 3h6m37s." >> "$log"
      sleep 1
    done
    sleep 300
''').strip()

FAKE_AGY_OK = textwrap.dedent(r'''
    #!/usr/bin/env bash
    log=""; while [ $# -gt 0 ]; do [ "$1" = "--log-file" ] && log="$2"; shift; done
    echo "I0918 ok" >> "$log"
    echo "done by agy" > agy-was-here.txt
    echo '{"conversation_id":"x","status":"SUCCESS","response":"files changed: agy-was-here.txt","usage":{"input_tokens":10,"output_tokens":5}}'
''').strip()

# Executor when --json-schema is absent, verifier when present. FAKE_EXEC / FAKE_VERDICT pick the behaviour.
FAKE_CLAUDE = textwrap.dedent(r'''
    #!/usr/bin/env bash
    schema=0; for a in "$@"; do [ "$a" = "--json-schema" ] && schema=1; done
    prompt="$(cat)"  # like `claude -p`, the prompt arrives on stdin
    if [ -n "${FAKE_ARGS_LOG:-}" ]; then { printf '%s|%s|' "$CLAUDE_CONFIG_DIR" "${ANTHROPIC_BASE_URL:-}"; printf '%q ' "$@"; echo "stdin_bytes=$(printf %s "$prompt" | wc -c | tr -d ' ')"; } >> "$FAKE_ARGS_LOG"; fi
    if [ "$schema" = 0 ]; then
      if [ -n "${ANTHROPIC_BASE_URL:-}" ]; then
        case "$ANTHROPIC_AUTH_TOKEN" in sk-test|ollama) ;; *) echo '{"is_error":true,"result":"bad key"}'; exit 1;; esac
      fi
      case "${FAKE_EXEC:-ok}" in
        hang) sleep 30 ;;
        badjson) echo "Traceback: something exploded" ; exit 0 ;;
      esac
      echo "done via ${ANTHROPIC_BASE_URL:-login}" > cheap-was-here.txt
      [ "${FAKE_EXEC:-ok}" = commit ] && git add cheap-was-here.txt && git -c user.name=t -c user.email=t@t commit -qm exec
      echo '{"type":"result","is_error":false,"result":"files changed: cheap-was-here.txt","total_cost_usd":0.5,"usage":{"input_tokens":100,"output_tokens":20}}'
    else
      if [ "${FAKE_VERDICT:-ok}" = "quota" ]; then echo '{"is_error":true,"result":"You have hit your usage limit. Resets at 17:30"}'; exit 1; fi
      echo "{\"type\":\"result\",\"is_error\":false,\"result\":\"\",\"usage\":{\"input_tokens\":1000,\"output_tokens\":100},\"structured_output\":{\"verdict\":\"${FAKE_VERDICT:-ok}\",\"missing\":[],\"out_of_scope\":[],\"regressions\":[],\"checks_run\":[\"pytest -q -> 3 passed\"],\"summary\":\"looks right\"}}"
    fi
''').strip()

# Codex CLI: reads the prompt from stdin, emits JSONL events. FAKE_CODEX = ok|auth|quota.
FAKE_CODEX = textwrap.dedent(r"""
    #!/usr/bin/env bash
    schema=""; last=""; sandbox=""
    while [ $# -gt 0 ]; do
      case "$1" in --output-schema) schema="$2";; -o) last="$2";; --sandbox) sandbox="$2";;
        --dangerously-bypass-approvals-and-sandbox) sandbox=none;; esac; shift
    done
    cat > /dev/null
    [ -n "${FAKE_ARGS_LOG:-}" ] && echo "codex sandbox=$sandbox schema=${schema:+yes}" >> "$FAKE_ARGS_LOG"
    echo '{"type":"thread.started","thread_id":"t"}'
    case "${FAKE_CODEX:-ok}" in
      auth) echo '{"type":"turn.failed","error":{"message":"Your access token could not be refreshed. Please log out and sign in again."}}'; exit 1;;
      quota) echo '{"type":"turn.failed","error":{"message":"You have hit your usage limit. Try again in 2 hours."}}'; exit 1;;
    esac
    if [ "${FAKE_CODEX:-ok}" = "sandboxfail" ] && [ "$sandbox" != none ] && [ -z "$schema" ]; then
      echo '{"type":"item.completed","item":{"id":"f","type":"file_change","status":"failed","changes":[]}}'
      printf 'Files changed: none, sandbox rejected writes.' > "$last"
      echo '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":5}}'
      exit 0
    fi
    if [ -n "$schema" ]; then
      msg='{"verdict":"ok","missing":[],"out_of_scope":[],"regressions":[],"checks_run":[],"summary":"fine"}'
    else
      echo "done by codex" > codex-was-here.txt; msg="files changed: codex-was-here.txt"
    fi
    printf '%s' "$msg" > "$last"
    echo '{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"see last message"}}'
    echo '{"type":"turn.completed","usage":{"input_tokens":1000,"cached_input_tokens":400,"output_tokens":50}}'
""").strip()

# Generic headless CLI (kind "cli"): Copilot-like argv, plus a usage file. FAKE_CLI picks the behaviour.
FAKE_CLI = textwrap.dedent(r"""
    #!/usr/bin/env bash
    prompt=""; model=""; usage=""
    while [ $# -gt 0 ]; do
      case "$1" in -p) prompt="$2"; shift;; --model) model="$2"; shift;; --usage-output-file) usage="$2"; shift;; esac
      shift
    done
    [ -z "$prompt" ] && prompt="$(cat)"
    [ -n "${FAKE_ARGS_LOG:-}" ] && echo "cli model=$model prompt_len=${#prompt}" >> "$FAKE_ARGS_LOG"
    if [ -n "${FAKE_ISOLATION_LOG:-}" ]; then
      pgid="$(ps -o pgid= -p $$ | tr -d " ")"; extra="$(cat | wc -c | tr -d " ")"
      echo "own_session=$([ "$pgid" = "$$" ] && echo yes || echo no) stdin_bytes=$extra" >> "$FAKE_ISOLATION_LOG"
    fi
    case "${FAKE_CLI:-ok}" in
      fail0) echo "Execution failed: Error: The requested model is not supported."; exit 0;;
      quota) echo "Error: You have exceeded your premium requests for this month." >&2; exit 1;;
      auth) echo "Error: not logged in. Run the CLI and sign in." >&2; exit 1;;
      json) echo '{"response":"files changed: cli-was-here.txt","stats":{"models":{"m-a":{"tokens":{"prompt":100,"cached":40,"candidates":7}},"m-b":{"tokens":{"prompt":50,"cached":0,"candidates":3}}}}}'
            echo "done by cli" > cli-was-here.txt; exit 0;;
      jsonl) echo '{"type":"step_finish","part":{"tokens":{"input":900,"output":20,"reasoning":5,"cache":{"read":100,"write":0}},"cost":0.001}}'
             echo 'progress line that is not JSON'
             echo '{"type":"text","part":{"text":"files changed: cli-was-here.txt"}}'
             echo '{"type":"step_finish","part":{"tokens":{"input":50,"output":10,"reasoning":0,"cache":{"read":1000,"write":0}},"cost":0.0005}}'
             echo "done by cli" > cli-was-here.txt; exit 0;;
      jsonl_error) echo '{"type":"error","error":{"name":"UnknownError","data":{"message":"Provider not found"}}}'; exit 0;;
      jsonl_quota) echo '{"type":"error","timestamp":1790957893401,"error":{"name":"APIError","data":{"message":"Usage limit reached for 5 hour"}}}'; exit 0;;
    esac
    echo "done by cli" > cli-was-here.txt
    [ -n "$usage" ] && echo '{"tokenDetails":{"input":{"tokenCount":3},"cache_read":{"tokenCount":0},"cache_write":{"tokenCount":15707},"output":{"tokenCount":5}}}' > "$usage"
    echo "files changed: cli-was-here.txt"
""").strip()

HOOK = "cat >> \"$HOOK_LOG\"; echo >> \"$HOOK_LOG\""


def provider(**kw):
    base = {"kind": "claude", "models": {"flash": "m-flash", "pro": "m-pro"}, "timeout_s": 60}
    base.update(kw)
    return base


class CheapExecTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="execute-cheaper-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.xdg_cfg = self.tmp / "xdg-config"
        self.xdg_state = self.tmp / "xdg-state"
        self.cfg = self.xdg_cfg / "execute-cheaper"
        self.cfg.mkdir(parents=True)
        self.state = self.xdg_state / "execute-cheaper"
        self.repo = self.tmp / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "PLAN.md").write_text("# Plan\n- T1 create a file\n")
        subprocess.run(["git", "add", "PLAN.md"], cwd=self.repo, check=True)
        self.write_bin("claude", FAKE_CLAUDE)
        self.write_bin("agy", FAKE_AGY_OK)
        self.write_bin("codex", FAKE_CODEX)
        self.write_bin("fake-cli", FAKE_CLI)
        self.config = {
            "chain": ["agy", "cheapapi"],
            "verify": {"provider": "judge", "tier": "pro"},
            "providers": {
                "agy": {"kind": "agy", "bin": str(self.bin / "agy"), "models": {"flash": "g-flash", "pro": "g-pro"},
                        "inactivity_s": 4, "quota_hits_to_abort": 2, "timeout_s": 60, "billing": "subscription"},
                "cheapapi": provider(base_url="https://cheap.example/anthropic", key_env="CHEAP_KEY",
                                     prices={"m-flash": [0.15, 0.003, 0.60]}, billing="paid"),
                "judge": provider(base_url="https://judge.example/anthropic", key_env="CHEAP_KEY",
                                  prices={"m-pro": [2.0, 0.2, 10.0]}, billing="paid"),
                "codex": {"kind": "codex", "models": {"flash": "c-flash", "pro": "c-pro"}, "timeout_s": 60,
                          "billing": "subscription"},
                "local": provider(base_url="http://localhost:11434", env={"ANTHROPIC_AUTH_TOKEN": "ollama"},
                                  billing="local"),
                "cli": {"kind": "cli", "models": {"flash": "x-flash", "pro": "x-pro"}, "timeout_s": 60,
                        "billing": "subscription", "fail_re": "^Execution failed",
                        "cmd": ["fake-cli", "-p", "{prompt}", "--model", "{model}",
                                "--usage-output-file", "{run_dir}/{label}-usage.json"],
                        "usage_file": "{run_dir}/{label}-usage.json",
                        "usage": {"input_tokens": "tokenDetails.input.tokenCount",
                                  "cache_read_input_tokens": "tokenDetails.cache_read.tokenCount",
                                  "cache_creation_input_tokens": "tokenDetails.cache_write.tokenCount",
                                  "output_tokens": "tokenDetails.output.tokenCount"}},
            },
        }
        self.save_config()
        (self.cfg / ".env").write_text("CHEAP_KEY=sk-test\n")
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(("EXECUTE_CHEAPER_", "ANTHROPIC_"))}
        self.env.update(PATH=f"{self.bin}:{os.environ['PATH']}", XDG_CONFIG_HOME=str(self.xdg_cfg),
                        XDG_STATE_HOME=str(self.xdg_state))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_bin(self, name, body):
        (self.bin / name).write_text(body + "\n")
        (self.bin / name).chmod(0o755)

    def save_config(self):
        (self.cfg / "config.json").write_text(json.dumps(self.config))

    def run_cli(self, *args, extra_env=None, cwd=None):
        env = dict(self.env, **(extra_env or {}))
        return subprocess.run([sys.executable, str(SCRIPT), *args], cwd=cwd or self.repo, env=env,
                              capture_output=True, text=True, timeout=120)

    def ledger(self):
        return [json.loads(line) for line in (self.state / "ledger.jsonl").read_text().splitlines()]

    # --- fallback, cooldowns, watchdog -------------------------------------

    def test_agy_quota_hang_falls_back_and_verifies(self):
        self.write_bin("agy", FAKE_AGY_QUOTA_HANG)
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("verified OK", r.stdout)
        self.assertIn("agy: quota", r.stdout)
        self.assertIn("cheapapi: OK", r.stdout)
        self.assertTrue((self.repo / "cheap-was-here.txt").exists())
        state = json.loads((self.state / "state.json").read_text())
        self.assertIn("agy", state["cooldowns"])

    def test_agy_ok_no_fallback(self):
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("agy: OK", r.stdout)
        self.assertNotIn("cheapapi:", r.stdout)

    def test_manual_cooldown_skips_provider(self):
        self.run_cli("cooldown", "agy", "--minutes", "30")
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertIn("agy: cooldown", r.stdout)
        self.assertIn("cheapapi: OK", r.stdout)

    def test_executor_timeout_falls_back(self):
        self.config["chain"] = ["cheapapi", "agy"]
        self.config["providers"]["cheapapi"]["timeout_s"] = 2
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_EXEC": "hang"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cheapapi: timeout", r.stdout)
        self.assertIn("agy: OK", r.stdout)

    def test_unparseable_executor_output_falls_back(self):
        self.config["chain"] = ["cheapapi", "agy"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_EXEC": "badjson"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cheapapi: unparseable output", r.stdout)
        self.assertIn("agy: OK", r.stdout)

    def test_missing_keys_reports_blocked(self):
        self.write_bin("agy", FAKE_AGY_QUOTA_HANG)
        (self.cfg / ".env").write_text("")
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertEqual(r.returncode, 2)
        self.assertIn("no executor available", r.stdout)
        self.assertIn("missing key CHEAP_KEY", r.stdout)

    def test_local_provider_needs_no_key(self):
        (self.cfg / ".env").write_text("")
        self.config["chain"] = ["local"]
        self.config["verify"] = {"provider": "local"}
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Cost: $0.0000 paid", r.stdout)

    # --- verification ------------------------------------------------------

    def test_verifier_quota_leaves_verification_pending(self):
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_VERDICT": "quota"})
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("verification pending", r.stdout)
        self.assertIn("judge", json.loads((self.state / "state.json").read_text())["cooldowns"])

    def test_repair_round_on_incomplete(self):
        r = self.run_cli("run", "--plan", "PLAN.md", "--max-rounds", "2", extra_env={"FAKE_VERDICT": "incomplete"})
        self.assertEqual(r.returncode, 2)
        self.assertIn("## Round 2", r.stdout)
        self.assertIn("verification: incomplete", r.stdout)

    def test_executor_commit_still_reaches_verifier(self):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
                       cwd=self.repo, check=True)
        r = self.run_cli("run", "--plan", "PLAN.md", "--chain", "cheapapi", extra_env={"FAKE_EXEC": "commit"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True,
                                        text=True).stdout, "")
        run_dir = next((self.state / "runs").iterdir())
        self.assertIn("cheap-was-here.txt", (run_dir / "r1" / "verify-prompt.md").read_text())
        self.assertEqual([row["files"] for row in self.ledger() if row["role"] == "run"], [1])

    def test_verifier_is_read_only_and_isolated(self):
        log_path = self.tmp / "args.log"
        self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_ARGS_LOG": str(log_path)})
        verifier_line = next(line for line in log_path.read_text(errors="replace").splitlines() if "--json-schema" in line)
        self.assertIn("https://judge.example/anthropic", verifier_line)
        for tool in ("Edit", "Write", "MultiEdit"):
            self.assertIn(tool, verifier_line.split("--disallowedTools", 1)[1])
        self.assertIn("--setting-sources '' ", verifier_line)

    def test_verifier_gets_large_diff_on_stdin_not_argv(self):
        # Multi-byte text: 120k characters of diff are ~240 KB, over Linux's 128 KiB cap on one argv entry.
        big = self.repo / "big.txt"
        big.write_text("a\n")
        subprocess.run(["git", "add", "big.txt"], cwd=self.repo, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "base"],
                       cwd=self.repo, check=True)
        big.write_text("ñandú " * 50_000)
        log_path = self.tmp / "args.log"
        r = self.run_cli("run", "--plan", "PLAN.md", "--chain", "cheapapi", extra_env={"FAKE_ARGS_LOG": str(log_path)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        verifier_line = next(line for line in log_path.read_text(errors="replace").splitlines() if "--json-schema" in line)
        self.assertNotIn("ñandú", verifier_line.split("stdin_bytes=")[0])
        self.assertGreater(int(verifier_line.rsplit("stdin_bytes=", 1)[1]), 131_072)

    # --- codex ---------------------------------------------------------------

    def test_codex_executes_and_verifies_read_only(self):
        self.config["chain"] = ["codex"]
        self.config["verify"] = {"provider": "codex", "tier": "pro"}
        self.save_config()
        log_path = self.tmp / "args.log"
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_ARGS_LOG": str(log_path)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("codex: OK", r.stdout)
        self.assertTrue((self.repo / "codex-was-here.txt").exists())
        self.assertEqual(log_path.read_text(errors="replace").splitlines(),
                         ["codex sandbox=workspace-write schema=", "codex sandbox=read-only schema=yes"])
        ex = next(e for e in self.ledger() if e["role"] == "executor")
        self.assertEqual((ex["input"], ex["cached"], ex["output"], ex["billing"]), (600, 400, 50, "subscription"))

    def test_verify_override_drops_pinned_model(self):
        self.config["verify"] = {"provider": "judge", "model": "judge-only-model"}
        self.save_config()
        log_path = self.tmp / "args.log"
        r = self.run_cli("run", "--plan", "PLAN.md", "--verify", "codex", extra_env={"FAKE_ARGS_LOG": str(log_path)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("codex/c-pro", r.stdout)
        self.assertNotIn("judge-only-model", log_path.read_text(errors="replace"))

    def test_codex_sandbox_failure_is_not_success(self):
        self.config["chain"] = ["codex", "cheapapi"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CODEX": "sandboxfail"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("sandbox rejected every file edit", r.stdout)
        self.assertIn("cheapapi: OK", r.stdout)

    def test_codex_sandbox_none_bypasses(self):
        self.config["chain"] = ["codex"]
        self.config["providers"]["codex"]["sandbox"] = "none"
        self.save_config()
        log_path = self.tmp / "args.log"
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CODEX": "sandboxfail", "FAKE_ARGS_LOG": str(log_path)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.repo / "codex-was-here.txt").exists())
        self.assertIn("codex sandbox=none", log_path.read_text(errors="replace"))

    def test_codex_expired_login_falls_back_with_hint(self):
        self.config["chain"] = ["codex", "cheapapi"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CODEX": "auth"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("codex login", r.stdout + r.stderr)
        self.assertIn("cheapapi: OK", r.stdout)
        self.assertFalse((self.state / "state.json").exists()
                         and "codex" in json.loads((self.state / "state.json").read_text()).get("cooldowns", {}))

    def test_codex_quota_sets_cooldown(self):
        self.config["chain"] = ["codex", "cheapapi"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CODEX": "quota"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("codex", json.loads((self.state / "state.json").read_text())["cooldowns"])

    # --- generic cli adapter ------------------------------------------------

    def test_cli_executes_with_argv_prompt_and_usage_file(self):
        self.config["chain"] = ["cli"]
        self.save_config()
        log_path = self.tmp / "args.log"
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_ARGS_LOG": str(log_path)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cli: OK", r.stdout)
        self.assertTrue((self.repo / "cli-was-here.txt").exists())
        self.assertRegex(log_path.read_text(errors="replace"), r"cli model=x-flash prompt_len=[1-9]\d+")
        ex = next(e for e in self.ledger() if e["role"] == "executor")
        # cache writes count as input; the usage file is the only source of these numbers.
        self.assertEqual((ex["input"], ex["cached"], ex["output"], ex["billing"]), (15710, 0, 5, "subscription"))

    def test_cli_runs_in_own_session_without_stdin(self):
        self.config["chain"] = ["cli"]
        self.save_config()
        iso = self.tmp / "iso.log"
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_ISOLATION_LOG": str(iso)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(iso.read_text().strip(), "own_session=yes stdin_bytes=0")

    def test_cli_stdin_prompt_and_json_output(self):
        self.config["chain"] = ["cli"]
        self.config["providers"]["cli"].update(
            cmd=["fake-cli", "--model", "{model}"], output="json", text_field="response", usage_file=None,
            usage={"input_tokens": "stats.models.*.tokens.prompt", "cache_read_input_tokens": "stats.models.*.tokens.cached",
                   "output_tokens": "stats.models.*.tokens.candidates"})
        self.save_config()
        log_path = self.tmp / "args.log"
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CLI": "json", "FAKE_ARGS_LOG": str(log_path)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cli: OK", r.stdout)
        self.assertRegex(log_path.read_text(errors="replace"), r"prompt_len=[1-9]\d+")  # arrived through stdin
        ex = next(e for e in self.ledger() if e["role"] == "executor")
        self.assertEqual((ex["input"], ex["cached"], ex["output"]), (150, 40, 10))  # '*' sums both models

    def test_cli_jsonl_events_sum_usage_and_cli_cost(self):
        preset = json.loads((ROOT / "examples" / "providers" / "opencode.json").read_text())["opencode"]
        self.config["chain"] = ["cli"]
        self.config["providers"]["cli"] = dict(preset, cmd=["fake-cli", "--model", "{model}", "-p", "{prompt}"],
                                               models={"flash": "x-flash", "pro": "x-pro"})
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CLI": "jsonl"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cli: OK", r.stdout)
        ex = next(e for e in self.ledger() if e["role"] == "executor")
        self.assertEqual((ex["input"], ex["cached"], ex["output"]), (950, 1100, 35))  # reasoning counts as output
        self.assertAlmostEqual(ex["cost_usd"], 0.0015)  # no price table: the CLI's own figure

    def test_cli_jsonl_error_event_falls_through(self):
        preset = json.loads((ROOT / "examples" / "providers" / "opencode.json").read_text())["opencode"]
        self.config["chain"] = ["cli", "cheapapi"]
        self.config["providers"]["cli"] = dict(preset, cmd=["fake-cli", "-p", "{prompt}"],
                                               models={"flash": "x-flash", "pro": "x-pro"})
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CLI": "jsonl_error"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("Provider not found", r.stdout + r.stderr)
        self.assertIn("cheapapi: OK", r.stdout)

    def test_cli_jsonl_quota_is_not_mistaken_for_auth(self):
        preset = json.loads((ROOT / "examples" / "providers" / "opencode-zai.json").read_text())["opencode-zai"]
        self.config["chain"] = ["cli", "cheapapi"]
        self.config["providers"]["cli"] = dict(preset, cmd=["fake-cli", "-p", "{prompt}"])
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CLI": "jsonl_quota"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("not logged in", r.stdout + r.stderr)  # the timestamp contains "401"
        cooldown = json.loads((self.state / "state.json").read_text())["cooldowns"]["cli"]
        self.assertIn("cheapapi: OK", r.stdout)
        self.assertTrue(cooldown)

    def test_cli_exit_zero_failure_is_detected(self):
        self.config["chain"] = ["cli", "cheapapi"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CLI": "fail0"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("not supported", r.stdout + r.stderr)
        self.assertIn("cheapapi: OK", r.stdout)
        self.assertFalse((self.repo / "cli-was-here.txt").exists())

    def test_cli_quota_sets_cooldown_and_auth_gives_hint(self):
        self.config["chain"] = ["cli", "cheapapi"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CLI": "quota"})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("cli", json.loads((self.state / "state.json").read_text())["cooldowns"])
        self.run_cli("cooldown", "cli", "--clear")
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_CLI": "auth"})
        self.assertIn("not logged in", r.stdout + r.stderr)
        self.assertNotIn("cli", json.loads((self.state / "state.json").read_text()).get("cooldowns", {}))

    def test_attempt_that_never_started_costs_nothing(self):
        self.config["providers"]["gone"] = dict(self.config["providers"]["cli"], cmd=["no-such-cli", "-p", "{prompt}"])
        self.config["chain"] = ["gone", "cheapapi"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("binary not found: no-such-cli", r.stdout)
        gone = next(e for e in self.ledger() if e["provider"] == "gone")
        self.assertEqual(gone["cost_usd"], 0.0)
        self.assertIn("0 unpriced attempts", r.stdout)

    def test_cli_cannot_verify_and_needs_cmd(self):
        self.config["verify"] = {"provider": "cli", "tier": "pro"}
        self.config["providers"]["cli"]["cmd"] = "fake-cli -p {prompt}"
        self.config["providers"]["cli"]["fail_re"] = "("
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertEqual(r.returncode, ce.EXIT_CONFIG, r.stdout + r.stderr)
        for needle in ("must be a 'claude' or 'codex'", "cmd must be a non-empty list", "not a valid regex"):
            self.assertIn(needle, r.stdout + r.stderr)

    # --- cost ledger -------------------------------------------------------

    def test_ledger_costs_are_deterministic(self):
        self.config["chain"] = ["cheapapi"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        rows = self.ledger()
        ex = next(e for e in rows if e["role"] == "executor")
        vf = next(e for e in rows if e["role"] == "verifier")
        self.assertAlmostEqual(ex["cost_usd"], (100 * 0.15 + 20 * 0.60) / 1e6)
        self.assertAlmostEqual(vf["cost_usd"], (1000 * 2.0 + 100 * 10.0) / 1e6)
        self.assertIn("Cost: $0.0030 paid", r.stdout)
        u = self.run_cli("usage", "--days", "1", "--json")
        self.assertEqual(json.loads(u.stdout)["cheapapi"]["calls"], 1)

    def test_login_executor_passes_effort_and_compare_reports_outcome(self):
        self.config["providers"]["haiku"] = {"kind": "claude", "use_login": True, "effort": "high",
                                             "models": {"flash": "claude-haiku-5-5", "pro": "claude-haiku-5-5"},
                                             "timeout_s": 60, "billing": "subscription"}
        self.config["chain"] = ["haiku"]
        self.save_config()
        args_log = self.tmp / "args.log"
        r = self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_ARGS_LOG": str(args_log)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        executor_call = args_log.read_text().splitlines()[0]
        self.assertIn("--model claude-haiku-5-5", executor_call)
        self.assertIn("--effort high", executor_call)
        run_row = next(e for e in self.ledger() if e["role"] == "run")
        self.assertEqual((run_row["provider"], run_row["status"], run_row["rounds"]), ("haiku", "verified OK", 1))
        c = self.run_cli("compare", "--days", "1", "--json")
        agg = json.loads(c.stdout)["haiku"]
        self.assertEqual((agg["runs"], agg["ok"]), (1, 1))
        self.assertAlmostEqual(agg["executor_usd"], 0.5)  # Claude Code's own figure for a login executor
        self.assertIn("| haiku | 1 | 1/1 |", self.run_cli("compare", "--days", "1").stdout)

    def test_executor_without_effort_gets_no_flag(self):
        self.config["chain"] = ["cheapapi"]
        self.save_config()
        args_log = self.tmp / "args.log"
        self.run_cli("run", "--plan", "PLAN.md", extra_env={"FAKE_ARGS_LOG": str(args_log)})
        self.assertNotIn("--effort", args_log.read_text())

    def test_peak_multiplier(self):
        pcfg = {"prices": {"m": [1.0, 0.0, 0.0]}, "peak": {"multiplier": 2.0, "utc_hours": [[1, 4]], "weekdays_only": True}}
        usage = {"input_tokens": 1_000_000}
        monday_peak = ce.dt.datetime(2026, 9, 21, 2, tzinfo=ce.dt.timezone.utc)
        saturday_peak = ce.dt.datetime(2026, 9, 26, 2, tzinfo=ce.dt.timezone.utc)
        self.assertEqual(ce.estimate_cost(pcfg, "m", usage, monday_peak), 2.0)
        self.assertEqual(ce.estimate_cost(pcfg, "m", usage, saturday_peak), 1.0)
        self.assertIsNone(ce.estimate_cost(pcfg, "unknown-model", usage, monday_peak))

    def test_server_tool_calls_priced_after_since_and_without_peak(self):
        pcfg = {"prices": {"m": [0.0, 0.0, 0.0]}, "peak": {"multiplier": 2.0, "utc_hours": [[1, 4]]},
                "tool_prices": {"web_search_requests": 0.001, "web_fetch_requests": 0.002},
                "tool_prices_since": "2026-10-31T16:00:00+00:00"}
        usage = {"server_tool_use": {"web_search_requests": 3, "web_fetch_requests": 1}}
        free = ce.dt.datetime(2026, 10, 31, 15, 59, tzinfo=ce.dt.timezone.utc)
        billed_peak = ce.dt.datetime(2026, 11, 2, 2, tzinfo=ce.dt.timezone.utc)
        self.assertEqual(ce.estimate_cost(pcfg, "m", usage, free), 0.0)
        self.assertAlmostEqual(ce.estimate_cost(pcfg, "m", usage, billed_peak), 0.005)
        self.assertEqual(ce.estimate_cost(pcfg, "m", {"input_tokens": 0}, billed_peak), 0.0)
        self.assertTrue(ce.EXAMPLE_CONFIG["providers"]["deepseek"]["tool_prices"])

    # --- config, guards, hooks --------------------------------------------

    def test_unknown_provider_is_a_readable_config_error(self):
        self.config["chain"] = ["nope"]
        self.save_config()
        r = self.run_cli("run", "--plan", "PLAN.md")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown provider 'nope'", r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_cwd_must_be_git_repo(self):
        plain = self.tmp / "plain"
        plain.mkdir()
        (plain / "PLAN.md").write_text("# Plan\n")
        r = self.run_cli("run", "--plan", "PLAN.md", cwd=plain)
        self.assertEqual(r.returncode, 1)
        self.assertIn("not a git repository", r.stderr)

    def test_on_event_hook_receives_events(self):
        hook_log = self.tmp / "events.jsonl"
        r = self.run_cli("run", "--plan", "PLAN.md", "--on-event", HOOK, extra_env={"HOOK_LOG": str(hook_log)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        events = [json.loads(line) for line in hook_log.read_text().splitlines() if line.strip()]
        self.assertEqual([e["event"] for e in events], ["run_start", "attempt", "verify", "run_end"])
        self.assertEqual(events[-1]["exit_code"], 0)
        self.assertIn("agy-was-here.txt", events[-1]["files"])

    def test_failing_hook_does_not_break_run(self):
        r = self.run_cli("run", "--plan", "PLAN.md", "--on-event", "exit 7")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("on_event hook exited 7", r.stdout)

    def test_init_is_idempotent_and_private(self):
        shutil.rmtree(self.cfg)
        for _ in range(2):
            r = self.run_cli("init", "--chain", "codex,openrouter", "--verify", "claude-login")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(stat.S_IMODE((self.cfg / ".env").stat().st_mode), 0o600)
        cfg = json.loads((self.cfg / "config.json").read_text())
        self.assertEqual(cfg["chain"], ["codex", "openrouter"])
        self.assertEqual(sorted(cfg["providers"]), ["claude-login", "codex", "openrouter"])
        settings = json.loads((self.state / "claude-profile" / "settings.json").read_text())
        self.assertIn(f"Read({self.cfg}/**)", settings["permissions"]["deny"])
        s = self.run_cli("status")
        self.assertIn("OPENROUTER_API_KEY MISSING", s.stdout)
        self.assertIn("chain provider 'openrouter' can't run yet: no OPENROUTER_API_KEY", s.stdout)

    def test_example_config_matches_and_validates(self):
        self.assertEqual(json.loads((ROOT / "config.example.json").read_text()), ce.EXAMPLE_CONFIG)
        cfg = json.loads(json.dumps(ce.DEFAULT_CONFIG))
        cfg.update(json.loads(json.dumps(ce.EXAMPLE_CONFIG)))
        self.assertEqual(ce.validate_config(cfg), [])
        presets = sorted((ROOT / "examples" / "providers").glob("*.json"))
        self.assertTrue(presets)
        for path in presets:  # untested against the real tools, but at least loadable and complete
            cfg["providers"].update(json.loads(path.read_text()))
        self.assertEqual(ce.validate_config(cfg), [], presets)

    def test_two_runs_same_outcome(self):
        outcomes = []
        for _ in range(2):
            subprocess.run(["git", "clean", "-fdq", "-e", "PLAN.md"], cwd=self.repo, check=True)
            r = self.run_cli("run", "--plan", "PLAN.md")
            outcomes.append((r.returncode, "verified OK" in r.stdout, "agy: OK" in r.stdout))
        self.assertEqual(outcomes[0], outcomes[1])

    def test_tty_summary_is_short_and_hides_home(self):
        summary = {"status": "verified OK", "rounds": [{}], "elapsed_s": 51,
                   "cost": {"paid": 0.0, "subscription": 0.0691, "unpriced": 1}}
        report = Path.home() / ".local/state/execute-cheaper/runs/x/report.md"
        out = ce.tty_summary(summary, ["app/main.py", "tests/test_main.py"], report)
        self.assertEqual(len(out.splitlines()), 4)
        self.assertIn("verified OK · 1 round · 51 s", out)
        self.assertIn("$0.00 paid · $0.07 on subscriptions", out)
        self.assertIn("~/.local/state/execute-cheaper/runs/x/report.md", out)
        self.assertNotIn(str(Path.home()) + "/", out)

    def test_claude_code_plugin_manifests(self):
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
        plugin_dir = ROOT / market["plugins"][0]["source"]
        manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text())
        self.assertEqual(manifest["version"], ce.__version__)  # bump both together
        skill = (plugin_dir / "skills" / "execute-cheaper" / "SKILL.md").read_text()
        self.assertTrue(skill.startswith("---\nname: execute-cheaper\n"))
        setup = (plugin_dir / "skills" / "setup" / "SKILL.md").read_text()
        self.assertTrue(setup.startswith("---\nname: setup\n"))
        for doc in (setup, (ROOT / "AGENTS.md").read_text()):
            for cmd in ("init --detect --json", "init --chain", "status --json"):
                self.assertIn(cmd, doc)

    def test_parse_reset(self):
        self.assertEqual(ce.parse_reset("3h6m37s"), 3 * 3600 + 6 * 60 + 37 + 60)
        self.assertEqual(ce.parse_reset("45m"), 45 * 60 + 60)
        self.assertEqual(ce.parse_reset("garbage"), 3600)


KEY_VARS = ("OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "ZAI_API_KEY", "ANTHROPIC_API_KEY")
SECRET = "sk-should-never-be-printed-42"


class InitTest(unittest.TestCase):
    """`init` detection and choice, with a PATH that holds only fake CLIs."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="execute-cheaper-init-"))
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.cfg = self.tmp / "xdg-config" / "execute-cheaper"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("EXECUTE_CHEAPER_", "ANTHROPIC_")) and k not in KEY_VARS}
        self.env.update(PATH=str(self.bin), HOME=str(self.tmp), XDG_CONFIG_HOME=str(self.tmp / "xdg-config"),
                        XDG_STATE_HOME=str(self.tmp / "xdg-state"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fake(self, *names):
        for n in names:
            (self.bin / n).write_text("#!/bin/sh\nexit 0\n")
            (self.bin / n).chmod(0o755)

    def init(self, *args, **extra):
        return subprocess.run([sys.executable, str(SCRIPT), "init", *args], env=dict(self.env, **extra),
                              capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)

    def detect(self, **extra):
        r = self.init("--detect", "--json", **extra)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def test_detect_json_schema_is_stable(self):
        self.fake("codex", "claude")
        d = self.detect(DEEPSEEK_API_KEY=SECRET)
        self.assertEqual(sorted(d), ["clis", "config_exists", "config_path", "env_path", "keys", "routes",
                                     "schema", "suggested", "warnings"])
        self.assertEqual(d["schema"], 1)
        self.assertEqual(d["clis"], {"agy": False, "claude": True, "codex": True, "copilot": False})
        self.assertEqual(d["keys"]["DEEPSEEK_API_KEY"], "env")
        self.assertIsNone(d["keys"]["OPENROUTER_API_KEY"])
        self.assertEqual([r["name"] for r in d["routes"]], list(ce.EXAMPLE_CONFIG["providers"]))
        for r in d["routes"]:
            self.assertEqual(sorted(r), ["about", "billing", "can_verify", "cli", "family", "key_env", "kind",
                                         "missing", "name", "note", "opt_in", "ready"])
        self.assertEqual(d["suggested"], {"chain": ["codex", "deepseek"], "verify": "claude-login"})
        self.assertFalse((self.cfg / "config.json").exists())  # --detect writes nothing
        agents = (ROOT / "AGENTS.md").read_text()
        for field in [*d, *d["routes"][0]]:  # every documented field is real and every real field documented
            self.assertIn(f"`{field}`", agents)
        self.assertFalse(self.cfg.exists())

    def test_agy_is_never_suggested_and_carries_the_terms_warning(self):
        self.fake("agy", "claude")
        d = self.detect()
        agy = next(r for r in d["routes"] if r["name"] == "agy")
        self.assertTrue(agy["ready"] and agy["opt_in"])
        self.assertIn("terms", agy["note"])
        self.assertTrue(any("terms" in w for w in d["warnings"]))
        self.assertEqual(d["suggested"], {"chain": ["claude-login"], "verify": "claude-login"})
        r = self.init("--chain", "agy", "--verify", "claude-login", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(json.loads((self.cfg / "config.json").read_text())["chain"], ["agy"])

    def test_verifier_defaults_to_another_family(self):
        self.fake("codex", "claude")
        self.assertEqual(self.detect()["suggested"], {"chain": ["codex"], "verify": "claude-login"})
        (self.bin / "claude").unlink()
        self.assertEqual(self.detect()["suggested"], {"chain": ["codex"], "verify": "codex"})

    def test_flags_write_only_the_chosen_providers_and_their_keys(self):
        self.fake("codex", "claude")
        r = self.init("--chain", "codex,openrouter", "--verify", "claude-login", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual((out["ok"], out["written"], out["keys_to_fill"]), (True, True, ["OPENROUTER_API_KEY"]))
        cfg = json.loads((self.cfg / "config.json").read_text())
        self.assertEqual(sorted(cfg["providers"]), ["claude-login", "codex", "openrouter"])
        self.assertEqual(cfg["verify"]["provider"], "claude-login")
        self.assertEqual(ce.validate_config(cfg), [])
        env_lines = [ln for ln in (self.cfg / ".env").read_text().splitlines() if not ln.startswith("#")]
        self.assertEqual(env_lines, ["OPENROUTER_API_KEY="])
        again = self.init("--chain", "codex,openrouter", "--verify", "claude-login", "--json")
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertFalse(json.loads(again.stdout)["written"])
        self.assertEqual((self.cfg / ".env").read_text().count("OPENROUTER_API_KEY"), 1)
        other = self.init("--chain", "codex", "--json")
        self.assertEqual(other.returncode, 1)
        self.assertIn("--force", json.loads(other.stdout)["errors"][0])
        forced = self.init("--chain", "codex", "--verify", "none", "--force")
        self.assertEqual(forced.returncode, 0, forced.stderr)
        self.assertEqual(json.loads((self.cfg / "config.json").read_text())["providers"].keys(), {"codex"})

    def test_flags_reject_missing_cli_and_bad_verifier(self):
        self.fake("claude")
        r = self.init("--chain", "copilot")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no `copilot` CLI", r.stderr)
        r = self.init("--chain", "claude-login", "--verify", "nope")
        self.assertEqual(r.returncode, 1)
        self.assertIn("unknown route 'nope'", r.stderr)
        self.fake("copilot")
        r = self.init("--chain", "claude-login", "--verify", "copilot")
        self.assertEqual(r.returncode, 1)
        self.assertIn("must be a claude or codex route", r.stderr)
        self.assertFalse((self.cfg / "config.json").exists())

    def test_nothing_usable_exits_nonzero_without_writing(self):
        for args in ((), ("--yes",), ("--json",)):
            r = self.init(*args)
            self.assertEqual(r.returncode, 1, args)
            self.assertIn("nothing usable found", r.stdout + r.stderr)
        self.assertFalse((self.cfg / "config.json").exists())

    def test_yes_accepts_detection(self):
        self.fake("codex", "claude")
        r = self.init("--yes", OPENROUTER_API_KEY=SECRET)
        self.assertEqual(r.returncode, 0, r.stderr)
        cfg = json.loads((self.cfg / "config.json").read_text())
        self.assertEqual((cfg["chain"], cfg["verify"]["provider"]), (["codex", "openrouter"], "claude-login"))
        self.assertNotIn("OPENROUTER_API_KEY=", (self.cfg / ".env").read_text())  # already in the environment

    def test_keys_never_appear_in_any_output(self):
        self.fake("codex", "claude")
        self.cfg.mkdir(parents=True)
        (self.cfg / ".env").write_text(f"ZAI_API_KEY={SECRET}\n")
        extra = {"OPENROUTER_API_KEY": SECRET, "DEEPSEEK_API_KEY": SECRET}
        outs = [self.init("--detect", **extra), self.init("--detect", "--json", **extra),
                self.init("--yes", "--json", **extra), self.init("--chain", "zai,deepseek", "--force", **extra)]
        for args in (("status",), ("status", "--json")):
            outs.append(subprocess.run([sys.executable, str(SCRIPT), *args], env=dict(self.env, **extra),
                                       capture_output=True, text=True, timeout=60))
        for r in outs:
            self.assertNotIn(SECRET, r.stdout + r.stderr)
        status = json.loads(outs[-1].stdout)
        self.assertEqual({p["name"]: p["key_source"] for p in status["providers"] if p["in_chain"]},
                         {"zai": ".env", "deepseek": "env"})
        self.assertTrue(status["ready"])

    def test_status_reports_routes_that_cannot_run(self):
        self.fake("codex", "claude")
        self.assertEqual(self.init("--chain", "codex,openrouter", "--verify", "claude-login").returncode, 0)
        (self.bin / "codex").unlink()
        r = subprocess.run([sys.executable, str(SCRIPT), "status", "--json"], env=self.env, capture_output=True,
                           text=True, timeout=60)
        s = json.loads(r.stdout)
        self.assertEqual(s["problems"], ["chain provider 'codex' can't run yet: no `codex` CLI",
                                         "chain provider 'openrouter' can't run yet: no OPENROUTER_API_KEY"])
        self.assertFalse(s["ready"])

    def test_interactive_choice(self):
        self.fake("codex", "claude", "agy")
        answers = iter(["copilot", "agy,1", "", ""])  # copilot is missing: asked again
        prompts = []

        def fake_input(prompt):
            prompts.append(prompt)
            return next(answers)

        paths = dict(CONFIG_DIR=self.cfg, CONFIG_PATH=self.cfg / "config.json", ENV_PATH=self.cfg / ".env",
                     PROFILE_DIR=self.tmp / "profile", STATE_DIR=self.tmp / "state")
        args = ce.build_parser().parse_args(["init"])
        with mock.patch.multiple(ce, **paths), mock.patch.dict(os.environ, {"PATH": str(self.bin)}), \
                mock.patch("builtins.input", fake_input), mock.patch.object(sys.stdin, "isatty", lambda: True), \
                mock.patch("sys.stdout", new_callable=io.StringIO) as out:
            for k in KEY_VARS:
                os.environ.pop(k, None)
            self.assertEqual(ce.cmd_init(args), 0)
        self.assertIn("[codex]", prompts[0])
        self.assertIn("no `copilot` CLI", out.getvalue())
        self.assertIn("[claude-login]", prompts[2])  # verifier default: another family than agy and codex
        self.assertIn("terms", out.getvalue())
        cfg = json.loads((self.cfg / "config.json").read_text())
        self.assertEqual((cfg["chain"], cfg["verify"]["provider"]), (["agy", "codex"], "claude-login"))


if __name__ == "__main__":
    unittest.main()
