"""⚡ Lightning Round (2026-10-12): a 3-minute, text-only fast pass over the cards the model is least sure about,
then a "sweep" of the backlog cards that look just like what you've been passing on.

Model: logistic regression on the card's text embedding (nomic via LM Studio, cached in card_emb) + how much she says,
trained on every card you decided (kept = journal/ready/published, else not kept). Cross-validated by recording.
The sweep only exists if a threshold on the cross-validated scores filed >= SWEEP_PRECISION of decided cards correctly
as "not kept" (in a bucket of >= 40 cards) — otherwise it says so and offers nothing. Sweeps go to 'log' (her speech
still counts in the language log), are logged in `edits` (kind='sweep', via='lightning') and are undoable."""
import hashlib
import random
import time

import numpy as np

from . import db, edits
from .learn import DECIDED, KEPT, trust
from .rank import card_text

SWEEP_PRECISION = 0.93
MIN_BUCKET = 40
_cache = {"at": 0, "state": None}


def _ensure_table():
    with db.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS card_emb(moment_id INTEGER PRIMARY KEY, sig TEXT, vec BLOB)")


def _embeddings(ms):
    """Cached nomic embedding per card; re-embedded only if the card's text changed."""
    from .compare import _embed
    _ensure_table()
    have = {r["moment_id"]: r for r in db.rows("SELECT moment_id, sig, vec FROM card_emb")}
    texts = {m["id"]: (card_text(m) or "(empty)") for m in ms}
    sig = {i: hashlib.md5(t.encode()).hexdigest()[:12] for i, t in texts.items()}
    todo = [i for i in texts if i not in have or have[i]["sig"] != sig[i]]
    for k in range(0, len(todo), 64):
        chunk = todo[k:k + 64]
        vecs = _embed([texts[i] for i in chunk])
        with db.tx() as c:
            c.executemany("INSERT OR REPLACE INTO card_emb(moment_id, sig, vec) VALUES(?,?,?)",
                          [(i, sig[i], v.astype(np.float32).tobytes()) for i, v in zip(chunk, vecs)])
        for i, v in zip(chunk, vecs):
            have[i] = {"vec": v.astype(np.float32).tobytes()}
    return np.vstack([np.frombuffer(have[m["id"]]["vec"], dtype=np.float32) for m in ms])


_STRUCT = ["kid_chars", "turn_rate", "turns", "excl", "f0_mean", "lines", "kid_lines", "longest_kid", "kid_rate"]


def _struct(m):
    """Leak-free structure of the card: only the machine's own speaker guesses and raw ASR text are used, because on cards
    you've kept the speakers/text were corrected by you and pending cards never have that (it inflated early scores)."""
    segs = db.rows("SELECT start, end, spk_guess sp, text, f0_med FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? "
                   "AND coalesce(flags,'') IN ('','media') ORDER BY idx", m["rec_id"], m["seg_from"], m["seg_to"])
    kid = [x for x in segs if (x["sp"] or "other") == "kid"]
    kt = " ".join(x["text"] or "" for x in kid)
    turns = sum(1 for a, b in zip(segs, segs[1:]) if (a["sp"] or "o") != (b["sp"] or "o"))
    f0 = [x["f0_med"] for x in kid if x["f0_med"]]
    kd = sum(x["end"] - x["start"] for x in kid)
    return [np.log1p(len(kt)) / 6, turns / max(1, len(segs)), min(turns, 12) / 12, min(kt.count("!"), 4) / 4,
            (np.mean(f0) if f0 else 0) / 400, min(len(segs), 30) / 30, min(len(kid), 15) / 15,
            min(max((len(x["text"] or "") for x in kid), default=0), 120) / 120,
            min(len(kt) / max(0.1, kd), 30) / 30 if kd else 0], len(kt)


def _X(ms):
    emb = _embeddings(ms)
    st = [_struct(m) for m in ms]
    num = np.array([s[0] for s in st])
    return np.hstack([emb, num * 3]), np.array([s[1] for s in st], dtype=float)


