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

@main.command("start")
@click.option("--port", default=5111, show_default=True, help="Port to listen on.")
@click.option("--voice", default="af_heart", show_default=True, help="Voice to use.")
@click.option("--bg", is_flag=True, default=False, help="Start server in background.")
def cmd_start(port: int, voice: str, bg: bool):
    """Start the cortana-tts server."""
    if _is_running():
        click.echo("cortana-tts server is already running.")
        return

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
        # Avoid duplicates
        for entry in event_hooks:
            if isinstance(entry, dict) and entry.get("command") == cmd:
                return
            if entry == cmd:
                return
        event_hooks.append({"type": "command", "command": cmd})

    _ensure_hook("Stop", notify_cmd)
    _ensure_hook("PreToolUse", notify_cmd)
    _ensure_hook("UserPromptSubmit", status_cmd)
    _ensure_hook("PreToolUse", status_cmd)
    _ensure_hook("Stop", status_cmd)

    settings_path.write_text(json.dumps(settings, indent=2))
    click.echo(f"Updated: {settings_path}")
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

    snippet = src.read_text()
    marker = "# cortana-tts copilot wrapper"
    full_snippet = f"\n{marker}\n{snippet}\n"

    installed_in = []
    for rc in [Path.home() / ".zshrc", Path.home() / ".bashrc"]:
        if rc.exists():
            existing = rc.read_text()
            if marker in existing:
                click.echo(f"Already installed in {rc}")
                continue
            with open(rc, "a") as f:
                f.write(full_snippet)
            installed_in.append(str(rc))
            click.echo(f"Appended to {rc}")

    if not installed_in:
        click.echo("No .zshrc or .bashrc found. Snippet:")
        click.echo(full_snippet)
    else:
        click.echo("Reload your shell or run: source ~/.zshrc")


def _install_copilot_windows():
    src = _integration_path("copilot/wrapper.ps1")
    if not src.exists():
        click.echo(f"Source not found: {src}", err=True)
        sys.exit(1)

    snippet = src.read_text()
    marker = "# cortana-tts copilot wrapper"

    profile_path_raw = subprocess.run(
        ["powershell", "-Command", "$PROFILE"],
        capture_output=True, text=True
    ).stdout.strip()
    profile = Path(profile_path_raw) if profile_path_raw else Path.home() / "Documents" / "PowerShell" / "Microsoft.PowerShell_profile.ps1"
    profile.parent.mkdir(parents=True, exist_ok=True)

    existing = profile.read_text() if profile.exists() else ""
    if marker in existing:
        click.echo(f"Already installed in {profile}")
        return

    with open(profile, "a") as f:
        f.write(f"\n{marker}\n{snippet}\n")
    click.echo(f"Appended to {profile}")
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
    marker = "# cortana-tts copilot wrapper"

    if platform.system() == "Windows":
        profile_path_raw = subprocess.run(
            ["powershell", "-Command", "$PROFILE"],
            capture_output=True, text=True
        ).stdout.strip()
        rc_files = [Path(profile_path_raw)] if profile_path_raw else []
    else:
        rc_files = [Path.home() / ".zshrc", Path.home() / ".bashrc"]

    for rc in rc_files:
        if not rc.exists():
            continue
        lines = rc.read_text().splitlines(keepends=True)
        # Find marker line and remove from there to next blank line after block
        new_lines = []
        skip = False
        for line in lines:
            if marker in line:
                skip = True
            if not skip:
                new_lines.append(line)
            elif line.strip() == "" and skip:
                # End of block
                skip = False
        if len(new_lines) != len(lines):
            rc.write_text("".join(new_lines))
            click.echo(f"Removed wrapper from {rc}")
        else:
            click.echo(f"Wrapper not found in {rc}")


if __name__ == "__main__":
    main()
