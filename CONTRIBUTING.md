# Contributing

Issues and pull requests are welcome, especially: Linux/Windows paths, other language pairs, a GGML conversion of the LoRA adapter, and docs.

- Run the tests: `./.venv/bin/python tests/test_notes.py`, `tests/test_idempotent_edits.py`, `node tests/test_notation.js`.
- Never commit recordings, transcripts, `data/`, `config.local.json` or real names. Run `python3 scripts/pii_gate.py . --deny your-denylist.json` before you push.
- Keep the review app dependency-free (no build step).
