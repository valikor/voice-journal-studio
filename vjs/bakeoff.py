"""ASR bake-off on the owner's own corrections: every line he fixed is ground truth. Each contender decodes
the same audio clip; score = character error rate vs his text (lower is better)."""
import io
import json
import os
import re
import statistics
import time

import numpy as np
import requests
import soundfile as sf

from . import db
from .asr import BASE, asr_prompt
from .audio import clip
from .textutil import lang_of

SV_DIR = os.path.expanduser("~/asr-models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09")
NORM = re.compile(r"[\W_]+", re.U)


def cer(hyp, ref):
    import difflib
    h, r = NORM.sub("", (hyp or "").lower()), NORM.sub("", (ref or "").lower())
    if not r:
        return None
    # edit distance / len(ref), via difflib opcodes
    sm = difflib.SequenceMatcher(None, h, r, autojunk=False)
    dist = sum(max(i2 - i1, j2 - j1) for op, i1, i2, j1, j2 in sm.get_opcodes() if op != "equal")
    return min(1.0, dist / len(r))


def cer_in(hyp, ref):
    """CER of ref against the best-matching stretch of hyp (all contenders heard the same extra context)."""
    h = NORM.sub("", (hyp or "").lower()); r = NORM.sub("", (ref or "").lower())
    if not r:
        return None
    if len(h) <= len(r):
        return cer(hyp, ref)
    best = 1.0
    for L in {max(1, int(len(r) * f)) for f in (0.8, 0.9, 1.0, 1.1, 1.25)}:
        for i in range(0, max(1, len(h) - L + 1)):
            best = min(best, cer(h[i:i + L], r))
            if best == 0:
                return 0.0
    return best


def testset():
    rows = db.rows("""
        SELECT s.id, s.start, s.end, s.text_final truth, r.path,
               (SELECT e.before FROM edits e WHERE e.seg_id=s.id AND e.kind='text' ORDER BY e.id LIMIT 1) pipeline
        FROM segments s JOIN recordings r ON r.id=s.rec_id
        WHERE s.text_final IS NOT NULL AND coalesce(s.src,'') != 'manual' AND s.end - s.start >= 0.3""")
    keep = []
    for r in rows:
        t = r["truth"] or ""
        if not r["pipeline"] or re.search(r"<[^>]*>|unintelligible|\?\?\?", t, re.I) or len(NORM.sub("", t)) < 2:
            continue
        keep.append(r)
    return keep


def _whisper(audio, beam, prompt):
    buf = io.BytesIO()
    sf.write(buf, audio, 16000, format="WAV", subtype="PCM_16")
    buf.seek(0)
    data = {"response_format": "json", "language": "auto", "temperature": "0", "temperature_inc": "0.2"}
    if beam:
        data["beam_size"] = str(beam)
    if prompt:
        data["prompt"] = prompt
    return requests.post(BASE + "/inference", files={"file": ("a.wav", buf, "audio/wav")}, data=data, timeout=300).json().get("text", "").strip()


_sv = None


def _sensevoice(audio):
    global _sv
    import sherpa_onnx
    if _sv is None:
        _sv = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=f"{SV_DIR}/model.int8.onnx", tokens=f"{SV_DIR}/tokens.txt", use_itn=True, num_threads=4, language="auto")
    s = _sv.create_stream()
    s.accept_waveform(16000, audio)
    _sv.decode_stream(s)
    return s.result.text.strip()


def run(out_path=None, context=0.0):
    rows = testset()
    prompt = asr_prompt()
    contenders = {
        "whisper greedy + prompt (current)": lambda a: _whisper(a, 0, prompt),
        "whisper beam-5 + prompt": lambda a: _whisper(a, 5, prompt),
        "whisper beam-5, no prompt": lambda a: _whisper(a, 5, ""),
        "SenseVoice-Small": _sensevoice,
    }
    results = {k: [] for k in ["pipeline as-is (what you saw)", *contenders]}
    per_line = []
    timing = {k: 0.0 for k in contenders}
    for i, r in enumerate(rows, 1):
        pad_s = 0.3 + context
        try:
            a, _ = sf.read(str(clip(r["path"], max(0, r["start"] - pad_s), r["end"] + 0.1 + pad_s, fmt="wav")), dtype="float32")
        except Exception as e:
            print(f"[{i}/{len(rows)}] skipped (clip unreadable): {e}", flush=True)
            continue
        score = cer_in if context else cer
        pad = np.zeros(int(0.2 * 16000), np.float32)
        a = np.concatenate([pad, a, pad])
        line = {"truth": r["truth"], "lang": lang_of(r["truth"]), "pipeline": r["pipeline"]}
        results["pipeline as-is (what you saw)"].append((cer(r["pipeline"], r["truth"]), line["lang"]))
        for k, fn in contenders.items():
            t = time.time()
            try:
                hyp = fn(a)
            except Exception as e:
                hyp = ""
                print("   ", k, "failed:", e)
            timing[k] += time.time() - t
            line[k] = hyp
            results[k].append((score(hyp, r["truth"]), line["lang"]))
        per_line.append(line)
        print(f"[{i}/{len(rows)}] {r['truth'][:40]}", flush=True)

    def summ(v):
        xs = [c for c, _ in v if c is not None]
        return {"n": len(xs), "mean": round(statistics.mean(xs), 3), "median": round(statistics.median(xs), 3),
                "usable": round(sum(x <= 0.1 for x in xs) / len(xs), 3)}
    table = {}
    for k, v in results.items():
        table[k] = {"all": summ(v), **{L: summ([x for x in v if x[1] == L]) for L in ("en", "zh", "mixed") if any(x[1] == L for x in v)}}
        if k in timing:
            table[k]["sec_per_line"] = round(timing[k] / max(1, len(rows)), 2)
    out = {"n": len(rows), "table": table, "lines": per_line}
    db.kv_set("bakeoff", {"n": len(rows), "table": table, "at": time.strftime("%Y-%m-%d %H:%M")})
    if out_path:
        with open(out_path, "w") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    return out
