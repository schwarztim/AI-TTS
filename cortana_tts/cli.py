"""cortana-tts CLI — manage the local TTS server and integrations."""

import json
import os
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import click
import requests

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CORTANA_TTS_URL_DEFAULT = "http://127.0.0.1:5111"

VOICES = [
    # American female
    "af_heart", "af_bella", "af_nicole", "af_sarah", "af_sky",
    "af_aoede", "af_kore", "af_stella", "af_jessica", "af_river",
    # American male
    "am_adam", "am_eric", "am_liam", "am_michael", "am_puck",
    # British female
    "bf_isabella", "bf_alice", "bf_emma", "bf_lily",
    # British male
    "bm_daniel", "bm_george", "bm_lewis",
]


def _server_url() -> str:
    return os.environ.get("CORTANA_TTS_SERVER", CORTANA_TTS_URL_DEFAULT)


def _pid_file() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / "cortana-tts" / "server.pid"
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return base / "cortana-tts" / "server.pid"


def _config_dir() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / "cortana-tts"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        return base / "cortana-tts"


def _hooks_dir() -> Path:
    return _config_dir() / "hooks"


def _is_running() -> bool:
    """Return True if the server responds to /health."""
    try:
        r = requests.get(f"{_server_url()}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _read_pid() -> Optional[int]:
    pid_path = _pid_file()
    if pid_path.exists():
        try:
            return int(pid_path.read_text().strip())
        except Exception:
            pass
    return None


def _kill_pid(pid: int):
    try:
        if platform.system() == "Windows":
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], check=False, capture_output=True)
        else:
            os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass


# ---------------------------------------------------------------------------
# Package data: locate bundled integration files
# ---------------------------------------------------------------------------

def _package_root() -> Path:
    """Return the root of the installed cortana-tts package (parent of cortana_tts/)."""
    return Path(__file__).resolve().parent.parent


def _integration_path(rel: str) -> Path:
    return _package_root() / "integrations" / rel


# ---------------------------------------------------------------------------
# CLI root
# ---------------------------------------------------------------------------

@click.group()
def main():
    """cortana-tts — Local neural TTS server with Claude Code, OpenCode, and Copilot integrations."""
    pass


# ---------------------------------------------------------------------------
# Server management
# ---------------------------------------------------------------------------

def _model_cached() -> bool:
    """Return True if either the standard or lightweight model is already set up."""
    # Check standard (kokoro / HuggingFace)
    hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
    if hf_cache.exists() and any(hf_cache.glob("models--hexgrad*")):
        return True
    # Check lightweight (piper voices)
    piper_cache = _config_dir() / "piper-voices"
    if piper_cache.exists() and any(piper_cache.glob("*.onnx")):
        return True
    # Check if engine preference was already saved (user ran wizard before)
    env_path = _config_dir() / ".env"
    if env_path.exists():
        content = env_path.read_text()
        if "TTS_ENGINE=" in content:
            return True
    return False


