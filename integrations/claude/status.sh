#!/usr/bin/env bash
# integrations/claude/status.sh — Send overlay state updates on hook events
# Used by: UserPromptSubmit, PreToolUse, SubagentStart, Stop
#
# Install via: cortana-tts install claude
# Or manually set CORTANA_TTS_SERVER env var to override the server URL.

set -euo pipefail

CORTANA_TTS_SERVER="${CORTANA_TTS_SERVER:-http://127.0.0.1:5111}"
if [ -d "$HOME/Library/Logs" ]; then
    LOGFILE="$HOME/Library/Logs/cortana-tts-hook.log"
else
    LOGFILE="${XDG_STATE_HOME:-$HOME/.local/state}/cortana-tts-hook.log"
fi

INPUT=$(cat)

HOOK_EVENT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hook_event_name',''))" 2>/dev/null || echo "")

echo "$(date): [cortana-tts] status.sh event=$HOOK_EVENT" >> "$LOGFILE"

case "$HOOK_EVENT" in
    UserPromptSubmit)
        # Auto-start TTS server if not running
        if ! curl -s --connect-timeout 1 --max-time 1 "$CORTANA_TTS_SERVER/health" > /dev/null 2>&1; then
            if command -v cortana-tts >/dev/null 2>&1; then
                cortana-tts start --bg >> "$LOGFILE" 2>&1 &
                echo "$(date): [cortana-tts] auto-starting server via cortana-tts start --bg" >> "$LOGFILE"
                sleep 2
            else
                echo "$(date): [cortana-tts] cortana-tts not found in PATH, cannot auto-start" >> "$LOGFILE"
            fi
        fi
        # Clear TTS played flag (new turn starting)
        CORTANA_TTS_RUNTIME="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/cortana-tts-$(id -u)"
        rm -f "$CORTANA_TTS_RUNTIME/tts_played"
        # Inject TTS verbosity mode into Claude's context
        TTS_MODE_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/cortana-tts/tts_mode"
        if [ -f "$TTS_MODE_FILE" ]; then
            echo "tts_verbosity=$(cat "$TTS_MODE_FILE")"
        else
            echo "tts_verbosity=normal"
        fi
        curl -s -X POST "$CORTANA_TTS_SERVER/status" \
            -H "Content-Type: application/json" \
            -d '{"state": "thinking"}' \
            --connect-timeout 1 --max-time 2 \
            >> "$LOGFILE" 2>&1 &
        ;;
    PreToolUse)
        TOOL_JSON=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
tool = data.get('tool_name', '')
print(json.dumps({'state': 'thinking', 'event': 'tool_use', 'tool_name': tool}))
" 2>/dev/null || echo '{"state":"thinking","event":"tool_use"}')
        curl -s -X POST "$CORTANA_TTS_SERVER/status" \
            -H "Content-Type: application/json" \
            -d "$TOOL_JSON" \
            --connect-timeout 1 --max-time 2 \
            >> "$LOGFILE" 2>&1 &
        ;;
    SubagentStart)
        curl -s -X POST "$CORTANA_TTS_SERVER/status" \
            -H "Content-Type: application/json" \
            -d '{"state": "thinking", "event": "subagent_start"}' \
            --connect-timeout 1 --max-time 2 \
            >> "$LOGFILE" 2>&1 &
        ;;
    Stop)
        curl -s -X POST "$CORTANA_TTS_SERVER/status" \
            -H "Content-Type: application/json" \
            -d '{"state": "idle"}' \
            --connect-timeout 1 --max-time 2 \
            >> "$LOGFILE" 2>&1 &
        ;;
esac

exit 0
