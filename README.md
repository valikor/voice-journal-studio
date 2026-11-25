# Voice Journal Studio

Turn the voice memos you already record of your family into a private, searchable, shareable **journal of moments** — with the child's exact words, English and Mandarin side by side, and the real audio clip beside each quote.

Everything runs **on your own Mac**: [whisper.cpp](https://github.com/ggml-org/whisper.cpp) for transcription, a local LLM in [LM Studio](https://lmstudio.ai) for nominating moments and translating, and a small browser app for review. No audio, no transcript and no name ever leaves the machine.

> Built by a parent for a bilingual household (see [`docs/WHY.md`](docs/WHY.md)). The same pipeline works for any language pair, or for one language.

## What it does

```
Voice Memos ─▶ intake ─▶ VAD + whisper ─▶ pitch + voice model → who spoke ─▶ local LLM nominates moments
                                                                          │
      ┌───────────────────────── review app (localhost) ─────────────────┘
      ▼
  TRIAGE  keep / log / skip in seconds, audio auto-plays
      ▼
  POLISH  fix words and speakers, re-listen in 2 languages, pinyin + translation suggested
      ▼
  PUBLISH → markdown entries (+ audio clips) for a static site you control
```

- **Review designed for tired parents.** One moment at a time, keyboard-first. You only spend effort where it matters.
- **Learns from your corrections.** Speaker identification, vocabulary hints and ranking improve from what you fix. An optional **Whisper LoRA fine-tune** on your own corrections beat stock Whisper by ~13–16% (character error rate) on held-out recordings in our tests — see [`docs/FINE_TUNING.md`](docs/FINE_TUNING.md).
- **Privacy by construction.** Local-only processing; a PII gate for releases (`scripts/pii_gate.py`); family-only audio on the published site. See [`docs/PRIVACY.md`](docs/PRIVACY.md).
- **Idle-GPU jobs.** Report-only, resumable background jobs (second-opinion QA, re-listen, gloss check) that use the hours your Mac would otherwise waste.

## Quick start (macOS, Apple Silicon)

```bash
# 1. tools
brew install whisper-cpp ffmpeg
# LM Studio: install, download a ~30B instruct model (we use Qwen3-30B-A3B), start the server on :1234
# whisper model: ggml-large-v3.bin and ggml-silero-v5.1.2.bin into ~/whisper-models (see docs/ARCHITECTURE.md)

# 2. python
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# 3. configure (names, language hints, where your recordings are)
cp config.example.json config.local.json      # gitignored; edit it

# 4. try the synthetic demo (no real people, no real audio)
python3 scripts/make_demo_audio.py demo/inbox
cp demo/config.demo.json config.local.json   # demo settings (a synthetic child voice has a lower pitch than the defaults expect)
./vjs.sh run        # intake + transcribe + nominate moments
./vjs.sh serve      # review app → http://localhost:8765
```

Run from **Terminal**: macOS only lets Terminal read `~/Documents` once you've allowed it.

## Layout

| | |
|---|---|
| `vjs/` | the pipeline and the review server (Python, stdlib HTTP + SQLite) |
| `web/` | the review app (plain HTML/JS/CSS, no build step) |
| `scripts/` | demo audio generator, PII gate |
| `tests/` | edit-idempotency, note-verification and notation tests |
| `docs/` | architecture, privacy model, fine-tuning recipe |

## Status

Real-world use since 2026 on ~640 recordings / ~75,000 child lines. It is a personal tool made public: expect rough edges, macOS-specific paths and opinions. Issues and ideas welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

MIT © 2026 David Pritts. See [`LICENSE`](LICENSE).
