# cortana-tts copilot wrapper
# Wraps `gh copilot` to speak responses via cortana-tts.
# Source this file or append it to ~/.zshrc / ~/.bashrc.
# Install automatically via: cortana-tts install copilot

cortana_tts_copilot() {
  local CORTANA_TTS_URL="${CORTANA_TTS_SERVER:-http://127.0.0.1:5111}"
  local tmpfile
  tmpfile=$(mktemp)

  # `gh copilot` is a TUI — it needs a real PTY to run interactively.
  # Use `script` to record output to a file while keeping the PTY intact.
  # macOS: script -q <file> <cmd...>
  # Linux: script -q -c '<cmd>' <file>   (or script -q --command '<cmd>' <file>)
  if [ "$(uname)" = "Darwin" ]; then
    script -q "$tmpfile" command gh copilot "$@"
  else
    script -q -c "gh copilot $(printf '%q ' "$@")" "$tmpfile"
  fi
  local exit_code=$?

  # Strip ANSI escape codes, extract meaningful response lines, speak them.
  local speak_text
  speak_text=$(
    cat "$tmpfile" \
    | python3 -c "
import sys, re
text = sys.stdin.read()
# Strip ANSI escape sequences
text = re.sub(r'\x1b\[[0-9;]*[mABCDEFGHJKSTfnsu]', '', text)
text = re.sub(r'\x1b\].*?\x07', '', text)
text = re.sub(r'\r', '', text)
lines = text.splitlines()
# Keep non-empty lines that aren't UI prompts (? / # / spinner chars)
kept = [l for l in lines if l.strip() and not l.strip().startswith(('?', '#', '\u280b', '\u2819', '\u2839', '\u2838'))]
# Take the last meaningful block (typically the model's answer)
print('\n'.join(kept[-10:]))
" 2>/dev/null
  )
  rm -f "$tmpfile"

  if [ -n "$speak_text" ] && [ $exit_code -eq 0 ]; then
    local json_text
    json_text=$(printf '%s' "$speak_text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    curl -s -X POST "$CORTANA_TTS_URL/speak" \
      -H "Content-Type: application/json" \
      -d "{\"text\": $json_text}" \
      --connect-timeout 2 \
      --max-time 30 &
  fi

  return $exit_code
}

_cortana_tts_gh_wrapper() {
  if [ "$1" = "copilot" ]; then
    cortana_tts_copilot "${@:2}"
  else
    command gh "$@"
  fi
}

alias gh='_cortana_tts_gh_wrapper'
