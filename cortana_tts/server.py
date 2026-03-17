import json
import logging
import os
import time

logger = logging.getLogger(__name__)
perf_logger = logging.getLogger("cortana_tts.perf")
from contextlib import asynccontextmanager
from enum import Enum
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from dotenv import load_dotenv

from cortana_tts.tts_engine import TTSEngine
from cortana_tts.audio_player import AudioPlayer
from cortana_tts.pipeline import SpeakPipeline
from cortana_tts.alert_cache import AlertCache, SAMPLE_RATE


class SpeakRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=50000)
    mood: Optional[str] = None


class StatusState(str, Enum):
    thinking = "thinking"
    idle = "idle"


class StatusRequest(BaseModel):
    state: StatusState
    event: Optional[str] = None
    tool_name: Optional[str] = None


class VoiceCueModeRequest(BaseModel):
    mode: str


class PlaybackModeRequest(BaseModel):
    mode: str


class VoiceRequest(BaseModel):
    voice: str


class MuteRequest(BaseModel):
    muted: bool


class ConfigRequest(BaseModel):
    personality: Optional[str] = None
    confirm: Optional[str] = None    # "on" | "off"
    updates: Optional[str] = None    # "on" | "off"
    end: Optional[str] = None        # "on" | "off"
    verbosity: Optional[str] = None  # "normal" | "verbose"


TOOL_MOOD = {
    "Glob": "search",
    "Grep": "search",
    "WebSearch": "search",
    "WebFetch": "search",
    "Bash": "execute",
    "Agent": "agent",
}


