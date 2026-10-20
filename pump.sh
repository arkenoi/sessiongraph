#!/bin/sh
# Pull a session's events through Claude itself and drop them where the HUD
# watches. No ssh, no token here: the Claude CLI already has your credentials.
#
#   ./pump.sh session_01ABC...                       # every 2 min into ~/.sessiongraph/dumps
#   ./pump.sh session_01ABC... ~/dumps 300           # custom directory and interval
#   CLAUDE_CMD=/path/to/claude ./pump.sh session_...
#
# Then, on the machine you are watching from:
#   python3 server.py --watch-dir ~/.sessiongraph/dumps
#
# Records are de-duplicated by uuid, so re-fetching the same page every poll is
# harmless and no cursor is tracked on either side.
set -e
SESSION="$1"
OUT="${2:-$HOME/.sessiongraph/dumps}"
EVERY="${3:-120}"
CLAUDE="${CLAUDE_CMD:-claude}"

[ -n "$SESSION" ] || { echo "usage: $0 <session_id> [outdir] [seconds]" >&2; exit 2; }
mkdir -p "$OUT"
echo "pumping $SESSION -> $OUT every ${EVERY}s (ctrl-c to stop)"

while :; do
  STAMP=$(date +%Y%m%d-%H%M%S)
  "$CLAUDE" -p "Call the claude-code-remote list_events tool for session_id $SESSION with kinds [\"assistant\",\"user\"] and limit 100. Write the tool result verbatim to $OUT/page-$STAMP.json — do not summarise, reformat or comment on it. Then reply with only the number of events written." \
    --allowedTools "mcp__claude-code-remote__list_events" "Write" "Bash(cp:*)" \
    >/dev/null 2>&1 || echo "poll failed at $STAMP" >&2
  [ -s "$OUT/page-$STAMP.json" ] && echo "$STAMP  $(wc -c < "$OUT/page-$STAMP.json") bytes"
  sleep "$EVERY"
done
