#!/usr/bin/env bash
# Append every event to a JSONL file another agent (or a dashboard) can tail.
set -euo pipefail
{ cat; echo; } >> "${EXECUTE_CHEAPER_EVENTS:-$HOME/execute-cheaper-events.jsonl}"