def model(force=False):
    """Fit on everything decided; CV predictions on decided, probabilities for pending. Cached 60 s."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    if not force and _cache["state"] and time.time() - _cache["at"] < 60:
        return _cache["state"]
    dec = db.rows(f"SELECT * FROM moments WHERE status IN {DECIDED} AND id NOT IN ({db.SWEPT_SQL})")
    pend = db.rows("SELECT * FROM moments WHERE status='pending'")
    Xd, kd = _X(dec)
    Xp, kp = _X(pend)
    y = np.array([m["status"] in KEPT for m in dec], dtype=int)
    groups = np.array([m["rec_id"] for m in dec])
    cv = np.zeros(len(dec))
    for tr, te in GroupKFold(n_splits=5).split(Xd, y, groups):
        cv[te] = LogisticRegression(C=0.5, max_iter=2000).fit(Xd[tr], y[tr]).predict_proba(Xd[te])[:, 1]
    clf = LogisticRegression(C=0.5, max_iter=2000).fit(Xd, y)
    pp = clf.predict_proba(Xp)[:, 1] if len(pend) else np.array([])
    # sweep threshold: biggest bucket of decided cards (cv p < t) that were "not kept" >= SWEEP_PRECISION of the time
    thr, prec, nb = None, None, 0
    for t in np.unique(np.round(cv, 3)):
        sel = cv < t
        if sel.sum() >= MIN_BUCKET and 1 - y[sel].mean() >= SWEEP_PRECISION and sel.sum() > nb:
            thr, prec, nb = float(t), float(1 - y[sel].mean()), int(sel.sum())
    st = {"dec": dec, "pend": pend, "y": y, "cv": cv, "pp": pp, "kp": kp, "emb_p": Xp[:, :-len(_STRUCT)], "thr": thr, "prec": prec, "nb": nb,
          "auc": float(roc_auc_score(y, cv)) if len(set(y)) > 1 else 0.5, "base": float(y.mean()), "n": len(dec)}
    _cache.update(at=time.time(), state=st)
    return st


def _card(m, p=None, audio=True):
    segs = db.rows("SELECT idx, start, end, coalesce(spk, spk_guess, 'other') spk, coalesce(text_final, text) t FROM segments "
                   "WHERE rec_id=? AND idx BETWEEN ? AND ? AND coalesce(flags,'') IN ('','media') ORDER BY idx",
                   m["rec_id"], m["seg_from"] - 2, m["seg_to"])
    rec = db.one("SELECT recorded_at FROM recordings WHERE id=?", m["rec_id"])
    inside = [x for x in segs if x["idx"] >= m["seg_from"]]
    out = {"id": m["id"], "date": rec["recorded_at"], "p": None if p is None else round(float(p), 3), "rec": m["rec_id"],
           "ctx": [{"spk": x["spk"], "t": x["t"]} for x in segs if x["idx"] < m["seg_from"]],  # two lines before, shown faint
           "lines": [{"spk": x["spk"], "t": x["t"]} for x in inside]}
    if audio and inside:  # same clip window the Triage card plays
        out["clip"] = {"start": max(0, inside[0]["start"] - 0.5), "end": inside[-1]["end"] + 0.6}
    return out


def make_round(n=30, random_n=6):
    """Mostly the cards the model is least sure about (spread out so they aren't all alike), plus a few random ones
    so the round also measures how good the model's guesses are."""
    st = model(force=True)
    pend, pp, kp = st["pend"], st["pp"], st["kp"]
    idx = [i for i in range(len(pend)) if kp[i] >= 15]
    rnd = random.Random(time.time())
    rand_ids = set(rnd.sample(idx, min(random_n, len(idx))))
    unc = sorted((i for i in idx if i not in rand_ids), key=lambda i: abs(pp[i] - st["base"]))[:150]
    chosen, E = [], st["emb_p"]
    for _ in range(n - len(rand_ids)):  # farthest-point: each pick is unlike the ones already chosen
        if not unc:
            break
        if not chosen:
            best = unc[0]
        else:
            best = max(unc, key=lambda i: min(1 - float(E[i] @ E[j]) for j in chosen))
        chosen.append(best); unc.remove(best)
    out = [(i, False) for i in chosen] + [(i, True) for i in rand_ids]
    rnd.shuffle(out)
    return [dict(_card(pend[i], pp[i]), rand=r) for i, r in out]


def sweep_preview():
    st = model()
    if st["thr"] is None:
        return {"available": False, "reason": f"On your {st['n']} past decisions no group of cards could be filed away at "
                f"{SWEEP_PRECISION:.0%} accuracy yet — more answers from the round will help.", "auc": round(st["auc"], 3)}
    pend, pp, kp = st["pend"], st["pp"], st["kp"]
    ids = [pend[i] for i in range(len(pend)) if pp[i] < st["thr"] and kp[i] >= 15]
    ids.sort(key=lambda m: m["id"])
    return {"available": True, "threshold": st["thr"], "precision": round(st["prec"], 3), "bucket_eval": st["nb"],
            "auc": round(st["auc"], 3), "count": len(ids), "cards": [_card(m, pp[pend.index(m)], audio=False) for m in ids[:300]],
            "ids": [m["id"] for m in ids]}


def apply_sweep(ids):
    n = 0
    with db.tx() as c:
        for i in ids:
            m = db.one("SELECT status FROM moments WHERE id=?", i)
            if m and m["status"] == "pending":
                c.execute("UPDATE moments SET status='log', triaged_at=datetime('now','localtime') WHERE id=?", (i,))
                edits.log(c, "sweep", "pending", "log", moment_id=i, via="lightning")
                n += 1
    _cache["state"] = None
    return n


def undo_sweep():
    """Puts back every swept card that hasn't been touched since (still 'log' with its latest edit being the sweep)."""
    rows = db.rows("SELECT moment_id FROM edits e WHERE kind='sweep' AND via='lightning' AND id=(SELECT max(id) FROM edits "
                   "WHERE moment_id=e.moment_id AND kind IN ('sweep','triage'))")
    n = 0
    with db.tx() as c:
        for r in rows:
            m = db.one("SELECT status FROM moments WHERE id=?", r["moment_id"])
            if m and m["status"] == "log":
                c.execute("UPDATE moments SET status='pending', triaged_at=NULL WHERE id=?", (r["moment_id"],))
                edits.log(c, "sweep-undo", "log", "pending", moment_id=r["moment_id"], via="lightning")
                n += 1
    _cache["state"] = None
    return n
