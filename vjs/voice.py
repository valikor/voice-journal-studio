"""Speaker recognition by voice, learned from the owner's own speaker corrections.

Pitch alone can't tell a playful high-voiced adult from a preschooler (quality check 2: only 1 of 6
first-pass "her" guesses was her). A CAM++ speaker-embedding model (3D-Speaker, trained on Mandarin +
English) turns each line into a 192-number voice fingerprint; lines the owner has labelled (kid / dad / mom)
become reference voices, and every other line is matched to the closest ones.
"""
import os
from pathlib import Path

import numpy as np
import soundfile as sf

from . import db
from .audio import drop_wav, wav16k
from .config import DATA

MODEL = os.path.expanduser("~/asr-models/campplus_zh_en.onnx")
EMB = DATA / "emb"
EMB.mkdir(exist_ok=True)
SR = 16000
CLASSES = ("kid", "dad", "mom")
_ex = None


def _extractor():
    global _ex
    if _ex is None:
        import sherpa_onnx
        _ex = sherpa_onnx.SpeakerEmbeddingExtractor(sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=MODEL, num_threads=4))
    return _ex


def embed_recording(rec_id, force=False):
    out = EMB / f"{rec_id}.npz"
    if out.exists() and not force:
        return out
    rec = db.one("SELECT path FROM recordings WHERE id=?", rec_id)
    segs = db.rows("SELECT id, start, end FROM segments WHERE rec_id=? AND end - start >= 0.5", rec_id)
    if not segs:
        return None
    wav = wav16k(rec["path"], rec_id)
    audio, _ = sf.read(str(wav), dtype="float32")
    ex, ids, vecs = _extractor(), [], []
    for s in segs:
        a = audio[int(s["start"] * SR): int(s["end"] * SR)]
        if len(a) < int(0.5 * SR):
            continue
        st = ex.create_stream()
        st.accept_waveform(SR, a)
        st.input_finished()
        if not ex.is_ready(st):
            continue
        v = np.array(ex.compute(st), dtype=np.float32)
        ids.append(s["id"])
        vecs.append(v / (np.linalg.norm(v) + 1e-9))
    drop_wav(rec_id)
    np.savez_compressed(out, ids=np.array(ids), vecs=np.array(vecs))
    return out


def load(rec_ids=None):
    ids, vecs = [], []
    for f in EMB.glob("*.npz"):
        if rec_ids and f.stem not in rec_ids:
            continue
        d = np.load(f)
        ids += list(d["ids"])
        vecs.append(d["vecs"])
    return ids, (np.vstack(vecs) if vecs else np.zeros((0, 192), np.float32))


def labels():
    # typed-in lines and split turns have guessed timings — their audio isn't a clean sample of that voice
    return {r["id"]: r["spk"] for r in db.rows("SELECT id, spk FROM segments WHERE spk IN ('kid','dad','mom') "
                                               "AND coalesce(src,'') NOT IN ('manual','split')")}


TAU = None  # days; set from the evaluation (see _best_tau) — her voice changes as she grows


def _days(seg_ids):
    """Recording date (days since 2019) per segment id — used to favour labels from around the same time."""
    if not seg_ids:
        return {}
    rows = db.rows("SELECT s.id, julianday(r.recorded_at) - julianday('2019-01-01') d FROM segments s JOIN recordings r ON r.id=s.rec_id "
                   "WHERE s.id IN (%s)" % ",".join(str(int(x)) for x in seg_ids))
    return {r["id"]: r["d"] or 0.0 for r in rows}


def _knn(train_x, train_y, q, k=7, train_d=None, q_d=None, tau=None):
    """Class-balanced k-NN: each vote is divided by √(examples of that person), so a voice with few labels
    (Mom: 16 vs Dad: 151) isn't outvoted. Held-out Mom recall went 8/16 → 13/16 with no loss for the others."""
    from collections import Counter
    prior = Counter(train_y)
    sims = train_x @ q
    if tau and train_d is not None and q_d is not None:  # a label from far away in time counts for up to half as much
        sims = sims * (0.5 + 0.5 * np.exp(-np.abs(np.asarray(train_d) - q_d) / tau))
    idx = np.argsort(-sims)[:k]
    votes = {}
    for i in idx:
        votes[train_y[i]] = votes.get(train_y[i], 0) + max(0.0, float(sims[i])) / (prior[train_y[i]] ** 0.5)
    best = max(votes, key=votes.get) if votes else None
    total = sum(votes.values()) or 1
    return best, votes.get(best, 0) / total, float(sims[idx[0]]) if len(idx) else 0.0


