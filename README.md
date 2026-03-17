# cortana-tts

A standalone, pip-installable local TTS server with ready-made integrations for Claude Code, OpenCode, and GitHub Copilot CLI.

Speaks assistant responses aloud in real time using a fast, on-device neural TTS model — no cloud, no API keys.

---

## Requirements

| Engine | Python version |
|---|---|
| Lightweight (piper-tts) | Python 3.10+ |
| Standard (neural) | Python 3.10–3.12 (PyTorch does not yet support 3.13+) |

**Linux** also requires PortAudio:
```bash
sudo apt install libportaudio2   # Debian/Ubuntu
sudo pacman -S portaudio          # Arch
brew install portaudio            # macOS (if not auto-bundled)
```

**Windows** requires the [Visual C++ Redistributable](https://aka.ms/vs/17/release/vc_redist.x64.exe).

---

## Install

```bash
pip install cortana-tts
```

---

## Engine options

| Engine | Install size | Voices | Requires |
|---|---|---|---|
| Standard (default) | ~1.5 GB | 22 neural voices | PyTorch + HuggingFace |
| Lightweight | ~80 MB | 5 English voices | Nothing extra |

The setup wizard will ask which you prefer on first run. You can also switch at any time:

```bash
cortana-tts engine                    # show current engine
cortana-tts engine standard           # switch to standard (kokoro)
cortana-tts engine lightweight        # switch to lightweight (piper-tts)
cortana-tts engine lightweight en_US-ryan-medium  # switch with specific voice
```

Available lightweight voices: `en_US-lessac-medium`, `en_US-ryan-medium`, `en_US-amy-medium`, `en_GB-alba-medium`, `en_GB-northern_english_male-medium`

---

## Quick start

```bash
# Start the server (foreground)
cortana-tts start

# Start in background
cortana-tts start --bg

# Test speech
cortana-tts speak "Hello. The server is running."

# Check status
cortana-tts status

# Stop background server
cortana-tts stop
```

The server listens on `http://127.0.0.1:5111` by default.

---

## Integrations

### Claude Code

Installs `notify.sh` (or `.ps1` on Windows) and `status.sh` into `~/.config/cortana-tts/hooks/`, then registers them as Claude Code hooks in `~/.claude/settings.json`.

```bash
cortana-tts install claude
```

Claude Code must be restarted to pick up the new hooks. After that, every response containing a `<!-- <tts>...</tts> -->` tag will be spoken aloud automatically.

**Uninstall:**

```bash
cortana-tts uninstall claude
```

### OpenCode

Copies `integrations/opencode/index.ts` to `~/.config/opencode/plugins/cortana-tts.ts` and registers it in `~/.config/opencode/opencode.json`.

```bash
cortana-tts install opencode
```

**Uninstall:**

```bash
cortana-tts uninstall opencode
```

### GitHub Copilot CLI

Appends a shell function to `~/.zshrc` and `~/.bashrc` (or PowerShell profile on Windows) that wraps `gh copilot` and speaks the response.

```bash
cortana-tts install copilot
source ~/.zshrc   # or restart your shell
```

**Uninstall:**

```bash
cortana-tts uninstall copilot
```

---

## Voices

22 voices are available. Switch at any time — the new voice takes effect immediately.

```bash
cortana-tts voice list
cortana-tts voice set af_heart
```

| Category | Voices |
|---|---|
| American female | af_heart, af_bella, af_nicole, af_sarah, af_sky, af_aoede, af_kore, af_stella, af_jessica, af_river |
| American male | am_adam, am_eric, am_liam, am_michael, am_puck |
| British female | bf_isabella, bf_alice, bf_emma, bf_lily |
| British male | bm_daniel, bm_george, bm_lewis |

---

## Configuration

Config is read from `~/.config/cortana-tts/.env` (or the path in `$CORTANA_TTS_CONFIG`).

Copy the example and edit:

```bash
cp .env.example ~/.config/cortana-tts/.env
```

Key settings:

| Variable | Default | Description |
|---|---|---|
| `TTS_ENGINE` | `standard` | `standard` (kokoro) or `piper` (lightweight) |
| `TTS_VOICE` | `af_heart` | Active voice (standard engine) |
| `TTS_PIPER_VOICE` | `en_US-lessac-medium` | Active voice (piper engine) |
| `TTS_PORT` | `5111` | Server port |
| `TTS_SPEED` | `1.1` | Speech speed multiplier |
| `CORTANA_TTS_SERVER` | `http://127.0.0.1:5111` | Server URL (for hook scripts) |
| `ALERT_CACHE_DIR` | `~/.config/cortana-tts/alert_cache` | Pre-generated audio cue cache |
| `TTS_DEBUG_DUMP` | `0` | Set to `1` to save WAV files to `~/Desktop/tts_debug/` |

---

## API endpoints

The server exposes these HTTP endpoints:

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Health check + uptime |
| POST | `/speak` | Speak text `{"text": "...", "mood": "error"}` |
| POST | `/stop` | Stop current playback |
| POST | `/alert` | Play a random pre-cached alert |
| POST | `/status` | Set state `{"state": "thinking"\|"idle"}` |
| POST | `/mute` | Toggle mute `{"muted": true}` |
| POST | `/voice` | Switch voice `{"voice": "af_heart"}` |
| POST | `/playback-mode` | Set mode `{"mode": "chunked"\|"full"}` |
| POST | `/voice-cue-mode` | Set cue frequency `{"mode": "off"\|"once"\|"15s"\|"30s"\|"always"}` |
| WS | `/ws/status` | JSON state/amplitude stream |
| WS | `/ws/audio` | Base64 PCM audio stream |

---

## TTS tag format

For Claude Code integration, responses should include a hidden TTS summary:

```
<!-- <tts>Your spoken summary here.</tts> -->
<!-- <tts mood="error">Something went wrong.</tts> -->
```

Valid moods: `error`, `success`, `warn`, `melancholy` (omit for default).

---

## Platform notes

**Linux:** Requires `libportaudio2`. Install with `sudo apt install libportaudio2`.

**macOS:** PortAudio is bundled with sounddevice wheels. No extra steps needed.

**Windows:** Requires Visual C++ Redistributable 2015–2022. Hook scripts use PowerShell (`.ps1`). The `cortana-tts install claude` command automatically selects the correct scripts.

---

## PID file location

| Platform | Path |
|---|---|
| Linux / macOS | `~/.local/state/cortana-tts/server.pid` |
| Windows | `%LOCALAPPDATA%\cortana-tts\server.pid` |

---

## CLI reference

```
cortana-tts start [--port 5111] [--voice af_heart] [--bg]
cortana-tts stop
cortana-tts restart
cortana-tts status
cortana-tts voice list
cortana-tts voice set <name>
cortana-tts speak "<text>"
cortana-tts engine
cortana-tts engine standard
cortana-tts engine lightweight [voice]
cortana-tts install claude|opencode|copilot
cortana-tts uninstall claude|opencode|copilot
```
