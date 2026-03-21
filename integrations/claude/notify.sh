#!/usr/bin/env bash
# integrations/claude/notify.sh — cortana-tts hook for Claude Code
# Extracts <tts> tags from Claude's response and sends to TTS server.
# Supports type="confirm|update|end" with per-type toggles.
#
# Install via: cortana-tts install claude

set -euo pipefail

CORTANA_TTS_SERVER="${CORTANA_TTS_SERVER:-http://127.0.0.1:5111}"
if [ -d "$HOME/Library/Logs" ]; then
    LOGFILE="$HOME/Library/Logs/cortana-tts-hook.log"
else
    LOGFILE="${XDG_STATE_HOME:-$HOME/.local/state}/cortana-tts-hook.log"
fi
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/cortana-tts"
CORTANA_TTS_RUNTIME="${XDG_RUNTIME_DIR:-${TMPDIR:-/tmp}}/cortana-tts-$(id -u)"
mkdir -p "$CORTANA_TTS_RUNTIME"
TTS_PLAYED_FLAG="$CORTANA_TTS_RUNTIME/tts_played"
TTS_LAST_HASH="$CORTANA_TTS_RUNTIME/tts_last_hash"

# Portable md5
md5_hash() { command -v md5 >/dev/null 2>&1 && md5 -q || md5sum | cut -d' ' -f1; }

# Read a config file with a default value
read_config() { local f="$CONFIG_DIR/$1"; [ -f "$f" ] && cat "$f" || echo "$2"; }

# Check if a message type is enabled
type_enabled() {
    local type="$1"
    case "$type" in
        confirm)  [ "$(read_config messaging_confirm off)" = "on" ] ;;
        update)   [ "$(read_config messaging_updates off)" = "on" ] ;;
        end|*)    [ "$(read_config messaging_end on)" = "on" ] ;;
    esac
}

INPUT=$(cat)
HOOK_EVENT=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('hook_event_name','unknown'))" 2>/dev/null || echo "unknown")

echo "$(date +%H:%M:%S.%3N): [cortana-tts] hook entry event=$HOOK_EVENT" >> "$LOGFILE"

TTS_TIMING=$(read_config tts_timing on-complete)

# Extract ALL <tts> tags from text. Outputs lines of: text|||mood|||type
extract_tags_from_text() {
    echo "$1" | python3 -c "
import sys, re
text = sys.stdin.read()
pattern = r'(?:<!--\s*)?<tts([^>]*)>(.*?)</tts>(?:\s*-->)?'
for m in re.finditer(pattern, text, re.DOTALL):
    attrs = m.group(1)
    content = m.group(2).strip()
    if not content:
        continue
    mood_m = re.search(r'mood=\"([^\"]*)\"', attrs)
    type_m = re.search(r'type=\"([^\"]*)\"', attrs)
    mood = mood_m.group(1) if mood_m else ''
    tts_type = type_m.group(1) if type_m else 'end'
    print(content + '|||' + mood + '|||' + tts_type)
" 2>/dev/null || true
}

# Extract ALL <tts> tags from transcript JSONL (current turn only)
extract_tags_from_transcript() {
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

# Only extract from the LAST assistant message to avoid replaying old turns
last_assistant = None
for obj in reversed(entries):
    msg = obj.get('message', {})
    if isinstance(msg, dict) and msg.get('role') == 'assistant':
        last_assistant = msg
        break

pattern = r'(?:<!--\s*)?<tts([^>]*)>(.*?)</tts>(?:\s*-->)?'
if last_assistant:
    for block in (last_assistant.get('content', []) or []):
        if isinstance(block, dict) and block.get('type') == 'text':
            text = block.get('text', '')
            for m in re.finditer(pattern, text, re.DOTALL):
                attrs = m.group(1)
                content_text = m.group(2).strip()
                if not content_text:
                    continue
                mood_m = re.search(r'mood=\"([^\"]*)\"', attrs)
                type_m = re.search(r'type=\"([^\"]*)\"', attrs)
                mood = mood_m.group(1) if mood_m else ''
                tts_type = type_m.group(1) if type_m else 'end'
                print(content_text + '|||' + mood + '|||' + tts_type)
" "$transcript_path" 2>/dev/null || true
}

# Fire one TTS request (async background)
fire_tts() {
    local tts_text="$1"
    local mood="$2"
    local mood_json=""
    if [ -n "$mood" ]; then
        case "$mood" in
            error|success|warn|melancholy) ;;
            *) mood="" ;;
        esac
        [ -n "$mood" ] && mood_json=", \"mood\": \"${mood}\""
    fi
    echo "$(date +%H:%M:%S.%3N): [cortana-tts] POST /speak mood=${mood:-none}" >> "$LOGFILE"
    curl -s -X POST "$CORTANA_TTS_SERVER/speak" \
        -H "Content-Type: application/json" \
        -d "{\"text\": $(echo "$tts_text" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))')$mood_json}" \
        --connect-timeout 2 \
        --max-time 10 \
        >> "$LOGFILE" 2>&1 &
}

