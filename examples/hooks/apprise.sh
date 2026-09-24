#!/usr/bin/env bash
# Send run results to Telegram, Slack, email, ntfy... via apprise (pip install apprise).
# Set APPRISE_URLS, e.g. "tgram://BOT_TOKEN/CHAT_ID" or "ntfy://my-topic".
set -euo pipefail
event=$(cat)
kind=$(printf '%s' "$event" | python3 -c 'import json,sys; print(json.load(sys.stdin)["event"])')
[ "$kind" = run_end ] || exit 0
body=$(printf '%s' "$event" | python3 -c 'import json,sys; e=json.load(sys.stdin); print(e["status"]); print(e["report"])')
apprise -t "execute-cheaper" -b "$body" ${APPRISE_URLS}
