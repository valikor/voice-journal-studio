# Architecture

One SQLite file (`data/studio.db`), one small HTTP server (`vjs/server.py`), plain-JS front end (`web/`). No cloud, no framework.

## Data model
- **recordings** — one row per audio file (path, date, title, status).
- **segments** — one row per whisper line: `start/end`, `text` (as heard), `text_final` (your correction), `spk` (your label) / `spk_guess` (model guess), flags (`dup`, `media`, …).
- **moments** — a *card*: a window of segments (`seg_from..seg_to`, by `idx`), score, tags, status (`pending | later | journal | log | rejected | ready | published`), star, public note.
- **moment_lines** — the Polish draft of a card: speaker, verbatim text, `rendered` house-style line (with translation/pinyin), include flag.
- **edits** — an audit log of every correction (this is also the training set for learning).

## Pipeline (`./vjs.sh run`)
1. **ingest** — find new audio in `sources`, skip files you tagged as work (Finder label).
2. **ASR** — VAD splits speech, whisper.cpp transcribes each chunk (beam search, an initial prompt of your household vocabulary).
3. **speaker** — pitch bands give a first guess; once you have corrected enough lines, a nearest-neighbour voice model trained on *your* labels takes over (`speaker.py`, `voice.py`).
4. **rules** — optional "who can be in this recording" rules from config (`rules.py`).
5. **mine** — the local LLM reads windows of ~45 lines and nominates moments with a score and tags (`mine.py`). `rank.py` can reorder by a pairwise tournament, enabled only if it beats the current order on your past decisions.
6. **review app** — Triage → Polish → Publish.

## Local models
- Whisper large-v3 via `whisper-server` (:8178). Optionally a LoRA-tuned copy served by `vjs/ft_server.py` (:8179); `asr.decode_clip` prefers it and falls back to stock when it is down.
- An LLM behind an OpenAI-compatible endpoint (LM Studio, :1234). JSON-schema-constrained calls only (`llm.py`).
- 36 GB unified memory runs Whisper + a 17 GB model comfortably; training needs the LLM unloaded.

## Publishing
`publish.py` writes one markdown block per card into a static-site repo you point `journal_repo` at, and `journal_audio.py` cuts a normalised clip per published card. The site itself is yours; the privacy model we use is in `PRIVACY.md`.

## Idle-GPU jobs
`vjs/idle*.py`-style jobs are *report-only*: they write markdown reports and key-value progress, never change cards, and resume where they stopped. A `data/PAUSE` file pauses them between items.