class StatusBroadcaster:
    def __init__(self):
        self.connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.connections.append(ws)

    def disconnect(self, ws: WebSocket):
        if ws in self.connections:
            self.connections.remove(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead = []
        for ws in self.connections:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


broadcaster = StatusBroadcaster()
audio_broadcaster = StatusBroadcaster()


def _get_env_path() -> Path:
    """Resolve .env path: CORTANA_TTS_CONFIG env var → ~/.config/cortana-tts/.env"""
    env_override = os.environ.get("CORTANA_TTS_CONFIG")
    if env_override:
        return Path(env_override)
    return Path.home() / ".config" / "cortana-tts" / ".env"


def create_pipeline() -> tuple[SpeakPipeline, AlertCache]:
    _env_path = _get_env_path()
    load_dotenv(_env_path)

    tts_engine_name = os.getenv("TTS_ENGINE", "standard").lower()
    speed = float(os.getenv("TTS_SPEED", "1.1"))

    if tts_engine_name == "piper":
        from cortana_tts.piper_engine import PiperEngine
        piper_voice = os.getenv("TTS_PIPER_VOICE", "en_US-lessac-medium")
        engine = PiperEngine(voice=piper_voice, speed=speed)
        logger.info("Using PiperEngine (lightweight), voice=%s", piper_voice)
    else:
        voice = os.getenv("TTS_VOICE", "af_heart")
        engine = TTSEngine(voice=voice, speed=speed)
        logger.info("Using TTSEngine (standard), voice=%s", voice)

    player = AudioPlayer()
    pipeline = SpeakPipeline(tts_engine=engine, audio_player=player, broadcaster=broadcaster)
    cache_dir = Path(os.getenv("ALERT_CACHE_DIR", Path.home() / ".config" / "cortana-tts" / "alert_cache"))
    alert_cache = AlertCache(cache_dir=cache_dir, tts_engine=engine)
    return pipeline, alert_cache


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    from cortana_tts.pipeline import set_main_loop
    set_main_loop(asyncio.get_running_loop())
    app.state.start_time = time.time()
    pipeline, alert_cache = create_pipeline()
    app.state.pipeline = pipeline
    app.state.alert_cache = alert_cache
    app.state.broadcaster = broadcaster
    app.state.audio_broadcaster = audio_broadcaster
    pipeline.audio_broadcaster = audio_broadcaster
    alert_cache.warm()
    app.state.voice_cue_mode = "30s"
    app.state.playback_mode = "chunked"
    app.state.last_cue_time = 0.0
    app.state.cue_fired_this_cycle = False
    app.state.muted = False
    yield


def _should_play_cue(app) -> bool:
    mode = getattr(app.state, "voice_cue_mode", "30s")
    if mode == "off":
        return False
    if mode == "always":
        return True
    if mode == "once":
        return not getattr(app.state, "cue_fired_this_cycle", False)
    try:
        seconds = int(mode.rstrip("s"))
    except ValueError:
        seconds = 30
    elapsed = time.time() - getattr(app.state, "last_cue_time", 0.0)
    return elapsed >= seconds


def create_app(custom_lifespan=None) -> FastAPI:
    app = FastAPI(title="cortana-tts", lifespan=custom_lifespan or lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    def health():
        return {
            "status": "ok",
            "uptime_seconds": round(time.time() - app.state.start_time, 1),
        }

    @app.post("/speak")
    def speak(req: SpeakRequest, background_tasks: BackgroundTasks):
        perf_logger.info("Server received /speak: text=%d chars, mood=%s", len(req.text), req.mood or "none")
        if req.mood:
            background_tasks.add_task(app.state.broadcaster.broadcast, {"mood": req.mood})
        if app.state.playback_mode == "chunked":
            background_tasks.add_task(app.state.pipeline.speak_chunked, req.text, leadin_audio=None)
        else:
            background_tasks.add_task(app.state.pipeline.speak, req.text)
        status = "muted" if app.state.muted else "speaking"
        return {"status": status, "mode": app.state.playback_mode, "mood": req.mood}

    @app.post("/alert")
    def alert(background_tasks: BackgroundTasks):
        result = app.state.alert_cache.random_alert()
        if result is None:
            return {"status": "no_alerts_cached"}
        text, audio = result
        def play_alert():
            app.state.pipeline._start_speaking()
            def on_amplitude(level):
                app.state.pipeline._broadcast({"amplitude": round(float(level), 3)})
            try:
                app.state.pipeline.player.play(audio, SAMPLE_RATE, on_amplitude=on_amplitude)
            finally:
                app.state.pipeline._stop_speaking()
        background_tasks.add_task(play_alert)
        return {"status": "alerting", "text": text}

    @app.post("/status")
    def status(req: StatusRequest, background_tasks: BackgroundTasks):
        broadcast_data = {"state": req.state.value}
        background_tasks.add_task(app.state.broadcaster.broadcast, broadcast_data)

        if req.state == StatusState.idle:
            app.state.cue_fired_this_cycle = False
            return {"state": "idle"}

        if req.event and _should_play_cue(app):
            cue = None
            if req.event == "subagent_start":
                cue = app.state.alert_cache.random_cue("Agent")
            elif req.event == "tool_use":
                cue = app.state.alert_cache.random_cue(req.tool_name)

            if cue:
                text, audio = cue
                app.state.last_cue_time = time.time()
                app.state.cue_fired_this_cycle = True

                def play_cue():
                    app.state.pipeline._start_speaking()
                    def on_amplitude(level):
                        app.state.pipeline._broadcast({"amplitude": round(float(level), 3)})
                    try:
                        app.state.pipeline.player.play(audio, SAMPLE_RATE, on_amplitude=on_amplitude)
                    finally:
                        app.state.pipeline._stop_speaking()
                background_tasks.add_task(play_cue)
                return {"state": req.state.value, "cue": text}

        if req.event == "tool_use" and req.tool_name:
            tool_mood = TOOL_MOOD.get(req.tool_name)
            if tool_mood:
                background_tasks.add_task(app.state.broadcaster.broadcast, {"tool_mood": tool_mood})

        return {"state": req.state.value}

    @app.post("/stop")
    def stop_speaking():
        app.state.pipeline.player.stop()
        with app.state.pipeline._speaking_lock:
            app.state.pipeline._speaking_count = 0
        app.state.pipeline._broadcast({"speaking": False})
        return {"status": "stopped"}

    @app.post("/playback-mode")
    def set_playback_mode(req: PlaybackModeRequest):
        if req.mode not in ("full", "chunked"):
            return {"error": "invalid mode"}
        app.state.playback_mode = req.mode
        return {"mode": req.mode}

    @app.post("/mute")
    def set_mute(req: MuteRequest):
        app.state.muted = req.muted
        app.state.pipeline.player.volume = 0.0 if req.muted else 1.0
        if req.muted:
            app.state.pipeline.player.stop()
        return {"muted": req.muted}

    @app.post("/voice")
    def set_voice(req: VoiceRequest):
        app.state.pipeline.tts.voice = req.voice
        app.state.alert_cache.switch_voice(req.voice)
        return {"voice": req.voice}

    @app.get("/config")
    def get_config():
        config_dir = Path.home() / ".config" / "cortana-tts"
        def rf(name, default):
            p = config_dir / name
            return p.read_text().strip() if p.exists() else default
        return {
            "personality": rf("tts_personality", "ara"),
            "confirm": rf("messaging_confirm", "off"),
            "updates": rf("messaging_updates", "on"),
            "end": rf("messaging_end", "on"),
            "verbosity": rf("tts_mode", "normal"),
        }

    @app.post("/config")
    def set_config(req: ConfigRequest):
        config_dir = Path.home() / ".config" / "cortana-tts"
        config_dir.mkdir(parents=True, exist_ok=True)
        mapping = {
            "personality": "tts_personality",
            "confirm": "messaging_confirm",
            "updates": "messaging_updates",
            "end": "messaging_end",
            "verbosity": "tts_mode",
        }
        updated = {}
        for field, filename in mapping.items():
            value = getattr(req, field, None)
            if value is not None:
                (config_dir / filename).write_text(value)
                updated[field] = value
        return {"ok": True, "updated": updated}

    @app.post("/voice-cue-mode")
    def set_voice_cue_mode(req: VoiceCueModeRequest):
        if req.mode not in ("off", "once", "15s", "30s", "always"):
            return {"error": "invalid mode"}
        app.state.voice_cue_mode = req.mode
        return {"mode": req.mode}

    @app.websocket("/ws/status")
    async def ws_status(ws: WebSocket):
        await app.state.broadcaster.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            app.state.broadcaster.disconnect(ws)
            logger.info("WebSocket /ws/status client disconnected")

    @app.websocket("/ws/audio")
    async def ws_audio(ws: WebSocket):
        await audio_broadcaster.connect(ws)
        try:
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            audio_broadcaster.disconnect(ws)
            logger.info("WebSocket /ws/audio client disconnected")

    return app


app = create_app()


def main():
    import uvicorn
    load_dotenv(_get_env_path())
    perf_log = logging.getLogger("cortana_tts.perf")
    perf_log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [PERF] %(message)s", datefmt="%H:%M:%S"))
    perf_log.addHandler(handler)
    port = int(os.getenv("TTS_PORT", os.getenv("CORTANA_TTS_PORT", "5111")))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