def _save_env_var(key: str, value: str):
    """Write or update a KEY=value line in ~/.config/cortana-tts/.env."""
    env_path = _config_dir() / ".env"
    env_path.parent.mkdir(parents=True, exist_ok=True)
    existing = env_path.read_text() if env_path.exists() else ""
    lines = existing.splitlines()
    new_lines = []
    found = False
    for line in lines:
        if line.startswith(f"{key}=") or line.startswith(f"{key} ="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n")


def _run_setup_wizard(voice: str):
    """First-run wizard: ask engine preference, download models, save config."""
    click.echo("")
    click.echo("╔══════════════════════════════════════════╗")
    click.echo("║        cortana-tts  first-time setup     ║")
    click.echo("╚══════════════════════════════════════════╝")
    click.echo("")
    click.echo("Which TTS engine would you like to use?")
    click.echo("")
    click.echo("  1) Standard    — 22 voices, higher quality, ~1.5 GB download (PyTorch + model)")
    click.echo("  2) Lightweight — faster setup, ~80 MB, no account needed (piper-tts / ONNX)")
    click.echo("")
    engine_choice = click.prompt("Choice [1/2]", default="1").strip()

    if engine_choice == "2":
        _run_setup_wizard_lightweight()
    else:
        _run_setup_wizard_standard(voice)


def _run_setup_wizard_lightweight():
    """Set up lightweight piper-tts engine."""
    try:
        import piper  # noqa: F401
    except ImportError:
        click.echo(
            "\nError: piper-tts is not installed in this Python environment.\n"
            f"  Python: {sys.executable}\n\n"
            "Fix: install piper-tts into this environment:\n"
            f"  {sys.executable} -m pip install piper-tts\n\n"
            "Tip: if you have multiple Python installs (Homebrew, system, pyenv),\n"
            "make sure piper-tts is installed into the same Python that runs cortana-tts.\n",
            err=True,
        )
        return False

    from cortana_tts.piper_engine import PIPER_VOICES, _model_paths

    click.echo("")
    click.echo("Setting up lightweight piper-tts engine...")
    click.echo("")
    click.echo("Available voices:")
    for i, v in enumerate(PIPER_VOICES, 1):
        click.echo(f"  {i}) {v}")
    click.echo("")

    piper_voice = "en_US-hfc_female-medium"
    click.echo(f"Using default voice: {piper_voice}")
    click.echo("(You can change this later with: cortana-tts engine lightweight <voice>)")
    click.echo("")
    click.echo(f"Downloading voice model for '{piper_voice}'...")
    click.echo("(~80 MB — only happens once)")
    click.echo("")

    try:
        _model_paths(piper_voice)
        click.echo("")
        click.echo("✓ Piper voice downloaded and ready.")
        click.echo("")
    except Exception as e:
        click.echo(f"Warning: voice download failed: {e}", err=True)
        click.echo("The server will attempt to download on first use.", err=True)

    _save_env_var("TTS_ENGINE", "piper")
    _save_env_var("TTS_PIPER_VOICE", piper_voice)
    click.echo("Engine saved to ~/.config/cortana-tts/.env")
    click.echo("")


def _run_setup_wizard_standard(voice: str):
    """Set up standard kokoro engine."""
    click.echo("")
    click.echo("The TTS model needs to be downloaded once (~326 MB).")
    click.echo("This is stored locally — no data ever leaves your machine.")
    click.echo("")

    # Optional HuggingFace token — not required, just faster
    hf_token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not hf_token:
        click.echo("HuggingFace token (press Enter to skip — download will still work, just slower):")
        hf_token = click.prompt("  Token", default="", show_default=False).strip() or None
        if hf_token:
            _save_env_var("HF_TOKEN", hf_token)
            click.echo("  Token saved to ~/.config/cortana-tts/.env")
        else:
            click.echo("  Skipping — downloading without token (this is fine, may be slower).")

    click.echo("")
    click.echo(f"Downloading model and warming up voice '{voice}'...")
    click.echo("(This may take a minute on slow connections — only happens once)")
    click.echo("")

    env = os.environ.copy()
    if hf_token:
        env["HF_TOKEN"] = hf_token
    env["TTS_VOICE"] = voice

    # Warm up by importing and running a tiny inference — downloads model weights
    warmup_script = (
        "from cortana_tts.tts_engine import TTSEngine; "
        "import os; "
        "e = TTSEngine(voice=os.environ.get('TTS_VOICE','af_heart')); "
        "list(e.generate_stream('Ready.')); "
        "print('Model ready.')"
    )
    result = subprocess.run([sys.executable, "-c", warmup_script], env=env)
    if result.returncode != 0:
        click.echo("Warning: model warmup failed. The server will still attempt to start.", err=True)
    else:
        click.echo("")
        click.echo("✓ Model downloaded and ready.")
        click.echo("")

    _save_env_var("TTS_ENGINE", "standard")


@main.command("start")
@click.option("--port", default=5111, show_default=True, help="Port to listen on.")
@click.option("--voice", default="af_heart", show_default=True, help="Voice to use.")
@click.option("--bg", is_flag=True, default=False, help="Start server in background.")
def cmd_start(port: int, voice: str, bg: bool):
    """Start the cortana-tts server."""
    if _is_running():
        click.echo("cortana-tts server is already running.")
        return

    # First-run: model not yet downloaded
    if not _model_cached():
        _run_setup_wizard(voice)

    env = os.environ.copy()
    env["TTS_PORT"] = str(port)
    env["TTS_VOICE"] = voice

    if bg:
        pid_path = _pid_file()
        pid_path.parent.mkdir(parents=True, exist_ok=True)

        log_dir = _config_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / "server.log"

        with open(log_path, "a") as log_f:
            proc = subprocess.Popen(
                [sys.executable, "-m", "cortana_tts.server"],
                env=env,
                stdout=log_f,
                stderr=log_f,
                start_new_session=True,
            )
        pid_path.write_text(str(proc.pid))
        click.echo(f"cortana-tts server started in background (PID {proc.pid}, port {port})")
        click.echo(f"Log: {log_path}")

        # Wait briefly to confirm startup
        for _ in range(10):
            time.sleep(0.5)
            if _is_running():
                click.echo("Server is up.")
                return
        click.echo("Warning: server did not respond to /health within 5s. Check the log.")
    else:
        click.echo(f"Starting cortana-tts server on port {port} (voice: {voice})...")
        subprocess.run([sys.executable, "-m", "cortana_tts.server"], env=env)


@main.command("stop")
def cmd_stop():
    """Stop the background cortana-tts server."""
    pid = _read_pid()
    if pid:
        _kill_pid(pid)
        _pid_file().unlink(missing_ok=True)
        click.echo(f"Stopped server (PID {pid}).")
    else:
        click.echo("No PID file found. Attempting /stop endpoint...")
        try:
            requests.post(f"{_server_url()}/stop", timeout=2)
        except Exception:
            pass
        click.echo("Done.")


@main.command("restart")
@click.pass_context
def cmd_restart(ctx):
    """Restart the background cortana-tts server."""
    ctx.invoke(cmd_stop)
    time.sleep(1)
    ctx.invoke(cmd_start, port=5111, voice="af_heart", bg=True)


@main.command("status")
def cmd_status():
    """Show server status and current voice."""
    if _is_running():
        try:
            r = requests.get(f"{_server_url()}/health", timeout=2)
            data = r.json()
            click.echo(f"running  uptime={data.get('uptime_seconds', '?')}s  url={_server_url()}")
        except Exception:
            click.echo("running (health parse failed)")
    else:
        click.echo("stopped")


# ---------------------------------------------------------------------------
# Voice
# ---------------------------------------------------------------------------

@main.group("voice")
def cmd_voice():
    """List or switch voices."""
    pass


@cmd_voice.command("list")
def voice_list():
    """List all available voices."""
    click.echo("Available voices:")
    categories = [
        ("American female", [v for v in VOICES if v.startswith("af_")]),
        ("American male",   [v for v in VOICES if v.startswith("am_")]),
        ("British female",  [v for v in VOICES if v.startswith("bf_")]),
        ("British male",    [v for v in VOICES if v.startswith("bm_")]),
    ]
    for label, voices in categories:
        click.echo(f"\n  {label}:")
        for v in voices:
            click.echo(f"    {v}")


@cmd_voice.command("set")
@click.argument("name")
def voice_set(name: str):
    """Switch the active voice."""
    if name not in VOICES:
        click.echo(f"Unknown voice: {name}. Run 'cortana-tts voice list' to see options.", err=True)
        sys.exit(1)
    try:
        r = requests.post(
            f"{_server_url()}/voice",
            json={"voice": name},
            timeout=5,
        )
        click.echo(f"Voice set to {r.json().get('voice', name)}")
    except Exception as e:
        click.echo(f"Failed to set voice: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Speak
# ---------------------------------------------------------------------------

@main.command("speak")
@click.argument("text")
def cmd_speak(text: str):
    """Send a one-shot TTS request to the server."""
    try:
        r = requests.post(
            f"{_server_url()}/speak",
            json={"text": text},
            timeout=10,
        )
        click.echo(r.json())
    except Exception as e:
        click.echo(f"Failed: {e}", err=True)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------

@main.group("install")
def cmd_install():
    """Install integrations for Claude Code, OpenCode, or Copilot."""
    pass


@cmd_install.command("claude")
def install_claude():
    """Install Claude Code hooks (notify.sh + status.sh)."""
    hooks_dst = _hooks_dir()
    hooks_dst.mkdir(parents=True, exist_ok=True)

    is_windows = platform.system() == "Windows"

    if is_windows:
        scripts = [
            ("claude/notify.ps1", "notify.ps1"),
            ("claude/status.ps1", "status.ps1"),
        ]
    else:
        scripts = [
            ("claude/notify.sh", "notify.sh"),
            ("claude/status.sh", "status.sh"),
        ]

    for src_rel, dst_name in scripts:
        src = _integration_path(src_rel)
        dst = hooks_dst / dst_name
        if not src.exists():
            click.echo(f"Warning: source not found: {src}", err=True)
            continue
        shutil.copy2(src, dst)
        if not is_windows:
            dst.chmod(0o755)
        click.echo(f"Installed: {dst}")

    # Update ~/.claude/settings.json
    settings_path = Path.home() / ".claude" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)

    settings = {}
    if settings_path.exists():
        try:
            settings = json.loads(settings_path.read_text())
        except json.JSONDecodeError:
            click.echo("Warning: ~/.claude/settings.json is invalid JSON — overwriting with hooks only.", err=True)

    hooks = settings.setdefault("hooks", {})

    if is_windows:
        notify_cmd = f"powershell -File \"{hooks_dst / 'notify.ps1'}\""
        status_cmd = f"powershell -File \"{hooks_dst / 'status.ps1'}\""
    else:
        notify_cmd = str(hooks_dst / "notify.sh")
        status_cmd = str(hooks_dst / "status.sh")

    def _ensure_hook(event: str, cmd: str):
        event_hooks = hooks.setdefault(event, [])
        # Claude Code format: {"matcher": "", "hooks": [{"type": "command", "command": "..."}]}
        for entry in event_hooks:
            if isinstance(entry, dict):
                for h in entry.get("hooks", []):
                    if isinstance(h, dict) and h.get("command") == cmd:
                        return
        event_hooks.append({"matcher": "", "hooks": [{"type": "command", "command": cmd}]})

    _ensure_hook("Stop", notify_cmd)
    # notify only on Stop — not PreToolUse, to avoid speaking during intermediate steps
    _ensure_hook("UserPromptSubmit", status_cmd)
    _ensure_hook("PreToolUse", status_cmd)
    _ensure_hook("Stop", status_cmd)

    settings_path.write_text(json.dumps(settings, indent=2))
    click.echo(f"Updated: {settings_path}")

    # Add TTS instructions to CLAUDE.md so Claude knows to generate <tts> tags
    claude_md_path = Path.home() / ".claude" / "CLAUDE.md"
    tts_marker = "## cortana-tts Voice Output"
    tts_instructions = f"""
{tts_marker}

Every response MUST include at least one hidden `<tts>` tag. The hook reads
settings output by the UserPromptSubmit event to know what to speak.

### Tag types

```
<!-- <tts type="end">final summary, recap, follow-up questions</tts> -->
<!-- <tts type="confirm">about to do X</tts> -->
<!-- <tts type="update" mood="warn">hit an issue, trying another approach</tts> -->
```

- `end` — always include. Final spoken summary of what was done. Include
  any follow-up questions naturally in the sentence. Spoken when Claude stops.
- `confirm` — include only if `tts_confirm=on`. Brief sentence before starting
  significant work. Place it right before the first tool call.
- `update` — include only if `tts_updates=on`. Mid-response status, especially
  when something unexpected happened. Place after the issue is described.

Omitting the `type=` attribute defaults to `end`.

### Verbosity

- `tts_verbosity=normal` — 1–3 concise sentences. Key outcome only.
- `tts_verbosity=verbose` — Comprehensive. Cover every file changed, every
  decision made, every caveat. Still spoken prose, not a list.

### Personalities

Speak as the configured personality (`tts_personality=` from hook context):
- `ara` (default) — natural, direct, slightly warm. Short sentences.
- `professional` — formal, concise. Business register. No filler.
- `casual` — relaxed, brief. Like messaging a teammate.
- `brief` — one sentence only. The single most important thing.

### Rules

- Natural spoken prose — no markdown, no bullet points, no code snippets
- The `<!-- -->` wrapper is mandatory — without it the text renders in terminal
- TTS and terminal output are separate channels — do not repeat yourself
- Valid moods: `error`, `success`, `warn`, `melancholy` (omit for default)
"""

    existing_md = claude_md_path.read_text() if claude_md_path.exists() else ""
    if tts_marker not in existing_md:
        with open(claude_md_path, "a") as f:
            f.write(tts_instructions)
        click.echo(f"Added TTS instructions to: {claude_md_path}")
    else:
        click.echo(f"TTS instructions already in: {claude_md_path}")

    click.echo("Claude Code hooks installed. Restart Claude Code to activate.")


@cmd_install.command("opencode")
def install_opencode():
    """Install OpenCode plugin."""
    src = _integration_path("opencode/index.ts")
    if not src.exists():
        click.echo(f"Source not found: {src}", err=True)
        sys.exit(1)

    plugins_dir = Path.home() / ".config" / "opencode" / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    dst = plugins_dir / "cortana-tts.ts"
    shutil.copy2(src, dst)
    click.echo(f"Installed plugin: {dst}")

    # Update opencode.json
    oc_config = Path.home() / ".config" / "opencode" / "opencode.json"
    config = {}
    if oc_config.exists():
        try:
            config = json.loads(oc_config.read_text())
        except json.JSONDecodeError:
            click.echo("Warning: opencode.json is invalid JSON — overwriting.", err=True)

    plugin_ref = "~/.config/opencode/plugins/cortana-tts.ts"
    plugins = config.setdefault("plugins", [])
    if plugin_ref not in plugins:
        plugins.append(plugin_ref)
        oc_config.write_text(json.dumps(config, indent=2))
        click.echo(f"Updated: {oc_config}")
    else:
        click.echo("Plugin already registered in opencode.json.")

    click.echo("OpenCode plugin installed.")


@cmd_install.command("copilot")
def install_copilot():
    """Add gh copilot TTS wrapper to shell config."""
    if platform.system() == "Windows":
        _install_copilot_windows()
    else:
        _install_copilot_unix()


def _install_copilot_unix():
    src = _integration_path("copilot/wrapper.sh")
    if not src.exists():
        click.echo(f"Source not found: {src}", err=True)
        sys.exit(1)

    # Install watcher.py alongside the shell snippet
    watcher_src = _integration_path("copilot/watcher.py")
    watcher_dest = _config_dir() / "copilot-watcher.py"
    if watcher_src.exists():
        watcher_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(watcher_src, watcher_dest)
        click.echo(f"Installed watcher: {watcher_dest}")
    else:
        click.echo(f"Warning: watcher not found: {watcher_src}", err=True)

    # Write snippet to a dedicated file so it never ends up after an early
    # `return` or inside a conditional block in ~/.zshrc / ~/.bashrc.
    dest = _config_dir() / "copilot.sh"
    dest.write_text(src.read_text())
    dest.chmod(0o755)

    source_line = f'source "{dest}"'
    old_marker = "# cortana-tts copilot wrapper"
    rc_comment = "# cortana-tts copilot integration"

    installed_in = []
    for rc in [Path.home() / ".zshrc", Path.home() / ".bashrc"]:
        if not rc.exists():
            continue
        existing = rc.read_text()

        has_source_line = str(dest) in existing
        has_old_inline = old_marker in existing

        # Already installed AND no stale inline snippet — nothing to do
        if has_source_line and not has_old_inline:
            click.echo(f"Already installed in {rc}")
            continue

        # Remove any old inline snippet block (whether or not source line exists)
        if has_old_inline:
            lines = existing.splitlines(keepends=True)
            new_lines = []
            skip = False
            for line in lines:
                if old_marker in line:
                    skip = True
                    continue
                if skip:
                    if line.strip() == "":
                        skip = False
                    continue
                new_lines.append(line)
            # Only add source line if it wasn't already present
            if not has_source_line:
                new_lines.append(f"\n{rc_comment}\n{source_line}\n")
            rc.write_text("".join(new_lines))
            label = "Removed stale inline snippet from" if has_source_line else "Migrated"
            click.echo(f"{label} {rc}")
            installed_in.append(str(rc))
            continue

        with open(rc, "a") as f:
            f.write(f"\n{rc_comment}\n{source_line}\n")
        installed_in.append(str(rc))
        click.echo(f"Appended source line to {rc}")

    if not installed_in:
        click.echo("No .zshrc or .bashrc found.")
        click.echo(f"Add this line manually:\n  {source_line}")
    else:
        click.echo(f"Snippet written to: {dest}")
        click.echo("Reload your shell or run: source ~/.zshrc")

    # Add TTS instructions to ~/.copilot/instructions.md
    copilot_md = Path.home() / ".copilot" / "instructions.md"
    tts_marker = "## cortana-tts Voice Output"
    tts_block = f"""
{tts_marker}

Every response MUST include a hidden `<tts>` tag with a natural spoken summary.

Format — wrap in HTML comment so it stays invisible in terminal:
```
<!-- <tts>spoken summary here</tts> -->
<!-- <tts mood="error">something went wrong</tts> -->
```

Rules for TTS content:
- Speak like a human — natural, conversational, not robotic
- Summarize what was done and the outcome. Don't read out code, file paths, or technical details
- If you have questions for the user, ask them naturally in the spoken summary
- Keep it concise — 1-3 sentences for simple tasks, more for complex ones
- Valid moods: `error`, `success`, `warn` (omit for default)
- The `<!-- -->` wrapper is mandatory — without it the text renders visibly in terminal
- TTS and terminal text are separate channels — don't repeat what's in the TTS tag in your visible response
"""

    existing_md = copilot_md.read_text() if copilot_md.exists() else ""
    if tts_marker not in existing_md:
        copilot_md.parent.mkdir(parents=True, exist_ok=True)
        with open(copilot_md, "a") as f:
            f.write(tts_block)
        click.echo(f"Added TTS instructions to: {copilot_md}")
    else:
        click.echo(f"TTS instructions already in: {copilot_md}")


def _install_copilot_windows():
    src = _integration_path("copilot/wrapper.ps1")
    if not src.exists():
        click.echo(f"Source not found: {src}", err=True)
        sys.exit(1)

    dest = _config_dir() / "copilot.ps1"
    dest.write_text(src.read_text())

    dot_source = f'. "{dest}"'
    old_marker = "# cortana-tts copilot wrapper"

    profile_path_raw = subprocess.run(
        ["powershell", "-Command", "$PROFILE"],
        capture_output=True, text=True
    ).stdout.strip()
    profile = Path(profile_path_raw) if profile_path_raw else Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    profile.parent.mkdir(parents=True, exist_ok=True)

    existing = profile.read_text() if profile.exists() else ""
    has_source_line = str(dest) in existing
    has_old_inline = old_marker in existing

    if has_source_line and not has_old_inline:
        click.echo(f"Already installed in {profile}")
        return

    if has_old_inline:
        lines = existing.splitlines(keepends=True)
        new_lines = []
        skip = False
        for line in lines:
            if old_marker in line:
                skip = True
                continue
            if skip:
                if line.strip() == "":
                    skip = False
                continue
            new_lines.append(line)
        if not has_source_line:
            new_lines.append(f"\n# cortana-tts copilot integration\n{dot_source}\n")
        profile.write_text("".join(new_lines))
        label = "Removed stale inline snippet from" if has_source_line else "Migrated"
        click.echo(f"{label} {profile}")
        return

    with open(profile, "a") as f:
        f.write(f"\n# cortana-tts copilot integration\n{dot_source}\n")
    click.echo(f"Appended dot-source to {profile}")
    click.echo("Reload PowerShell to activate.")


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

@main.group("uninstall")
def cmd_uninstall():
    """Remove integrations."""
    pass


@cmd_uninstall.command("claude")
def uninstall_claude():
    """Remove Claude Code hooks."""
    hooks_dst = _hooks_dir()

    for script in ["notify.sh", "status.sh", "notify.ps1", "status.ps1"]:
        p = hooks_dst / script
        if p.exists():
            p.unlink()
            click.echo(f"Removed: {p}")

    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        click.echo("No ~/.claude/settings.json found.")
        return

    try:
        settings = json.loads(settings_path.read_text())
    except json.JSONDecodeError:
        click.echo("settings.json is invalid JSON, skipping hook removal.", err=True)
        return

    hooks = settings.get("hooks", {})
    hooks_dir_str = str(hooks_dst)
    changed = False
    for event, event_hooks in list(hooks.items()):
        filtered = []
        for entry in event_hooks:
            if isinstance(entry, dict):
                cmd = entry.get("command", "")
            else:
                cmd = str(entry)
            if hooks_dir_str in cmd:
                changed = True
            else:
                filtered.append(entry)
        hooks[event] = filtered

    if changed:
        settings_path.write_text(json.dumps(settings, indent=2))
        click.echo(f"Removed hooks from {settings_path}")
    else:
        click.echo("No cortana-tts hooks found in settings.json.")


@cmd_uninstall.command("opencode")
def uninstall_opencode():
    """Remove OpenCode plugin."""
    dst = Path.home() / ".config" / "opencode" / "plugins" / "cortana-tts.ts"
    if dst.exists():
        dst.unlink()
        click.echo(f"Removed: {dst}")

    oc_config = Path.home() / ".config" / "opencode" / "opencode.json"
    if oc_config.exists():
        try:
            config = json.loads(oc_config.read_text())
        except json.JSONDecodeError:
            return
        plugin_ref = "~/.config/opencode/plugins/cortana-tts.ts"
        plugins = config.get("plugins", [])
        if plugin_ref in plugins:
            plugins.remove(plugin_ref)
            oc_config.write_text(json.dumps(config, indent=2))
            click.echo(f"Updated: {oc_config}")


@cmd_uninstall.command("copilot")
def uninstall_copilot():
    """Remove gh copilot TTS wrapper from shell config."""
    copilot_file = _config_dir() / "copilot.sh"
    old_marker = "# cortana-tts copilot wrapper"
    new_marker = "# cortana-tts copilot integration"

    if platform.system() == "Windows":
        profile_path_raw = subprocess.run(
            ["powershell", "-Command", "$PROFILE"],
            capture_output=True, text=True
        ).stdout.strip()
        rc_files = [Path(profile_path_raw)] if profile_path_raw else []
        copilot_file = _config_dir() / "copilot.ps1"
    else:
        rc_files = [Path.home() / ".zshrc", Path.home() / ".bashrc"]

    for rc in rc_files:
        if not rc.exists():
            continue
        original = rc.read_text()
        lines = original.splitlines(keepends=True)
        new_lines = []
        skip = False
        for line in lines:
            # Match both old inline marker and new source-line comment
            if old_marker in line or new_marker in line or str(copilot_file) in line:
                skip = True
                continue
            if skip and line.strip() == "":
                skip = False
                continue
            if not skip:
                new_lines.append(line)
        result = "".join(new_lines)
        if result != original:
            rc.write_text(result)
            click.echo(f"Removed from {rc}")
        else:
            click.echo(f"Not found in {rc}")

    # Remove the dedicated snippet file
    if copilot_file.exists():
        copilot_file.unlink()
        click.echo(f"Deleted {copilot_file}")


# ---------------------------------------------------------------------------
# Engine management
# ---------------------------------------------------------------------------

def _read_current_engine() -> str:
    """Read TTS_ENGINE from ~/.config/cortana-tts/.env, defaulting to 'standard'."""
    env_path = _config_dir() / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("TTS_ENGINE="):
                return line.split("=", 1)[1].strip()
    return "standard"


@main.group("engine", invoke_without_command=True)
@click.pass_context
def cmd_engine(ctx):
    """Show or switch the active TTS engine."""
    if ctx.invoked_subcommand is None:
        engine = _read_current_engine()
        label = "Standard (kokoro, 22 voices)" if engine == "standard" else "Lightweight (piper-tts, ONNX)"
        click.echo(f"Active engine: {engine}  —  {label}")


@cmd_engine.command("standard")
def engine_standard():
    """Switch to the standard kokoro engine."""
    _save_env_var("TTS_ENGINE", "standard")
    click.echo("Engine set to: standard (kokoro)")
    click.echo("Restart the server to apply: cortana-tts restart")


@cmd_engine.command("lightweight")
@click.argument("voice", default="en_US-hfc_female-medium", required=False)
def engine_lightweight(voice: str):
    """Switch to the lightweight piper-tts engine (optionally specify a voice)."""
    # Verify piper-tts is importable in this Python runtime before switching
    try:
        import piper  # noqa: F401
    except ImportError:
        click.echo(
            "Error: piper-tts is not installed in this Python environment.\n"
            f"  Python: {sys.executable}\n\n"
            "Fix: install piper-tts into this environment:\n"
            f"  {sys.executable} -m pip install piper-tts\n\n"
            "Note: if you have multiple Python installs (e.g. Homebrew + system),\n"
            "make sure you run 'pip install piper-tts' with the same Python that\n"
            "runs 'cortana-tts'. You can check with: which cortana-tts",
            err=True,
        )
        sys.exit(1)

    from cortana_tts.piper_engine import PIPER_VOICES, _model_paths

    if voice not in PIPER_VOICES:
        click.echo(f"Unknown piper voice: {voice}", err=True)
        click.echo("Available voices:")
        for v in PIPER_VOICES:
            click.echo(f"  {v}")
        sys.exit(1)

    click.echo(f"Downloading piper voice '{voice}' if not cached...")
    try:
        _model_paths(voice)
        click.echo("✓ Voice ready.")
    except Exception as e:
        click.echo(f"Warning: download failed: {e}", err=True)

    _save_env_var("TTS_ENGINE", "piper")
    _save_env_var("TTS_PIPER_VOICE", voice)
    click.echo(f"Engine set to: lightweight (piper-tts), voice: {voice}")
    click.echo("Restart the server to apply: cortana-tts restart")


# ---------------------------------------------------------------------------
# Personality management
# ---------------------------------------------------------------------------

PERSONALITIES = {
    "ara": "Natural, direct, slightly warm. Short conversational sentences.",
    "professional": "Formal and concise. Business register. No filler words.",
    "casual": "Relaxed, brief, friendly. Like messaging a teammate.",
    "brief": "One sentence only. The single most important thing.",
}


def _read_config_file(name: str, default: str) -> str:
    p = _config_dir() / name
    return p.read_text().strip() if p.exists() else default


def _write_config_file(name: str, value: str) -> None:
    p = _config_dir()
    p.mkdir(parents=True, exist_ok=True)
    (p / name).write_text(value)


@main.group("personality", invoke_without_command=True)
@click.pass_context
def cmd_personality(ctx):
    """Show or switch the TTS speaking personality."""
    if ctx.invoked_subcommand is None:
        current = _read_config_file("tts_personality", "ara")
        desc = PERSONALITIES.get(current, "custom")
        click.echo(f"Active personality: {current}  —  {desc}")


@cmd_personality.command("list")
def personality_list():
    """List available personalities."""
    current = _read_config_file("tts_personality", "ara")
    for name, desc in PERSONALITIES.items():
        marker = " *" if name == current else "  "
        click.echo(f"{marker} {name:<14} {desc}")


@cmd_personality.command("set")
@click.argument("name")
def personality_set(name: str):
    """Set the active personality (ara, professional, casual, brief)."""
    if name not in PERSONALITIES:
        click.echo(f"Unknown personality: {name}", err=True)
        click.echo(f"Available: {', '.join(PERSONALITIES)}")
        sys.exit(1)
    _write_config_file("tts_personality", name)
    click.echo(f"Personality set to: {name}  —  {PERSONALITIES[name]}")
    click.echo("Takes effect on the next Claude response (no restart needed).")


# ---------------------------------------------------------------------------
# Messaging frequency management
# ---------------------------------------------------------------------------

MESSAGING_TYPES = {
    "confirm": ("messaging_confirm", "off",
                "Speak before tool use — announces what Claude is about to do"),
    "updates": ("messaging_updates", "on",
                "Speak mid-response status/error updates"),
    "end":     ("messaging_end", "on",
                "Speak final summary with recap and follow-up questions"),
}


@main.group("messaging", invoke_without_command=True)
@click.pass_context
def cmd_messaging(ctx):
    """Show or configure which message types are spoken."""
    if ctx.invoked_subcommand is None:
        for key, (filename, default, desc) in MESSAGING_TYPES.items():
            val = _read_config_file(filename, default)
            marker = "on " if val == "on" else "off"
            click.echo(f"  {marker}  {key:<10} {desc}")


@cmd_messaging.command("confirm")
@click.argument("state", type=click.Choice(["on", "off"]))
def messaging_confirm(state: str):
    """Enable/disable confirm messages (spoken before tool use)."""
    _write_config_file("messaging_confirm", state)
    click.echo(f"Confirm messages: {state}")


@cmd_messaging.command("updates")
@click.argument("state", type=click.Choice(["on", "off"]))
def messaging_updates(state: str):
    """Enable/disable mid-response update/status messages."""
    _write_config_file("messaging_updates", state)
    click.echo(f"Update messages: {state}")


@cmd_messaging.command("end")
@click.argument("state", type=click.Choice(["on", "off"]))
def messaging_end(state: str):
    """Enable/disable end-of-response summary messages."""
    _write_config_file("messaging_end", state)
    click.echo(f"End messages: {state}")


@cmd_messaging.command("preset")
@click.argument("name", type=click.Choice(["minimal", "normal", "full"]))
def messaging_preset(name: str):
    """Apply a messaging preset.

    \b
    minimal  end only (quiet during work)
    normal   end + updates (default)
    full     confirm + updates + end (most verbose)
    """
    presets = {
        "minimal": {"messaging_confirm": "off", "messaging_updates": "off", "messaging_end": "on"},
        "normal":  {"messaging_confirm": "off", "messaging_updates": "on",  "messaging_end": "on"},
        "full":    {"messaging_confirm": "on",  "messaging_updates": "on",  "messaging_end": "on"},
    }
    for filename, value in presets[name].items():
        _write_config_file(filename, value)
    click.echo(f"Messaging preset: {name}")
    for filename, value in presets[name].items():
        click.echo(f"  {filename.replace('messaging_', ''):<10} {value}")


if __name__ == "__main__":
    main()
