"""Local server for the fine-tuned Whisper (merged LoRA, data/ft/merged). Run in ~/vjs-ft-venv on 127.0.0.1:8179.
POST /inference  — body: raw 16 kHz mono WAV bytes; query: ?language=en|zh|auto&prompt=...   → {"text": "..."}
Same job as whisper-server on :8178, which stays the live default until the tuned model is switched on in asr.py."""
import io, json, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import numpy as np, soundfile as sf, torch
from transformers import WhisperForConditionalGeneration, WhisperProcessor

MERGED = Path(__file__).resolve().parent.parent / "data" / "ft" / "merged"
dev = "mps" if torch.backends.mps.is_available() else "cpu"
proc = WhisperProcessor.from_pretrained(MERGED)
model = WhisperForConditionalGeneration.from_pretrained(MERGED, torch_dtype=torch.bfloat16).to(dev).eval()
LOCK = __import__("threading").Lock()


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        u = urlparse(self.path); q = {k: v[0] for k, v in parse_qs(u.query).items()}
        a, sr = sf.read(io.BytesIO(self.rfile.read(int(self.headers.get("Content-Length", 0)))), dtype="float32")
        if a.ndim > 1: a = a.mean(1)
        f = proc.feature_extractor(a[: 30 * 16000], sampling_rate=16000, return_tensors="pt").input_features.to(dev, torch.bfloat16)
        kw = {"task": "transcribe", "num_beams": 1, "max_new_tokens": 120}
        if q.get("language", "auto") != "auto": kw["language"] = q["language"]
        if q.get("prompt"): kw["prompt_ids"] = torch.tensor(proc.get_prompt_ids(q["prompt"])).to(dev)
        with LOCK, torch.no_grad():
            ids = model.generate(f, **kw)
        text = proc.batch_decode(ids, skip_special_tokens=True)[0].strip()
        body = json.dumps({"text": text}, ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def log_message(self, *a): pass


if __name__ == "__main__":
    print("tuned whisper ready on", dev, flush=True)
    ThreadingHTTPServer(("127.0.0.1", 8179), H).serve_forever()
