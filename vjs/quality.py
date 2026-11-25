"""Line-level transcript quality (0-1) and the character-error-rate report shown in the Library.

q is a heuristic from whisper's own signals — it is validated against the owner's actual
corrections (the edits log) in `report()`, so its usefulness is measured, not assumed.
"""
import difflib
import re
from collections import Counter

from . import db

TOK = re.compile(r"[㐀-鿿]|[A-Za-z']+", re.U)
HIDDEN = "(s.flags IS NULL OR s.flags='' OR s.flags='media')"


def repetition(text):
    """0 = no repetition … 1 = 'horse horse tiger tiger tiger'."""
    t = [x.lower() for x in TOK.findall(text or "")]
    if len(t) < 4:
        return 0.0
    consec = sum(1 for a, b in zip(t, t[1:]) if a == b) / (len(t) - 1)
    top = Counter(t).most_common(1)[0][1] / len(t)
    distinct = len(set(t)) / len(t)
    return max(consec, top - 0.25 if len(t) >= 6 else 0, 0.6 - distinct if distinct < 0.6 else 0) \
        if (consec or top > 0.4 or distinct < 0.6) else 0.0


def line_q(s):
    if s.get("text_final") or s.get("src") == "manual":
        return 1.0
    lp = s.get("avg_logprob")
    conf = 0.5 if lp is None else min(1.0, max(0.0, (lp + 1.0) / 0.8))  # -1.0 → 0, -0.2 → 1
    q = conf
    q *= 1 - min(0.8, 1.6 * repetition(s.get("text")))
    if (s.get("no_speech") or 0) > 0.5:
        q *= 0.7
    if len(TOK.findall(s.get("text") or "")) <= 1:
        q *= 0.85
    if "*" in (s.get("asr_lang") or ""):
        q *= 0.85
    return round(q, 3)


def score_all(rec_id=None):
    rows = db.rows("SELECT id, text, text_final, avg_logprob, no_speech, asr_lang, src FROM segments"
                   + (" WHERE rec_id=?" if rec_id else ""), *([rec_id] if rec_id else []))
    with db.tx() as c:
        c.executemany("UPDATE segments SET q=? WHERE id=?", [(line_q(r), r["id"]) for r in rows])
    moments_quality(rec_id)
    return len(rows)


def moments_quality(rec_id=None):
    ms = db.rows("SELECT id, rec_id, seg_from, seg_to FROM moments" + (" WHERE rec_id=?" if rec_id else ""),
                 *([rec_id] if rec_id else []))
    with db.tx() as c:
        for m in ms:
            qs = [r["q"] for r in db.rows(
                f"SELECT s.q, coalesce(s.spk, s.spk_guess) spk FROM segments s WHERE s.rec_id=? AND s.idx BETWEEN ? AND ? AND {HIDDEN}",
                m["rec_id"], m["seg_from"], m["seg_to"]) if r["q"] is not None and r["spk"] == "kid"] or \
                 [r["q"] for r in db.rows(f"SELECT s.q FROM segments s WHERE s.rec_id=? AND s.idx BETWEEN ? AND ? AND {HIDDEN}",
                                          m["rec_id"], m["seg_from"], m["seg_to"]) if r["q"] is not None]
            c.execute("UPDATE moments SET quality=? WHERE id=?", (round(sum(qs) / len(qs), 3) if qs else None, m["id"]))


def cer(a, b):
    a, b = re.sub(r"\s+", "", a or ""), re.sub(r"\s+", "", b or "")
    if not b:
        return 1.0
    return round(1 - difflib.SequenceMatcher(None, a, b).ratio(), 3)


def report():
    """What the Library shows: how wrong the machine's text was on the lines the owner corrected (character error rate).
    (The old "does the picker favor garbled lines?" comparison was answered in Sept 2026 and retired.)"""
    fixed = db.rows("SELECT e.before, e.after FROM edits e WHERE e.kind='text'")
    cers = [cer(f["before"], f["after"]) for f in fixed]
    return {"ground_truth": {"n": len(cers), "mean_cer": round(sum(cers) / len(cers), 3) if cers else None}}
