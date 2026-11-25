"""Head-to-head of two local models on the owner's own work (2026-09-29, decision F).

A · Translation — the answer key is the published journal's hand-curated quotes ("汉字" *(translation)*), excluding
    quotes this pipeline drafted. Each quote is translated with its neighbouring quotes as context and NO worked
    examples, so neither model can copy an answer. Scored by closeness to the owner's translation: meaning (embedding
    cosine, local nomic model) and wording (character n-gram F-score).
B · Polish — the owner's mechanical Polish edits (same words: punctuation, trims, case). Scored by closeness to his
    final line vs leaving the line as it was.
C · Side-by-side — both models' auto-polish proposals on real untouched Polish cards, for the owner to eyeball.
Nothing here writes to moment_lines."""
import json
import re
import subprocess
import time
from collections import Counter
from pathlib import Path

import numpy as np
import requests

from . import db, llm
from .config import CFG, DATA
from .polish import TR_SCHEMA, TR_SYSTEM

EMO = {"👧": "KID", "👨": "DAD", "👩": "MOM"}


def use(model):
    """One model at a time: 36 GB can't hold the 9B, the 30B, whisper and the desktop together."""
    llm.L["model"] = model
    subprocess.run(["lms", "unload", "--all"], capture_output=True, timeout=300)
    r = subprocess.run(["lms", "load", model, "-y", "--context-length", "16384"], capture_output=True, text=True, timeout=900)
    ps = subprocess.run(["lms", "ps"], capture_output=True, text=True).stdout
    if model.split("/")[-1] not in ps:
        raise RuntimeError(f"could not load {model}: {(r.stdout + r.stderr)[-300:]}")


def _embed(texts):
    r = requests.post(llm.L["base_url"] + "/embeddings", json={"model": "text-embedding-nomic-embed-text-v1.5", "input": texts}, timeout=120)
    v = np.array([d["embedding"] for d in r.json()["data"]], dtype=float)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def chrf(a, b, n=3):
    def grams(t):
        t = re.sub(r"\s+", " ", t.lower().strip())
        return Counter(t[i:i + n] for i in range(max(1, len(t) - n + 1)))
    ga, gb = grams(a), grams(b)
    ov = sum((ga & gb).values())
    if not ov:
        return 0.0
    p, r = ov / sum(ga.values()), ov / sum(gb.values())
    return 2 * p * r / (p + r)


def translation_items():
    root = Path(CFG["journal_repo"]) / "src" / "content" / "entries"
    items = []
    for f in sorted(root.glob("*/*.md")):
        t = f.read_text(encoding="utf-8")
        if re.search(r"^audience:.*private", t, re.M):
            continue
        body = re.sub(r"<!-- vjs:m\d+ -->.*?<!-- /vjs -->", "", t.split("---", 2)[-1], flags=re.S)
        paras = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
        for i, p in enumerate(paras):
            m = re.search(r'"([^"]*[㐀-鿿][^"]*)"\s*\*\(([^)]+)\)\*', p)
            if not m:
                continue
            ctx = []
            for j in range(max(0, i - 4), min(len(paras), i + 3)):
                q = re.search(r'"([^"]+)"', paras[j])
                if not q:
                    continue
                who = next((v for k, v in EMO.items() if paras[j].startswith(k) or paras[j].lstrip("⭐ ").startswith(k)), "KID")
                ctx.append((">>" if j == i else "  ") + f" [{j}] {who}: {q.group(1)}")
            items.append({"file": f.name, "id": i, "zh": m.group(1), "gold": m.group(2).strip(), "ctx": ctx})
    return items


def run_translation(items):
    out, t0 = [], time.time()
    for it in items:
        user = "Conversation:\n" + "\n".join(it["ctx"]) + f"\n\nReturn exactly 1 result, for id: [{it['id']}]"
        try:
            r = llm.chat_json(TR_SYSTEM, user, TR_SCHEMA, max_tokens=800)
            tr = next((x["translation"] for x in r.get("lines", []) if x.get("id") == it["id"]), "")
        except Exception as e:
            tr = ""
        out.append(tr)
    return out, (time.time() - t0) / max(1, len(items))


def polish_items():
    from .autopolish import _not_subsequence, _words
    def qt(r):
        r = re.sub(r"\*\([^)]*\)\*|\*\[[^\]]*\]\*", " ", r or "")
        return " ".join(re.findall(r'"([^"]*)"', r)).strip()
    first, last = {}, {}
    for e in db.rows("SELECT seg_id, moment_id, before, after FROM edits WHERE kind='rendered' AND via='polish' ORDER BY id"):
        k = (e["moment_id"], e["seg_id"])
        first.setdefault(k, e["before"]); last[k] = e["after"]
    items = []
    for (mid, sid), b in first.items():
        a = last[(mid, sid)]
        qb, qa = qt(b), qt(a)
        if qb and qa and qb != qa and "{" not in a and not re.search(r"\s{2,}", qa) and not _not_subsequence(_words(qa), _words(qb)):
            spk = next((v for k, v in EMO.items() if (b or "").startswith(k)), "OTHER")
            items.append({"moment": mid, "before": qb, "gold": qa, "speaker": spk})
    return items


def run_polish(items):
    from .autopolish import SCHEMA, SYSTEM
    out, t0 = [], time.time()
    for it in items:
        user = f"Draft:\n[1] {it['speaker']}: {it['before']}\n\nReturn one entry per id, same order: [1]"
        try:
            r = llm.chat_json(SYSTEM, user, SCHEMA, max_tokens=800)
            out.append(next((x["text"] for x in r.get("lines", []) if x.get("id") == 1), it["before"]))
        except Exception:
            out.append(it["before"])
    return out, (time.time() - t0) / max(1, len(items))


def run(models, polish_cards=6):
    A, B = translation_items(), polish_items()
    cards = [r["id"] for r in db.rows("SELECT m.id FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status='journal' "
                                      "AND NOT EXISTS (SELECT 1 FROM moment_lines l WHERE l.moment_id=m.id AND l.edited=1) "
                                      "ORDER BY r.recorded_at DESC LIMIT ?", polish_cards)]
    res = {"A_items": A, "B_items": B, "cards": cards, "models": {}}
    for mdl in models:
        print(f"== {mdl}", flush=True)
        use(mdl)
        ta, sa = run_translation(A)
        print(f"   translation: {len(A)} quotes, {sa:.1f}s each", flush=True)
        pb, sb = run_polish(B)
        print(f"   polish lines: {len(B)}, {sb:.1f}s each", flush=True)
        from .autopolish import propose
        props = []
        for c in cards:
            try:
                props.append(propose(c))
            except Exception as e:
                props.append({"moment": c, "error": str(e)})
        res["models"][mdl] = {"tr": ta, "tr_s": sa, "pol": pb, "pol_s": sb, "props": props}
    gold = _embed([a["gold"] for a in A])
    for mdl, r in res["models"].items():
        got = _embed([t or "(none)" for t in r["tr"]])
        r["tr_meaning"] = [float(x) for x in (gold * got).sum(1)]
        r["tr_chrf"] = [chrf(t, a["gold"]) for t, a in zip(r["tr"], A)]
        r["pol_gain"] = [chrf(p, b["gold"]) - chrf(b["before"], b["gold"]) for p, b in zip(r["pol"], B)]
        r["pol_exact"] = [re.sub(r"\s+", " ", p).strip() == b["gold"] for p, b in zip(r["pol"], B)]
    use(models[0])  # back to the everyday model
    out = DATA / "exports" / f"model_compare_{time.strftime('%Y%m%d_%H%M')}.json"
    out.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print("→", out)
    return res
