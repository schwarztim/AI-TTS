# cortana-tts copilot wrapper
# Wraps `gh copilot` to speak responses via cortana-tts.
# Source this file or append it to ~/.zshrc / ~/.bashrc.
# Install automatically via: cortana-tts install copilot

cortana_tts_copilot() {
  local CORTANA_TTS_URL="${CORTANA_TTS_SERVER:-http://127.0.0.1:5111}"
  local output
  output=$(command gh copilot "$@" 2>&1)
  echo "$output"
  # Extract the actual response lines — skip UI prompts and blank lines
  local speak_text
  speak_text=$(echo "$output" | grep -v "^?" | grep -v "^#" | grep -v "^$" | tail -20 | head -5)
  if [ -n "$speak_text" ]; then
    local json_text
    json_text=$(printf '%s' "$speak_text" | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read()))')
    curl -s -X POST "$CORTANA_TTS_URL/speak" \
      -H "Content-Type: application/json" \
      -d "{\"text\": $json_text}" \
      --connect-timeout 2 \
      --max-time 30 &
  fi
}

_cortana_tts_gh_wrapper() {
  if [ "$1" = "copilot" ]; then
    cortana_tts_copilot "${@:2}"
  else
    command gh "$@"
  fi
}

alias gh='_cortana_tts_gh_wrapper'
