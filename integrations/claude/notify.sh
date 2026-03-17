#!/usr/bin/env bash
# integrations/claude/notify.sh — cortana-tts hook for Claude Code
# Extracts <tts> tag from Claude's response and sends to TTS server.
# Handles: Stop, Notification, PermissionRequest, PreToolUse (immediate mode)
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
TTS_TIMING_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/cortana-tts/tts_timing"
CORTANA_TTS_RUNTIME="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/cortana-tts-$(id -u)"
mkdir -p "$CORTANA_TTS_RUNTIME"
TTS_PLAYED_FLAG="$CORTANA_TTS_RUNTIME/tts_played"
TTS_LAST_HASH="$CORTANA_TTS_RUNTIME/tts_last_hash"

# Portable md5 (macOS: md5, Linux: md5sum)
md5_hash() { command -v md5 >/dev/null 2>&1 && md5 -q || md5sum | cut -d' ' -f1; }

INPUT=$(cat)

HOOK_EVENT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hook_event_name','unknown'))" 2>/dev/null || echo "unknown")

echo "$(date +%H:%M:%S.%3N): [cortana-tts] hook entry event=$HOOK_EVENT" >> "$LOGFILE"

# Read TTS timing mode
TTS_TIMING="on-complete"
if [ -f "$TTS_TIMING_FILE" ]; then
    TTS_TIMING=$(cat "$TTS_TIMING_FILE")
fi

# Helper: extract TTS from transcript JSONL (current turn only)
extract_tts_from_transcript() {
    local transcript_path="$1"
    python3 -c "
import json, re, sys

entries = []
last_user_prompt_idx = -1

with open(sys.argv[1]) as f:
    for i, line in enumerate(f):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        entries.append(obj)
        msg = obj.get('message', {})
        if isinstance(msg, dict) and msg.get('role') == 'user':
            content = msg.get('content', [])
            if isinstance(content, list):
                is_tool_result = any(
                    isinstance(b, dict) and b.get('type') == 'tool_result'
                    for b in content
                )
                if not is_tool_result:
                    last_user_prompt_idx = len(entries) - 1

# Only search assistant messages after the last user prompt (current turn)
tts = ''
mood = ''
for obj in entries[last_user_prompt_idx + 1:]:
    msg = obj.get('message', {})
    if not isinstance(msg, dict) or msg.get('role') != 'assistant':
        continue
    content = msg.get('content', [])
    if not isinstance(content, list):
        continue
    for block in content:
        if isinstance(block, dict) and block.get('type') == 'text':
            text = block.get('text', '')
            m = re.search(r'(?:<!--\s*)?<tts(?:\s+mood=\"([^\"]*)\")?\s*>(.*?)</tts>(?:\s*-->)?', text, re.DOTALL)
            if m:
                tts = m.group(2).strip()
                mood = m.group(1) or ''
print(tts + '|||' + mood)
" "$transcript_path" 2>/dev/null || echo "|||"
}

# Helper: extract TTS from raw text
extract_tts_from_text() {
    echo "$1" | python3 -c "
import sys, re
text = sys.stdin.read()
match = re.search(r'(?:<!--\s*)?<tts(?:\s+mood=\"([^\"]*)\")?\s*>(.*?)</tts>(?:\s*-->)?', text, re.DOTALL)
if match:
    print(match.group(2).strip() + '|||' + (match.group(1) or ''))
else:
    print('|||')
" 2>/dev/null || echo "|||"
}

# Helper: fire TTS to server
fire_tts() {
    local tts_text="$1"
    local mood="$2"
    local mood_json=""
    if [ -n "$mood" ]; then
        # Whitelist valid moods
        case "$mood" in
            error|success|warn|melancholy) ;;
            *) mood="" ;;
        esac
        if [ -n "$mood" ]; then
            mood_json=", \"mood\": \"${mood}\""
        fi
    fi
    echo "$(date +%H:%M:%S.%3N): [cortana-tts] hook→server POST /speak" >> "$LOGFILE"
    curl -s -X POST "$CORTANA_TTS_SERVER/speak" \
        -H "Content-Type: application/json" \
        -d "{\"text\": $(echo "$tts_text" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')$mood_json}" \
        --connect-timeout 2 \
        --max-time 10 \
        >> "$LOGFILE" 2>&1 &
}

# --- Notification: skip (system events, no TTS content) ---
if [ "$HOOK_EVENT" = "Notification" ]; then
    echo "$(date): Notification event, skipping" >> "$LOGFILE"
    exit 0
