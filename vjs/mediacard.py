"""Whole-card "this isn't her" detector (TV / video / another child), proposed — never applied.

Signal: voice-embedding similarity between the card's kid-labelled lines and the owner's confirmed lines of her from OTHER recordings
(top-3 mean cosine). Cheap features (whisper confidence, speaker confidence, 'media' flag, adult-likeness) were backtested first
and had no separation at all (0/13 of the owner's media cards caught). The embedding feature, backtested on the cards where the owner set
most lines to 'other': threshold 0.60 catches 3 of 6 with 5 false hits in 276 kept cards. Thin evidence, so it only raises a
banner with a one-click reject; a card the owner has partly edited is shown in Started, not touched.
"""
import numpy as np

from . import db, voice

THRESH = 0.60


def _model():
    ids, vecs = voice.load()
    pos = {int(i): k for k, i in enumerate(ids)}
    lab = voice.labels()
    rec_of = {r["id"]: r["rec_id"] for r in db.rows("SELECT id, rec_id FROM segments")}
    kid = [i for i, l in lab.items() if l == "kid" and i in pos]
    return vecs, pos, vecs[[pos[i] for i in kid]], np.array([rec_of[i] for i in kid])


def kid_sim(m, mdl):
    vecs, pos, KX, KR = mdl
    sims = []
    for r in db.rows("SELECT id, coalesce(spk, spk_guess) spk FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? AND (flags IS NULL OR flags IN ('','media'))",
                     m["rec_id"], m["seg_from"], m["seg_to"]):
        if r["spk"] != "kid" or r["id"] not in pos:
            continue
        s = (KX @ vecs[pos[r["id"]]])[KR != m["rec_id"]]
        if len(s) >= 3:
            sims.append(float(np.sort(s)[-3:].mean()))
    return float(np.mean(sims)) if sims else None


def score_all(statuses=("pending", "later")):
    mdl = _model()
    n = flagged = 0
    with db.tx() as c:
        for m in db.rows("SELECT * FROM moments WHERE status IN (%s)" % ",".join("?" * len(statuses)), *statuses):
            v = kid_sim(m, mdl)
            c.execute("UPDATE moments SET kid_sim=? WHERE id=?", (v, m["id"]))
            n += 1
            flagged += v is not None and v < THRESH
    return {"scored": n, "flagged_below_%.2f" % THRESH: flagged}
