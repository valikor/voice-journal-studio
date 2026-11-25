"""ffmpeg helpers: 16 kHz WAV for ASR/pitch, and short cached clips for review playback."""
import hashlib
import os
import subprocess
import threading
from pathlib import Path

from .config import DATA

WAV_CACHE = DATA / "wav"
CLIP_CACHE = DATA / "clips"
WAV_CACHE.mkdir(exist_ok=True)
CLIP_CACHE.mkdir(exist_ok=True)


def probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL,
        ).stdout.strip()
        return float(out) if out else None
    except Exception:
        return None


def wav16k(path, rec_id):
    """Decode a recording to mono 16 kHz PCM (kept only while a recording is being processed)."""
    out = WAV_CACHE / f"{rec_id}.wav"
    if not out.exists() or out.stat().st_size == 0:
        tmp = out.with_suffix(".tmp.wav")
        r = subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(path), "-ar", "16000", "-ac", "1",
             "-c:a", "pcm_s16le", str(tmp)],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        if r.returncode != 0 or not tmp.exists():
            raise RuntimeError(f"ffmpeg failed: {r.stderr[-400:]}")
        tmp.rename(out)
    return out


def drop_wav(rec_id):
    (WAV_CACHE / f"{rec_id}.wav").unlink(missing_ok=True)


def clip(path, start, end, fmt="m4a"):
    """Cut [start,end] seconds from the original file for browser playback."""
    start = max(0.0, float(start))
    end = max(start + 0.2, float(end))
    key = hashlib.sha1(f"{path}|{start:.2f}|{end:.2f}|{fmt}".encode()).hexdigest()[:20]
    out = CLIP_CACHE / f"{key}.{fmt}"
    if not out.exists():
        codec = ["-c:a", "aac", "-b:a", "96k"] if fmt == "m4a" else ["-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le"]
        tmp = out.with_name(f"{key}.{os.getpid()}.{threading.get_ident()}.tmp.{fmt}")
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{start:.2f}", "-t", f"{end - start:.2f}",
             "-i", str(path), "-ac", "1", *codec, str(tmp)],
            capture_output=True, stdin=subprocess.DEVNULL, timeout=120,
        )
        if tmp.exists():
            tmp.replace(out)  # atomic: concurrent requests never see a half-written clip
    return out
