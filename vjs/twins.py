"""'Have I seen this?' (2026-10-17): a waiting card can look like one the owner already decided — either the SAME stretch of audio under another
card (overlapping range in the same recording) or a near-identical exchange on another day (a recurring game or phrase: cosine >= TWIN of the
card embeddings, vjs/lightning.py cache). The Triage card shows what it resembles, how it was decided, and a link to open it. Information only —
nothing is decided or hidden by this."""
import numpy as np

from . import db

TWIN = 0.95
_cache = {"n": -1, "ids": [], "M": None, "status": {}}


def _load():
    n = db.one("SELECT count(*) n FROM card_emb")["n"] if db.one("SELECT 1 FROM sqlite_master WHERE name='card_emb'") else 0
    if n == _cache["n"]:
        return
    rows = db.rows("SELECT e.moment_id, e.vec, m.status, m.rec_id FROM card_emb e JOIN moments m ON m.id=e.moment_id "
                   "WHERE m.status IN ('journal','ready','published','log','rejected')")
    ids = [r["moment_id"] for r in rows]
    M = np.vstack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows]) if rows else np.zeros((0, 1), np.float32)
    if len(M):
        M = M / np.linalg.norm(M, axis=1, keepdims=True)
    _cache.update(n=n, ids=ids, M=M, meta={r["moment_id"]: (r["status"], r["rec_id"]) for r in rows})


def _text(mid, k=2):
    return " / ".join(r["t"] for r in db.rows("SELECT coalesce(s.text_final, s.text) t FROM segments s JOIN moments m ON m.rec_id=s.rec_id AND s.idx BETWEEN m.seg_from AND m.seg_to "
                                               "WHERE m.id=? AND coalesce(s.flags,'') IN ('','media') ORDER BY s.idx LIMIT ?", mid, k))[:90]


def seen(m):
    """What this waiting card resembles: {'overlap': [...same audio...], 'twin': {...similar card elsewhere...}} or None."""
    if m["status"] not in ("pending", "later"):
        return None
    out = {}
    ov = db.rows("SELECT d.id, d.status FROM moments d WHERE d.rec_id=? AND d.id!=? AND d.seg_from<=? AND d.seg_to>=? "
                 "AND d.status IN ('journal','ready','published','log','rejected')", m["rec_id"], m["id"], m["seg_to"], m["seg_from"])
    if ov:
        out["overlap"] = [{"id": o["id"], "status": o["status"]} for o in ov[:3]]
    try:
        _load()
        v = db.one("SELECT vec FROM card_emb WHERE moment_id=?", m["id"])
        if v and len(_cache["ids"]):
            q = np.frombuffer(v["vec"], dtype=np.float32); q = q / np.linalg.norm(q)
            sims = _cache["M"] @ q
            order = np.argsort(-sims)[:6]
            for j in order:
                mid = _cache["ids"][j]
                st, rec = _cache["meta"][mid]
                if sims[j] < TWIN:
                    break
                if rec != m["rec_id"]:   # same-recording neighbours are the 'overlap' case above
                    r = db.one("SELECT recorded_at FROM recordings WHERE id=?", rec)
                    out["twin"] = {"id": mid, "status": st, "cos": round(float(sims[j]), 3), "date": r["recorded_at"][:10], "text": _text(mid)}
                    break
    except Exception:
        pass
    return out or None
