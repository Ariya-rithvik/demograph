"""Audio and ffmpeg helpers: voiceover synthesis, WAV timing, and the ffmpeg binary."""

import hashlib
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path

from ..config import settings

DEFAULT_VOICE = "en-US-AriaNeural"
RATE = 24000  # all narration audio is 24 kHz mono 16-bit, so WAVs can be joined directly


def ffmpeg_exe() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    import imageio_ffmpeg  # bundled binary for machines without ffmpeg on PATH
    return imageio_ffmpeg.get_ffmpeg_exe()


def ffmpeg(*args: str) -> None:
    subprocess.run([ffmpeg_exe(), "-v", "error", "-y", *map(str, args)], check=True)


def wav_duration(path: Path) -> float:
    with wave.open(str(path)) as w:
        return w.getnframes() / w.getframerate()


def write_silence(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x00\x00" * int(seconds * RATE))


def build_track(parts: list[tuple[float, Path | None, float]], out: Path) -> None:
    """parts: (start offset in s, wav or None, slot length in s). Writes one continuous WAV."""
    with wave.open(str(out), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(RATE)
        for lead, src, slot in parts:
            total = int(slot * RATE)
            lead_frames = int(lead * RATE)
            data = b""
            if src is not None:
                with wave.open(str(src)) as w:
                    data = w.readframes(w.getnframes())
            body = data[: max(0, (total - lead_frames)) * 2]
            dst.writeframes(b"\x00\x00" * lead_frames + body)
            written = lead_frames + len(body) // 2
            dst.writeframes(b"\x00\x00" * max(0, total - written))


@dataclass
class Voiceover:
    path: Path
    duration: float
    engine: str


async def synthesize(text: str, voice: str = DEFAULT_VOICE, engine: str = "auto") -> Voiceover:
    """Narration as a WAV. Uses Microsoft Edge neural voices (online, no API key).
    With engine="silent", or if the voice service is unreachable, returns silence of
    a natural reading length so the video timing still works and captions carry the words."""
    cache = settings.data_dir / "cache" / "tts"
    cache.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha1(f"{voice}|{text}".encode("utf-8")).hexdigest()[:16]
    wav = cache / f"{key}.wav"
    if engine != "silent":
        if wav.exists():
            return Voiceover(wav, wav_duration(wav), f"edge-tts:{voice}")
        try:
            import edge_tts
            mp3 = cache / f"{key}.mp3"
            await edge_tts.Communicate(text, voice).save(str(mp3))
            ffmpeg("-i", mp3, "-ar", RATE, "-ac", 1, wav)
            mp3.unlink(missing_ok=True)
            return Voiceover(wav, wav_duration(wav), f"edge-tts:{voice}")
        except Exception:
            if engine == "edge":
                raise
    silent = cache / f"silent-{key}.wav"
    seconds = max(1.6, len(text.split()) / 2.6)
    write_silence(silent, seconds)
    return Voiceover(silent, seconds, "silent")
