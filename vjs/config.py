"""Configuration: config.json (shareable defaults) merged with config.local.json
(gitignored — family names, ASR vocabulary prompt, private paths)."""
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

DEFAULTS = {
    # Folders scanned for new audio. The Voice Memos library itself is TCC-protected;
    # add it here only if the terminal running vjs has Full Disk Access.
    "sources": [
        "~/Documents/Recordings",
        "~/VoiceMemosInbox",
    ],
    "audio_exts": [".m4a", ".wav", ".mp3", ".aac", ".caf", ".qta", ".mp4", ".mov"],
    # Recordings older than this are ignored (before the kid was talking).
    "min_date": "2019-01-01",  # 2026-10-07: whole archive in scope (the owner: "work on the backlog")
    # Voice Memos database copy (optional) — supplies recording titles/places.
    "voice_memos_db": "~/Documents/Recordings/CloudRecordings.db",
    # Summaries from the earlier batch pipeline — only used to transcribe family recordings first.
    "prior_summaries": "~/Documents/Recordings/review/review_index.tsv",

    "whisper": {
        "server_bin": "/opt/homebrew/bin/whisper-server",
        "model": "~/whisper-models/ggml-large-v3.bin",
        "vad_model": "~/whisper-models/ggml-silero-v5.1.2.bin",
        "host": "127.0.0.1",
        "port": 8178,
        "threads": 8,
        # beam search beat greedy decoding in the 2026-09-24 bake-off on the owner's corrections (≈ same speed)
        "beam_size": 5,
    },
    "llm": {
        "base_url": "http://localhost:1234/v1",
        "model": "qwen/qwen3-30b-a3b-2507",  # 2026-09-29: switched from qwen/qwen3.5-9b (the owner, after data/exports/model_compare_20260929_1313.json)
        "lms_bin": "~/.lmstudio/bin/lms",
        "window_lines": 45,
        "window_overlap": 6,
    },
    # Pitch bands (Hz) for the first-pass speaker guess. Once you've corrected
    # enough speakers in review, a nearest-neighbour model trained on your labels
    # takes over (see speaker.py).
    "pitch": {"kid_min": 265, "adult_female_min": 165},
    # A kid segment auto-enters the language log (unreviewed tier) if whisper was
    # at least this confident.
    "log_auto_min_logprob": -0.55,
    "mine_min_kid_segments": 4,

    "journal_repo": "~/journal-site",
    "review_port": 8765,

    # Display labels only. Real names belong in config.local.json.
    "speakers": {
        "kid": {"label": "Kid", "emoji": "👧"},
        "dad": {"label": "Dad", "emoji": "👨"},
        "mom": {"label": "Mom", "emoji": "👩"},
        "other": {"label": "Other", "emoji": "🗣️"},
    },
    # Initial prompt biasing whisper toward household vocabulary.
    "asr_prompt": "",
    # Every spelling of one person, grouped: [["Pinyin/Latin form", "简体 form", "nickname", ...], ...]; first item is canonical.
    # Real names live ONLY in config.local.json. ASR bias, note verification and the lexicon treat each group as one entity.
    "name_groups": [],
    # the owner's typing shorthand, expanded when a line is rendered for the journal: {"xkx": "<hanzi>"} (config.local.json)
    "shorthand": {},
}


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _expand(v):
    if isinstance(v, str) and v.startswith("~"):
        return os.path.expanduser(v)
    if isinstance(v, list):
        return [_expand(x) for x in v]
    if isinstance(v, dict):
        return {k: _expand(x) for k, x in v.items()}
    return v


def load():
    cfg = DEFAULTS
    for name in ("config.json", "config.local.json"):
        p = ROOT / name
        if p.exists():
            cfg = _merge(cfg, json.loads(p.read_text(encoding="utf-8")))
    return _expand(cfg)


CFG = load()
DATA.mkdir(exist_ok=True)