# Speak all matching tags. $1=lines(text|||mood|||type), $2=type filter (empty=all)
# Prints count of tags spoken.
speak_tags() {
    local tags="$1"
    local only_type="${2:-}"
    local spoken=0
    while IFS= read -r line; do
        [ -z "$line" ] && continue
        local text="${line%%|||*}"
        local rest="${line#*|||}"
        local mood="${rest%%|||*}"
        local tts_type="${rest##*|||}"
        [ -z "$text" ] && continue
        if [ -n "$only_type" ] && [ "$tts_type" != "$only_type" ]; then
            continue
        fi
        if ! type_enabled "$tts_type"; then
            echo "$(date): skipping type=$tts_type (disabled)" >> "$LOGFILE"
            continue
        fi
        echo "$(date): speaking type=$tts_type mood=${mood:-none}: ${text:0:80}" >> "$LOGFILE"
        fire_tts "$text" "$mood"
        spoken=$((spoken + 1))
        # Small gap between sequential tags so the server pipeline can queue them
        [ $spoken -gt 1 ] && sleep 0.15
    done <<< "$tags"
    echo "$spoken"
}

# --- Skip Notification events ---
if [ "$HOOK_EVENT" = "Notification" ]; then
    exit 0
fi

# --- PreToolUse: speak confirm tags (and immediate-mode end tags) ---
if [ "$HOOK_EVENT" = "PreToolUse" ]; then
    CONFIRM_ENABLED=$(read_config messaging_confirm off)
    IMMEDIATE=$( [ "$TTS_TIMING" = "immediate" ] && echo "yes" || echo "no" )

    if [ "$CONFIRM_ENABLED" != "on" ] && [ "$IMMEDIATE" != "yes" ]; then
        exit 0
    fi
    if [ -f "$TTS_PLAYED_FLAG" ]; then
        exit 0
    fi

    TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")
    if [ -z "$TRANSCRIPT_PATH" ] || [ ! -f "$TRANSCRIPT_PATH" ]; then
        exit 0
    fi

    TAGS=$(extract_tags_from_transcript "$TRANSCRIPT_PATH")
    if [ -z "$TAGS" ]; then
        exit 0
    fi

    NEW_HASH=$(echo -n "$TAGS" | md5_hash)
    OLD_HASH=""
    [ -f "$TTS_LAST_HASH" ] && OLD_HASH=$(cat "$TTS_LAST_HASH")
    if [ "$NEW_HASH" = "$OLD_HASH" ]; then
        exit 0
    fi

    SPOKE=0
    if [ "$CONFIRM_ENABLED" = "on" ]; then
        COUNT=$(speak_tags "$TAGS" "confirm")
        SPOKE=$((SPOKE + COUNT))
    fi
    if [ "$IMMEDIATE" = "yes" ]; then
        COUNT=$(speak_tags "$TAGS" "end")
        SPOKE=$((SPOKE + COUNT))
        COUNT=$(speak_tags "$TAGS" "update")
        SPOKE=$((SPOKE + COUNT))
    fi
    if [ "$SPOKE" -gt 0 ]; then
        touch "$TTS_PLAYED_FLAG"
        echo "$NEW_HASH" > "$TTS_LAST_HASH"
    fi
    exit 0
fi

# --- Stop in immediate mode: speak final end tags if new ---
if [ "$HOOK_EVENT" = "Stop" ] && [ "$TTS_TIMING" = "immediate" ]; then
    rm -f "$TTS_PLAYED_FLAG"
    RESPONSE=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
print(data.get('last_assistant_message', '')[:5000])
" 2>/dev/null || echo "")
    TAGS=$(extract_tags_from_text "$RESPONSE")
    if [ -n "$TAGS" ]; then
        NEW_HASH=$(echo -n "$TAGS" | md5_hash)
        OLD_HASH=""
        [ -f "$TTS_LAST_HASH" ] && OLD_HASH=$(cat "$TTS_LAST_HASH")
        if [ "$NEW_HASH" != "$OLD_HASH" ]; then
            echo "$NEW_HASH" > "$TTS_LAST_HASH"
            speak_tags "$TAGS" "" > /dev/null
        fi
    fi
    exit 0
fi

# --- Stop (on-complete): speak all enabled tags in order ---
RESPONSE=$(echo "$INPUT" | python3 -c "
import sys, json
data = json.load(sys.stdin)
msg = data.get('message', '') or data.get('title', '') or data.get('last_assistant_message', '') or ''
print(msg[:5000])
" 2>/dev/null || echo "")

TAGS=$(extract_tags_from_text "$RESPONSE")

# Transcript fallback for multi-tool-call turns
if [ -z "$TAGS" ] && [ "$HOOK_EVENT" = "Stop" ]; then
    TRANSCRIPT_PATH=$(echo "$INPUT" | python3 -c "import sys,json; print(json.load(sys.stdin).get('transcript_path',''))" 2>/dev/null || echo "")
    if [ -n "$TRANSCRIPT_PATH" ] && [ -f "$TRANSCRIPT_PATH" ]; then
        TAGS=$(extract_tags_from_transcript "$TRANSCRIPT_PATH")
        [ -n "$TAGS" ] && echo "$(date): TTS found via transcript fallback" >> "$LOGFILE"
    fi
fi

if [ -z "$TAGS" ]; then
    echo "$(date): No <tts> tags found, playing random alert" >> "$LOGFILE"
    curl -s -X POST "$CORTANA_TTS_SERVER/alert" \
        --connect-timeout 2 --max-time 10 \
        >> "$LOGFILE" 2>&1 &
    exit 0
fi

echo -n "$TAGS" | md5_hash > "$TTS_LAST_HASH"
speak_tags "$TAGS" "" > /dev/null
rm -f "$TTS_PLAYED_FLAG"
exit 0