def evaluate(tau=None):
    """Leave-one-recording-out accuracy of voice matching vs the pitch rule, on the owner's labels."""
    from .speaker import band_guess
    lab = labels()
    ids, X = load()
    pos = {sid: i for i, sid in enumerate(ids)}
    L = [(sid, spk) for sid, spk in lab.items() if sid in pos]
    if len(L) < 10:
        return {"n": len(L), "note": "not enough labelled lines with voice fingerprints yet"}
    recs = {r["id"]: r["rec_id"] for r in db.rows(
        "SELECT id, rec_id FROM segments WHERE id IN (%s)" % ",".join(str(s) for s, _ in L))}
    feats = {r["id"]: r for r in db.rows(
        "SELECT id, f0_med, f0_p90, centroid FROM segments WHERE id IN (%s)" % ",".join(str(s) for s, _ in L))}
    ok_v = ok_p = 0
    D = _days([s for s, _ in L])
    kid_ok = kid_n = 0
    for sid, spk in L:
        train = [(s2, y) for s2, y in L if recs[s2] != recs[sid]]  # never learn from the same recording
        if not train:
            continue
        tx = X[[pos[s2] for s2, _ in train]]
        pred, _, _ = _knn(tx, [y for _, y in train], X[pos[sid]], train_d=[D.get(s2, 0) for s2, _ in train], q_d=D.get(sid, 0), tau=tau)
        ok_v += pred == spk
        if spk == "kid":
            kid_n += 1; kid_ok += pred == "kid"
        ok_p += band_guess(feats[sid])[0] == spk
    from collections import Counter
    return {"n": len(L), "by_class": dict(Counter(y for _, y in L)),
            "voice_acc": round(ok_v / len(L), 3), "pitch_acc": round(ok_p / len(L), 3),
            "kid_recall": round(kid_ok / kid_n, 3) if kid_n else None, "tau": tau}


def apply(min_sim=0.35, rec_id=None):
    """Re-guess unlabelled lines from their voice fingerprint. Returns how many guesses changed.
    Until Mom has ~10 labelled lines the model can't recognise her voice, so it only decides between
    her and Dad for lines the pitch rule put in those two groups; 'mom' guesses are left alone."""
    lab = labels()
    classes = {y for y in lab.values()}
    only_kid_dad = sum(1 for y in lab.values() if y == "mom") < 10
    ids, X = load()
    pos = {sid: i for i, sid in enumerate(ids)}
    train = [(sid, y) for sid, y in lab.items() if sid in pos]
    if len(train) < 20 or len({y for _, y in train}) < 2:
        return 0
    tx, ty = X[[pos[s] for s, _ in train]], [y for _, y in train]
    unl = db.rows("SELECT id, spk_guess FROM segments WHERE spk IS NULL"
                  + (" AND spk_guess IN ('kid','dad')" if only_kid_dad else "")
                  + (" AND rec_id=?" if rec_id else ""), *([rec_id] if rec_id else []))
    changed = 0
    with db.tx() as c:
        for s in unl:
            if s["id"] not in pos:
                continue
            pred, conf, top = _knn(tx, ty, X[pos[s["id"]]])
            if top < min_sim:
                pred, conf = "other", 0.3  # sounds like nobody we know (TV, guests)
            if pred != s["spk_guess"]:
                changed += 1
            c.execute("UPDATE segments SET spk_guess=?, spk_conf=? WHERE id=?", (pred, round(conf, 3), s["id"]))
    return changed


def run_all():
    """Fingerprint every processed recording, then re-guess speakers."""
    recs = db.rows("SELECT id, filename FROM recordings WHERE status IN ('analyzed','mined') ORDER BY recorded_at DESC")
    for i, r in enumerate(recs, 1):
        if not (EMB / f"{r['id']}.npz").exists():
            embed_recording(r["id"])
            print(f"  fingerprinted {i}/{len(recs)} {r['filename']}", flush=True)
    print("evaluation:", evaluate())
    print("speaker guesses changed:", apply())


def coverage():
    """Her voice changes as she grows: how many of the owner's labels exist per half-year, vs how much audio there is.
    Half-years with audio but few 'kid' labels are where the voice model is guessing from an older/younger voice —
    those need ~10 confirmed lines (Tonight → quick checks) before their speaker guesses are trusted.
    (2026-10-07: date-weighting the matcher was tested — 91.4% → 89.7–90.9% on current labels — so it stays off;
    labels from the right age are what's missing, not weighting.)"""
    rows = db.rows("""SELECT substr(r.recorded_at,1,4) || CASE WHEN substr(r.recorded_at,6,2) <= '06' THEN '-H1' ELSE '-H2' END p,
                      sum(s.spk='kid') kid_labels, count(DISTINCT r.id) recs, round(sum(DISTINCT r.duration)/3600.0,1) h
                      FROM recordings r LEFT JOIN segments s ON s.rec_id=r.id GROUP BY p ORDER BY p""")
    return [dict(r, needs_labels=(r["kid_labels"] or 0) < 10) for r in rows]
