#!/usr/bin/env bash
# Desktop notification when a run ends. Usage: execute-cheaper run --plan P.md --on-event examples/hooks/notify-desktop.sh
set -euo pipefail
event=$(cat)
[ "$(printf '%s' "$event" | python3 -c 'import json,sys; print(json.load(sys.stdin)["event"])')" = run_end ] || exit 0
msg=$(printf '%s' "$event" | python3 -c 'import json,sys; e=json.load(sys.stdin); print("%s · %s s · $%.4f" % (e["status"], e["elapsed_s"], e["cost"]["paid"]))')
if command -v notify-send >/dev/null; then notify-send "execute-cheaper" "$msg"
elif command -v osascript >/dev/null; then osascript -e "display notification \"$msg\" with title \"execute-cheaper\""
else echo "execute-cheaper: $msg" >&2; fi