fi

# --- PreToolUse: immediate mode early TTS ---
if [ "$HOOK_EVENT" = "PreToolUse" ]; then
    if [ "$TTS_TIMING" != "immediate" ]; then
        exit 0
    fi
    if [ -f "$TTS_PLAYED_FLAG" ]; then
        exit 0
    fi

    TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")
    if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
        exit 0
    fi

    TTS_OUTPUT=$(extract_tts_from_transcript "$TRANSCRIPT_PATH")
    TTS_TEXT="${TTS_OUTPUT%%|||*}"
    TTS_MOOD="${TTS_OUTPUT##*|||}"

    if [ -n "$TTS_TEXT" ]; then
        # Skip if this is the same TTS we already played (stale transcript)
        NEW_HASH=$(echo -n "$TTS_TEXT" | md5_hash)
        OLD_HASH=""
        [ -f "$TTS_LAST_HASH" ] && OLD_HASH=$(cat "$TTS_LAST_HASH")
        if [ "$NEW_HASH" = "$OLD_HASH" ]; then
            echo "$(date): immediate TTS: skipping stale duplicate" >> "$LOGFILE"
            exit 0
        fi
        echo "$(date): immediate TTS: ${TTS_TEXT:0:80}... mood=$TTS_MOOD" >> "$LOGFILE"
        touch "$TTS_PLAYED_FLAG"
        echo "$NEW_HASH" > "$TTS_LAST_HASH"
        fire_tts "$TTS_TEXT" "$TTS_MOOD"
    fi
    exit 0
fi

# --- Stop in immediate mode: check for NEW final TTS ---
if [ "$HOOK_EVENT" = "Stop" ] && [ "$TTS_TIMING" = "immediate" ]; then
    rm -f "$TTS_PLAYED_FLAG"

    RESPONSE=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('last_assistant_message', '')[:5000])
" 2>/dev/null || echo "")

    TTS_OUTPUT=$(extract_tts_from_text "$RESPONSE")
    TTS_TEXT="${TTS_OUTPUT%%|||*}"
    TTS_MOOD="${TTS_OUTPUT##*|||}"

    if [ -n "$TTS_TEXT" ]; then
        echo "$(date): immediate Stop: final TTS: ${TTS_TEXT:0:80}... mood=$TTS_MOOD" >> "$LOGFILE"
        echo -n "$TTS_TEXT" | md5_hash > "$TTS_LAST_HASH"
        fire_tts "$TTS_TEXT" "$TTS_MOOD"
    else
        echo "$(date): immediate Stop: no TTS in final message, skipping" >> "$LOGFILE"
    fi
    exit 0
fi

# --- Stop / Notification / PermissionRequest (on-complete mode) ---

RESPONSE=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
msg = data.get('message', '') or data.get('title', '') or data.get('last_assistant_message', '') or ''
print(msg[:5000])
" 2>/dev/null || echo "")

TTS_OUTPUT=$(extract_tts_from_text "$RESPONSE")
TTS_TEXT="${TTS_OUTPUT%%|||*}"
TTS_MOOD="${TTS_OUTPUT##*|||}"

# If not found, try transcript (fixes multi-tool-call turns)
if [ -z "$TTS_TEXT" ] && [ "$HOOK_EVENT" = "Stop" ]; then
    TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")
    if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
        TTS_OUTPUT=$(extract_tts_from_transcript "$TRANSCRIPT_PATH")
        TTS_TEXT="${TTS_OUTPUT%%|||*}"
        TTS_MOOD="${TTS_OUTPUT##*|||}"
        if [ -n "$TTS_TEXT" ]; then
            echo "$(date): TTS found via transcript fallback" >> "$LOGFILE"
        fi
    fi
fi

echo "$(date): tts_text=${TTS_TEXT:0:80}..." >> "$LOGFILE"

if [ -z "$TTS_TEXT" ]; then
    echo "$(date): No <tts> tag found, playing random alert" >> "$LOGFILE"
    curl -s -X POST "$CORTANA_TTS_SERVER/alert" \
        --connect-timeout 2 \
        --max-time 10 \
        >> "$LOGFILE" 2>&1 &
    exit 0
fi

echo -n "$TTS_TEXT" | md5_hash > "$TTS_LAST_HASH"
fire_tts "$TTS_TEXT" "$TTS_MOOD"

rm -f "$TTS_PLAYED_FLAG"
exit 0
