#!/usr/bin/env bash
# Append every event to a JSONL file another agent (or a dashboard) can tail.
set -euo pipefail
{ cat; echo; } >> "${CHEAP_EXEC_EVENTS:-$HOME/cheap-exec-events.jsonl}"
