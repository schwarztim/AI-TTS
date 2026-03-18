"""PiperEngine — lightweight TTS using piper-tts (ONNX Runtime, no PyTorch)."""

import logging
import re
import time
import urllib.request
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)
perf_logger = logging.getLogger("cortana_tts.perf")

SAMPLE_RATE = 22050  # piper outputs 22050 Hz PCM int16
FADE_MS = 10
CROSSFADE_MS = 20
_SENTENCE_RE = re.compile(r'(?<=[.!?])\s+')
MIN_CHUNK_CHARS = 100

# HuggingFace base URL for piper voices
_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0"

PIPER_VOICES = [
    "en_US-hfc_female-medium",
    "en_US-lessac-medium",
    "en_US-ryan-medium",
    "en_US-amy-medium",
    "en_GB-alba-medium",
    "en_GB-northern_english_male-medium",
]


def _voice_cache_dir() -> Path:
    base = Path.home() / ".config" / "cortana-tts" / "piper-voices"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _parse_voice_name(voice: str) -> tuple[str, str, str, str]:
    """Parse 'en_US-hfc_female-medium' → (lang='en', locale='en_US', name='hfc_female', quality='medium').

    HuggingFace path: en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx
    """
    parts = voice.rsplit("-", 1)
    if len(parts) != 2:
        raise ValueError(f"Cannot parse piper voice name: {voice!r}. Expected format: locale-name-quality")
    voice_prefix, quality = parts
    # voice_prefix is e.g. "en_US-hfc_female" or "en_US-lessac"
    locale = voice_prefix.split("-")[0]  # en_US
    lang = locale.split("_")[0]  # en
    name = voice_prefix.split("-", 1)[1]  # hfc_female, lessac, etc.
    return lang, locale, name, quality


