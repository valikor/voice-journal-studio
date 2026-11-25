"""Two no-loss queue cuts (the owner, 2026-10-25):
1. dupskip — a waiting card that is a near-copy (cosine >= 0.90) of a card in ANOTHER recording that is already decided/filed is
   filed as auto_skipped (logged kind 'dupskip' with the original's id; `restore` puts it back to waiting). Same scene recorded again.
2. park — waiting cards of <=2 lines you haven't touched leave New and wait in their own "Parked small cards" pile (moments.parked_at);
   Quick still draws them, so they can be skimmed at ~4 s a card. Cards with a strong punchline pick (punch_score>=5) stay in New.
Nothing is deleted or rewritten; `restore` undoes both.
"""
import numpy as np

from . import db

DECIDED = ("journal", "ready", "published", "log", "rejected", "later", "auto_skipped", "thin")
SIM = 0.90


def find_dups():
    rows = db.rows("SELECT e.moment_id id, e.vec, m.status, m.rec_id, m.touched_at FROM card_emb e JOIN moments m ON m.id=e.moment_id "
                   "WHERE m.status NOT IN ('merged','superseded','removed','adhoc','empty')")
    V = np.array([np.frombuffer(r["vec"], dtype=np.float32) for r in rows])
    V /= (np.linalg.norm(V, axis=1, keepdims=True) + 1e-9)
    pend = [i for i, r in enumerate(rows) if r["status"] == "pending" and not r["touched_at"]]
    S = V[pend] @ V.T
    out = []
    for a, i in enumerate(pend):
        s = S[a].copy(); s[i] = 0
        hits = [h for h in np.where(s >= SIM)[0] if rows[h]["rec_id"] != rows[i]["rec_id"] and rows[h]["status"] in DECIDED]
        if hits:
            best = max(hits, key=lambda h: s[h])
            out.append((rows[i]["id"], rows[best]["id"], float(s[best]), rows[best]["status"]))
    return out


def apply():
    dups = find_dups()
    with db.tx() as c:
        for mid, orig, sim, st in dups:
            c.execute("UPDATE moments SET status='auto_skipped' WHERE id=? AND status='pending'", (mid,))
            c.execute("INSERT INTO edits(kind, moment_id, before, after, via) VALUES('dupskip',?,?,?,?)", (mid, "pending", f"copy of #{orig} ({st}, sim {sim:.2f})", f"sim={SIM}"))
        c.execute("UPDATE moments SET parked_at=datetime('now','localtime') WHERE status='pending' AND touched_at IS NULL AND parked_at IS NULL "
                  "AND seg_to-seg_from+1<=2 AND coalesce(punch_score,0)<5")
        parked = c.execute("SELECT count(*) FROM moments WHERE status='pending' AND parked_at IS NOT NULL").fetchone()[0]
    return {"dupskipped": len(dups), "parked": parked, "new_left": db.one("SELECT count(*) n FROM moments WHERE status='pending' AND touched_at IS NULL AND parked_at IS NULL")["n"]}


def restore():
    with db.tx() as c:
        ids = [r[0] for r in c.execute("SELECT moment_id FROM edits WHERE kind='dupskip'")]
        for mid in ids:
            c.execute("UPDATE moments SET status='pending' WHERE id=? AND status='auto_skipped'", (mid,))
        c.execute("DELETE FROM edits WHERE kind='dupskip'")
        c.execute("UPDATE moments SET parked_at=NULL")
    return {"restored_dups": len(ids), "unparked": True}
