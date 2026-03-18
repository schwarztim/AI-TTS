# cortana-tts copilot wrapper
# Wraps `gh copilot` to speak responses via cortana-tts.
# Source this file or append it to ~/.zshrc / ~/.bashrc.
# Install automatically via: cortana-tts install copilot

cortana_tts_copilot() {
  local WATCHER="$HOME/.config/cortana-tts/copilot-watcher.py"
  if [ -f "$WATCHER" ]; then
    local ts
    ts=$(python3 -c "import time; print(time.time())")
    ( python3 "$WATCHER" \
        --tts-url "${CORTANA_TTS_SERVER:-http://127.0.0.1:5111}" \
        --started-after "$ts" </dev/null >/dev/null 2>&1 & )
  fi
  command gh copilot "$@"
}

# Non-interactive mode (ghc "what is 2+2")
ghc() {
  local CORTANA_TTS_URL="${CORTANA_TTS_SERVER:-http://127.0.0.1:5111}"
  local output
  output=$(command gh copilot -t "$@" 2>&1)
  local exit_code=$?
  printf '%s\n' "$output"

  if [ -n "$output" ] && [ $exit_code -eq 0 ]; then
    local json_text
    json_text=$(printf '%s' "$output" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    curl -s -X POST "$CORTANA_TTS_URL/speak" \
      -H "Content-Type: application/json" \
      -d "{\"text\": $json_text}" \
      --connect-timeout 2 \
      --max-time 30 >/dev/null 2>&1 &
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