def _model_paths(voice: str) -> tuple[Path, Path]:
    """Return (onnx_path, json_path) for a voice, downloading if needed."""
    cache = _voice_cache_dir()
    lang, locale, name, quality = _parse_voice_name(voice)

    # Filename: en_US-hfc_female-medium.onnx
    filename = f"{locale}-{name}-{quality}"
    onnx_path = cache / f"{filename}.onnx"
    json_path = cache / f"{filename}.onnx.json"

    if onnx_path.exists() and json_path.exists():
        return onnx_path, json_path

    # HF path: en/en_US/hfc_female/medium/en_US-hfc_female-medium.onnx
    base_url = f"{_HF_BASE}/{lang}/{locale}/{name}/{quality}"
    onnx_url = f"{base_url}/{filename}.onnx"
    json_url = f"{base_url}/{filename}.onnx.json"

    logger.info("Downloading piper voice model: %s", voice)

    for url, dest in [(json_url, json_path), (onnx_url, onnx_path)]:
        logger.info("  %s -> %s", url, dest)
        try:
            # Stream download with progress
            req = urllib.request.Request(url, headers={"User-Agent": "cortana-tts/0.1.3"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                total = int(resp.headers.get("Content-Length", 0))
                downloaded = 0
                chunk_size = 65536
                with open(dest, "wb") as f:
                    while True:
                        chunk = resp.read(chunk_size)
                        if not chunk:
                            break
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total:
                            pct = downloaded / total * 100
                            print(f"\r  {dest.name}: {pct:.0f}%", end="", flush=True)
            print()
        except Exception as e:
            # Clean up partial downloads
            if dest.exists():
                dest.unlink()
            raise RuntimeError(f"Failed to download {url}: {e}") from e

    logger.info("Piper voice model downloaded: %s", voice)
    return onnx_path, json_path


class PiperEngine:
    """Lightweight TTS engine using piper-tts (ONNX Runtime).

    Drop-in replacement for TTSEngine with identical interface.
    """

    def __init__(self, voice: str = "en_US-hfc_female-medium", speed: float = 1.0):
        self.voice = voice
        self.speed = speed
        self._voice_obj = None
        self._loaded_voice_name: str | None = None
        # Eagerly load the voice on init
        self._load_voice(voice)

    def _load_voice(self, voice: str):
        """Load (or reload) a piper voice, downloading model if needed."""
        if self._loaded_voice_name == voice and self._voice_obj is not None:
            return
        try:
            from piper import PiperVoice
        except ImportError as e:
            raise ImportError(
                "piper-tts is not installed. Run: pip install piper-tts"
            ) from e

        onnx_path, json_path = _model_paths(voice)
        self._voice_obj = PiperVoice.load(str(onnx_path), config_path=str(json_path))
        self._loaded_voice_name = voice
        logger.info("PiperEngine loaded voice: %s", voice)

    def _ensure_voice(self):
        """Reload voice if self.voice was changed externally."""
        if self.voice != self._loaded_voice_name:
            self._load_voice(self.voice)

    # ------------------------------------------------------------------
    # Audio helpers (same as TTSEngine)
    # ------------------------------------------------------------------

    def _apply_fade(self, audio: np.ndarray) -> np.ndarray:
        fade_samples = int(SAMPLE_RATE * FADE_MS / 1000)
        fade_samples = min(fade_samples, len(audio) // 2)
        if fade_samples > 0:
            audio = audio.copy()
            audio[-fade_samples:] *= np.linspace(1.0, 0.0, fade_samples, dtype=np.float32)
        return audio

    def _crossfade_chunks(self, chunks: list[np.ndarray]) -> np.ndarray:
        chunks = [np.asarray(c, dtype=np.float32) for c in chunks]
        chunks = [c - c.mean() for c in chunks]
        if len(chunks) == 1:
            return chunks[0]
        xf_samples = int(SAMPLE_RATE * CROSSFADE_MS / 1000)
        result = chunks[0]
        for chunk in chunks[1:]:
            xf = min(xf_samples, len(result), len(chunk))
            if xf > 0:
                fade_out = np.linspace(1.0, 0.0, xf, dtype=np.float32)
                fade_in = np.linspace(0.0, 1.0, xf, dtype=np.float32)
                overlap = result[-xf:] * fade_out + chunk[:xf] * fade_in
                result = np.concatenate([result[:-xf], overlap, chunk[xf:]])
            else:
                result = np.concatenate([result, chunk])
        return result

    def _raw_bytes_to_float32(self, raw_bytes: bytes) -> np.ndarray:
        """Convert raw int16 PCM bytes to float32 in [-1, 1]."""
        arr = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32)
        arr /= 32768.0
        return arr

    def _synthesize_chunk(self, text: str) -> np.ndarray:
        """Synthesize a single text chunk. Returns float32 numpy array."""
        self._ensure_voice()
        raw = self._synthesize_raw(text)
        audio = self._raw_bytes_to_float32(raw)
        audio = audio - audio.mean()
        audio = self._apply_fade(audio)
        peak = np.max(np.abs(audio))
        if peak > 1.0:
            audio = audio / peak
        return audio

    def _synthesize_raw(self, text: str) -> bytes:
        """Get raw int16 PCM bytes from piper, handling API differences."""
        voice = self._voice_obj
        # Preferred: synthesize_stream_raw (yields raw bytes per sentence)
        if hasattr(voice, "synthesize_stream_raw"):
            return b"".join(voice.synthesize_stream_raw(text))
        # Fallback: synthesize() returns Iterable[AudioChunk] with .audio_float_array
        chunks = list(voice.synthesize(text))
        if not chunks:
            return b""
        audio_float = np.concatenate([c.audio_float_array for c in chunks])
        audio_int16 = np.clip(audio_float * 32767, -32768, 32767).astype(np.int16)
        return audio_int16.tobytes()

    def _split_text(self, text: str) -> list[str]:
        """Split text into sentence-level chunks for streaming."""
        sentences = _SENTENCE_RE.split(text)
        sentences = [s.strip() for s in sentences if s.strip()]
        merged = []
        buf = ""
        for i, s in enumerate(sentences):
            candidate = (buf + " " + s).strip() if buf else s
            if len(candidate) >= MIN_CHUNK_CHARS or i == len(sentences) - 1:
                merged.append(candidate)
                buf = ""
            else:
                buf = candidate
        if buf:
            if merged:
                merged[-1] += " " + buf
            else:
                merged.append(buf)
        return merged if merged else [text]

    # ------------------------------------------------------------------
    # Public interface (mirrors TTSEngine exactly)
    # ------------------------------------------------------------------

    def generate_stream(self, text: str):
        """Yield (audio_array, sample_rate) chunks as they are synthesized."""
        try:
            chunks = self._split_text(text)
            total_start = time.perf_counter()
            total_samples = 0
            for chunk_idx, sentence in enumerate(chunks):
                sent_start = time.perf_counter()
                audio = self._synthesize_chunk(sentence)
                total_samples += len(audio)
                chunk_dur = len(audio) / SAMPLE_RATE
                sent_elapsed = time.perf_counter() - sent_start
                perf_logger.info(
                    "PiperTTS chunk %d: %.0fms gen, %.1fs audio (%.1fx RT)",
                    chunk_idx, sent_elapsed * 1000, chunk_dur,
                    chunk_dur / max(sent_elapsed, 0.001),
                )
                yield audio, SAMPLE_RATE
            total_elapsed = time.perf_counter() - total_start
            total_audio = total_samples / SAMPLE_RATE
            perf_logger.info(
                "PiperTTS stream done: %d chars, %d chunks, %.0fms gen, %.1fs audio (%.1fx RT)",
                len(text), len(chunks), total_elapsed * 1000, total_audio,
                total_audio / max(total_elapsed, 0.001),
            )
        except Exception:
            logger.exception("PiperEngine streaming generation failed for text: %s", text[:100])

    def generate(self, text: str) -> tuple[np.ndarray, int]:
        """Generate full audio from text. Returns (audio_array, sample_rate)."""
        try:
            gen_start = time.perf_counter()
            chunks_list = self._split_text(text)
            arrays = []
            for sentence in chunks_list:
                arrays.append(self._synthesize_chunk(sentence))
            if not arrays:
                return np.array([], dtype=np.float32), SAMPLE_RATE
            audio = self._crossfade_chunks(arrays)
            audio = self._apply_fade(audio)
            peak = np.max(np.abs(audio))
            if peak > 1.0:
                audio = audio / peak
            gen_elapsed = time.perf_counter() - gen_start
            audio_dur = len(audio) / SAMPLE_RATE
            perf_logger.info(
                "PiperTTS full: %d chars, %d chunks, %.0fms gen, %.1fs audio (%.1fx RT)",
                len(text), len(arrays), gen_elapsed * 1000, audio_dur,
                audio_dur / max(gen_elapsed, 0.001),
            )
            return audio, SAMPLE_RATE
        except Exception:
            logger.exception("PiperEngine generation failed for text: %s", text[:100])
            return np.array([], dtype=np.float32), SAMPLE_RATE
