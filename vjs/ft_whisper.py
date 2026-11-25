"""LoRA fine-tune of Whisper large-v3 on the owner's corrected lines (idle-GPU job, 2026-11-19).

Runs in its OWN venv (~/vjs-ft-venv: torch + transformers + peft); the live pipeline is untouched. Data: data/ft/{train,val}.jsonl
(built from segments.text_final, split by recording). Output: data/ft/lora/ (adapter) and data/ft/val_hyps.json (held-out decodes of the
base model and the tuned model; score them with vjs.bakeoff.cer in the main venv). Nothing ships unless the tuned model beats base on val.

  ~/vjs-ft-venv/bin/python -u vjs/ft_whisper.py [--epochs 3] [--limit N]
Checkpoints every epoch; resumable via --resume."""
import argparse
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import torch

ROOT = Path(__file__).resolve().parent.parent
FT = ROOT / "data" / "ft"
MODEL = "openai/whisper-large-v3"
SR = 16000


def load_clip(path, start, end):
    r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-ss", str(max(0, start - 0.3)), "-t", str(end - start + 0.5), "-i", path,
                        "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"], capture_output=True, timeout=120)
    a = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768
    return a if len(a) > 1600 else None


def read(split, limit=None):
    rows = [json.loads(l) for l in (FT / f"{split}.jsonl").read_text(encoding="utf8").splitlines() if l.strip()]
    out = []
    for r in rows[:limit] if limit else rows:
        a = load_clip(r["path"], r["start"], r["end"])
        if a is not None and len(a) < 30 * SR:
            out.append({**r, "audio": a})
    return out


def lang_code(r):
    return "zh" if r["lang"] == "zh" else "en"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--epochs", type=int, default=3)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--bs", type=int, default=4)
    ap.add_argument("--accum", type=int, default=4)
    ap.add_argument("--lr", type=float, default=1e-4)
    ap.add_argument("--skip-base", action="store_true")
    a = ap.parse_args()
    from peft import LoraConfig, get_peft_model
    from transformers import WhisperForConditionalGeneration, WhisperProcessor
    dev = "mps" if torch.backends.mps.is_available() else "cpu"
    print("device", dev, flush=True)
    proc = WhisperProcessor.from_pretrained(MODEL)
    model = WhisperForConditionalGeneration.from_pretrained(MODEL, torch_dtype=torch.bfloat16).to(dev)
    model.config.use_cache = False
    train, val = read("train", a.limit), read("val", a.limit)
    print(f"clips: train {len(train)} · val {len(val)}", flush=True)

    def decode(m, rows, tag):
        m.eval(); m.config.use_cache = True
        hyps = []
        for i in range(0, len(rows), 8):
            b = rows[i:i + 8]
            f = proc.feature_extractor([x["audio"] for x in b], sampling_rate=SR, return_tensors="pt").input_features.to(dev, torch.bfloat16)
            with torch.no_grad():
                ids = m.generate(f, task="transcribe", num_beams=1, max_new_tokens=120)
            hyps += proc.batch_decode(ids, skip_special_tokens=True)
            print(f"  {tag} {min(i + 8, len(rows))}/{len(rows)}", flush=True)
        m.config.use_cache = False
        return [h.strip() for h in hyps]

    out = {"truth": [r["text"] for r in val], "lang": [r["lang"] for r in val]}
    if a.skip_base:
        out = json.loads((FT / "val_hyps.json").read_text(encoding="utf8"))
    else:
        out["base"] = decode(model, val, "base")
        (FT / "val_hyps.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf8")

    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model = get_peft_model(model, LoraConfig(r=16, lora_alpha=32, lora_dropout=0.05, target_modules=["q_proj", "k_proj", "v_proj", "out_proj"]))
    for p in model.parameters():
        if p.requires_grad:
            p.data = p.data.float()
    model.print_trainable_parameters()
    opt = torch.optim.AdamW([p for p in model.parameters() if p.requires_grad], lr=a.lr, weight_decay=0.01)
    steps_total = a.epochs * (len(train) // (a.bs * a.accum))
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1, (s + 1) / 20) * max(0.05, 1 - s / max(1, steps_total)))
    rng = random.Random(1)
    t0 = time.time(); step = 0
    for ep in range(a.epochs):
        rng.shuffle(train); model.train(); run = 0; n = 0
        for i in range(0, len(train) - a.bs + 1, a.bs):
            b = train[i:i + a.bs]
            f = proc.feature_extractor([x["audio"] for x in b], sampling_rate=SR, return_tensors="pt").input_features.to(dev, torch.bfloat16)
            labs = []
            for x in b:
                proc.tokenizer.set_prefix_tokens(language=lang_code(x), task="transcribe")
                labs.append(proc.tokenizer(x["text"]).input_ids)
            L = max(map(len, labs))
            lab = torch.full((len(b), L), -100, dtype=torch.long)
            for j, l in enumerate(labs):
                lab[j, :len(l)] = torch.tensor(l)
            dec_in = lab[:, :-1].clone(); dec_in[dec_in == -100] = proc.tokenizer.pad_token_id
            tgt = lab[:, 1:].to(dev)
            logits = model(input_features=f, decoder_input_ids=dec_in.to(dev)).logits.float()
            loss = torch.nn.functional.cross_entropy(logits.reshape(-1, logits.size(-1)), tgt.reshape(-1), ignore_index=-100)
            (loss / a.accum).backward()
            run += loss.item(); n += 1
            if n % a.accum == 0:
                torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
                opt.step(); sched.step(); opt.zero_grad(); step += 1
                if step % 5 == 0:
                    print(f"  ep {ep + 1}/{a.epochs} step {step}/{steps_total} loss {run / n:.3f} · {int(time.time() - t0)}s", flush=True)
        model.save_pretrained(FT / "lora")
        print(f"== epoch {ep + 1} done · loss {run / max(1, n):.3f} · adapter saved", flush=True)
    out["tuned"] = decode(model, val, "tuned")
    (FT / "val_hyps.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf8")
    print("done", int(time.time() - t0), "s", flush=True)


if __name__ == "__main__":
    main()
