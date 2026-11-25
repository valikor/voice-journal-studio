# Fine-tuning Whisper on your own corrections

Every transcript line you fix in review is a labelled example: audio span + correct text. After a few thousand, a small LoRA adapter on Whisper large-v3 learns your household's words, accents and code-switching.

## Recipe (what we ran)
- Data: `segments.text_final` (your corrections) → `train.jsonl` / `val.jsonl`, **split by recording** (never by line) so validation is truly held out. Here: 1,447 train lines, 144 val lines on 21 held-out recordings.
- Model: `openai/whisper-large-v3`, bf16 on Apple MPS, LoRA r=16, alpha=32, dropout 0.05 on `q_proj k_proj v_proj out_proj`, lr 1e-4, batch 1 × grad-accum 16, gradient checkpointing, 3–4 epochs (~1–2 h on an M-series Mac with the LLM unloaded). Script: `vjs/ft_whisper.py` (own venv: torch, transformers, peft).
- Serve: merge the adapter and run `vjs/ft_server.py` (raw WAV in, text out). `asr.decode_clip` uses it for clip re-decodes and falls back to stock Whisper if it is down.

## Result (our data, held-out recordings, character error rate, lower is better)
| | CER |
|---|---|
| stock Whisper + household prompt | 0.514 |
| LoRA adapter (3 / 4 epochs) | 0.430 / 0.446 |

**Caveats, honestly:** one household, 144 lines, and our corrections started from the pipeline's own text, which flatters any "pipeline as-is" baseline; no other model we tried (bake-off of 6 ASR engines) beat the stock pipeline. Mixed English/Mandarin lines remain the weak spot (CER ≈ 0.62). Treat the numbers as directional and re-measure on yours with `vjs/bakeoff.py`.

## Practical notes
- Unload the LLM before training (`lms unload --all`); they don't fit in memory together.
- The tuned server has no timestamps: whole-file intake still uses stock whisper.cpp.
- Converting the adapter to GGML for whisper.cpp would remove the second server; we did not complete that.
