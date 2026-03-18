#!/usr/bin/env python3
"""cortana-tts copilot watcher — monitors Copilot CLI events.jsonl and speaks responses.

Standalone script (stdlib only). Launched as a background process by the shell wrapper.
Self-terminates on session.shutdown or after 60s of inactivity.

Usage:
    python3 watcher.py --tts-url http://127.0.0.1:5111 --started-after 1710000000.0
"""

import argparse
import json
import os
import re
import signal
import sys
import time
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SESSION_DIR = Path.home() / ".copilot" / "session-state"
DETECT_TIMEOUT = 30  # seconds to wait for a new session
IDLE_TIMEOUT = 60    # seconds of no file activity before self-exit
TTS_TAG_RE = re.compile(r'<!--\s*<tts(?:\s+mood="([^"]*)")?\s*>(.+?)</tts>\s*-->', re.DOTALL)
LOG_PATH = Path.home() / "Library" / "Logs" / "cortana-tts-copilot.log"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_log_file = None


def _log(msg: str):
    global _log_file
    try:
        if _log_file is None:
            LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            _log_file = open(LOG_PATH, "a")
        ts = time.strftime("%H:%M:%S")
        _log_file.write(f"[{ts}] {msg}\n")
        _log_file.flush()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# TTS helpers
# ---------------------------------------------------------------------------


def _post_json(url: str, data: dict):
    """POST JSON to the TTS server. Fire-and-forget."""
    try:
        body = json.dumps(data).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        _log(f"POST {url} failed: {e}")


def _extract_tts(content: str) -> tuple[str, str | None] | None:
    """Extract text and mood from <tts> tags, falling back to raw content (capped).

    Returns (text, mood) or None.
    """
    m = TTS_TAG_RE.search(content)
    if m:
        mood = m.group(1) or None  # group 1 = mood attribute
        text = m.group(2).strip()  # group 2 = tag content
        if text:
            return text, mood
        return None
    # Fallback: speak raw content, capped at 500 chars
    text = content.strip()
    if text:
        return text[:500], None
    return None


# ---------------------------------------------------------------------------
# Session detection
# ---------------------------------------------------------------------------


def _find_session(started_after: float, timeout: float) -> Path | None:
    """Poll for a session events.jsonl with mtime > started_after."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if SESSION_DIR.is_dir():
            for d in SESSION_DIR.iterdir():
                events = d / "events.jsonl"
                if events.is_file():
                    try:
                        if events.stat().st_mtime > started_after:
                            return events
                    except OSError:
                        pass
        time.sleep(0.5)
    return None


# ---------------------------------------------------------------------------
# File watcher (kqueue on macOS, stat polling on Linux)
# ---------------------------------------------------------------------------


def _watch_kqueue(path: Path, callback, idle_timeout: float):
    """Watch file for writes using kqueue (macOS)."""
    import select

    fd = os.open(str(path), os.O_RDONLY)
    try:
        kq = select.kqueue()
        ev = select.kevent(
            fd,
            filter=select.KQ_FILTER_VNODE,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE,
        )
        last_activity = time.time()
        while True:
            events = kq.control([ev], 1, 2.0)  # 2s timeout per poll
            if events:
                for e in events:
                    if e.fflags & select.KQ_NOTE_DELETE:
                        _log("File deleted, exiting")
                        return
                last_activity = time.time()
                if callback():
                    return  # session.shutdown
            elif time.time() - last_activity > idle_timeout:
                _log(f"No activity for {idle_timeout}s, exiting")
                return
    finally:
        os.close(fd)


def _watch_poll(path: Path, callback, idle_timeout: float):
    """Watch file for writes using stat polling (Linux fallback)."""
    try:
        last_mtime = path.stat().st_mtime
    except OSError:
        return
    last_activity = time.time()
    while True:
        time.sleep(1.0)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            _log("File gone, exiting")
            return
        if mtime != last_mtime:
            last_mtime = mtime
            last_activity = time.time()
            if callback():
                return  # session.shutdown
        elif time.time() - last_activity > idle_timeout:
            _log(f"No activity for {idle_timeout}s, exiting")
            return


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="cortana-tts copilot watcher")
    parser.add_argument("--tts-url", default="http://127.0.0.1:5111")
    parser.add_argument("--started-after", type=float, required=True)
    args = parser.parse_args()

    # Detach from terminal process group
    try:
        os.setsid()
    except OSError:
        pass

    # Ignore SIGHUP so we survive terminal close
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    tts_url = args.tts_url
    _log(f"Watcher started, tts_url={tts_url}, started_after={args.started_after}")

    # Find the session
    events_path = _find_session(args.started_after, DETECT_TIMEOUT)
    if not events_path:
        _log("No session found within timeout, exiting")
        return

    _log(f"Watching: {events_path}")

    # State
    file_pos = 0
    spoken_ids: set[str] = set()

    def process_new_events() -> bool:
        """Read new lines, process events. Return True on session.shutdown."""
        nonlocal file_pos
        try:
            with open(events_path, "r") as f:
                f.seek(file_pos)
                new_data = f.read()
                file_pos = f.tell()
        except OSError as e:
            _log(f"Read error: {e}")
            return False

        if not new_data:
            return False

        # Guard against partial line reads: if data doesn't end with newline,
        # rewind past the incomplete trailing fragment so it's re-read next time.
        if not new_data.endswith("\n"):
            last_nl = new_data.rfind("\n")
            if last_nl == -1:
                # Entire read is a partial line — rewind everything
                file_pos -= len(new_data.encode("utf-8"))
                return False
            partial = new_data[last_nl + 1:]
            file_pos -= len(partial.encode("utf-8"))
            new_data = new_data[: last_nl + 1]

        for line in new_data.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get("type", "")

            if etype in ("user.message", "assistant.turn_start"):
                _post_json(f"{tts_url}/status", {"state": "thinking"})
                _log(f"Event: {etype} -> thinking")

            elif etype == "assistant.message":
                msg_id = event.get("data", {}).get("messageId", "")
                if msg_id and msg_id in spoken_ids:
                    continue
                if msg_id:
                    spoken_ids.add(msg_id)

                content = event.get("data", {}).get("content", "")
                result = _extract_tts(content)
                if result:
                    text, mood = result
                    payload = {"text": text}
                    if mood:
                        payload["mood"] = mood
                    _post_json(f"{tts_url}/speak", payload)
                    _log(f"Spoke: {text[:80]}...")

            elif etype == "assistant.turn_end":
                _post_json(f"{tts_url}/status", {"state": "idle"})
                _log(f"Event: {etype} -> idle")

            elif etype == "session.shutdown":
                _post_json(f"{tts_url}/status", {"state": "idle"})
                _log("Session shutdown, exiting")
                return True

        return False

    # Initial read of any existing content
    process_new_events()

    # Watch for changes
    if sys.platform == "darwin":
        try:
            _watch_kqueue(events_path, process_new_events, IDLE_TIMEOUT)
        except Exception as e:
            _log(f"kqueue failed ({e}), falling back to poll")
            _watch_poll(events_path, process_new_events, IDLE_TIMEOUT)
    else:
        _watch_poll(events_path, process_new_events, IDLE_TIMEOUT)

    _log("Watcher exiting")


if __name__ == "__main__":
    main()
