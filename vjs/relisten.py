"""Second listen: re-transcribe a whole moment as ONE clip (whisper gets the full context) and
compare it line by line with the first pass.

  agree     — both listens produced (nearly) the same words → probably right
  disputed  — they differ → show the second listen as a one-tap fix

Whisper's own confidence turned out not to predict the owner's corrections (confidently wrong is the
common case), so agreement between independent listens is the quality signal we trust.
"""
import difflib
import json
import re

import numpy as np
import soundfile as sf

from . import db
from .asr import SR, _clean, _post
from .audio import clip

HIDDEN = "(flags IS NULL OR flags='' OR flags='media')"
NORM = re.compile(r"[\W_]+", re.U)


def _n(t):
    return NORM.sub("", (t or "").lower())


def similarity(a, b):
    a, b = _n(a), _n(b)
    if not a and not b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


AGREE = 0.88


def _langs(t):
    from .textutil import lang_of
    L = lang_of(t)
    return {"zh", "en"} if L == "mixed" else {L}


def relisten(moment_id):
    m = db.one("SELECT m.*, r.path FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.id=?", moment_id)
    lines = db.rows(f"SELECT id, start, end, coalesce(text_final, text) t, text_final FROM segments WHERE rec_id=? "
                    f"AND idx BETWEEN ? AND ? AND {HIDDEN} ORDER BY idx", m["rec_id"], m["seg_from"], m["seg_to"])
    if not lines:  # every line was hidden as junk since the card was made: record it, or the worker retries it forever
        with db.tx() as c:
            c.execute("UPDATE moments SET relisten=? WHERE id=?", (json.dumps({"lines": {}, "agree": None, "extra": [], "inconclusive": True, "empty": True}), moment_id))
            c.execute("UPDATE moments SET status='empty' WHERE id=? AND status='pending'", (moment_id,))
        return None
    a0, b0 = max(0.0, lines[0]["start"] - 1.5), lines[-1]["end"] + 1.5
    wav = clip(m["path"], a0, b0, fmt="wav")
    audio, _ = sf.read(str(wav), dtype="float32")
    audio = np.concatenate([np.zeros(int(0.2 * SR), np.float32), audio, np.zeros(int(0.3 * SR), np.float32)])
    # no vocabulary prompt: the second opinion should be independent of the first (and the prompt
    # can push a long mixed clip into hallucinating "生日快乐!")
    res = _post(audio, "auto", "")
    heard = [{"start": round(a0 + max(0, float(s["start"]) - 0.2), 2), "end": round(a0 + max(0, float(s["end"]) - 0.2), 2),
              "text": _clean(s.get("text"))} for s in res.get("segments", []) if _clean(s.get("text"))]
    from .asr import HALLUCINATIONS
    heard = [h for h in heard if not HALLUCINATIONS.search(h["text"])]
    texts = [_n(h["text"]) for h in heard]
    unreliable = (not heard) or (len(heard) >= 3 and len(set(texts)) / len(texts) < 0.6)
    per_line = {}
    for l in lines:
        # whisper's timestamps drift by a line or so: look ±3 s and prefer the most similar wording
        near = [h for h in heard if h["end"] >= l["start"] - 3 and h["start"] <= l["end"] + 3]
        best = max(near, key=lambda h: similarity(l["t"], h["text"]), default=None)
        if best and similarity(l["t"], best["text"]) >= AGREE:
            alt, sim = best["text"], similarity(l["t"], best["text"])
        else:
            over = sorted(near, key=lambda h: -(min(l["end"], h["end"]) - max(l["start"], h["start"])))
            alt = over[0]["text"] if over and min(l["end"], over[0]["end"]) - max(l["start"], over[0]["start"]) > 0 else ""
            sim = similarity(l["t"], alt) if alt else None
        other = ""
        if alt and not (_langs(alt) & _langs(l["t"])):
            # other language: usually whisper translating, occasionally the real fix ("不, 你们是两个人" → "No, you are teapot").
            # Offer it quietly; it doesn't count as a dispute.
            other, alt, sim = alt, "", None
        per_line[str(l["id"])] = {"alt": alt, "other": other, "sim": None if sim is None else round(sim, 3),
                                  "verdict": "you" if l["text_final"] else
                                             ("agree" if sim is not None and sim >= AGREE else "disputed" if alt else "unheard")}
    # heard by the second listen but matching no first-pass line = likely a missed line
    def overlaps_other_language(h):  # an English "extra" sitting on top of a Mandarin line is its translation
        return any(min(l["end"], h["end"]) - max(l["start"], h["start"]) > 0.3 * (h["end"] - h["start"])
                   and not (_langs(l["t"]) & _langs(h["text"])) for l in lines)
    used = {v["alt"] for v in per_line.values()} | {v.get("other") for v in per_line.values()}
    extra = [h for h in heard if max((similarity(l["t"], h["text"]) for l in lines), default=0) < 0.5
             and len(_n(h["text"])) >= 3 and not overlaps_other_language(h)
             and not any(h["text"] in (u or "") for u in used)]
    verdicts = [v["verdict"] for v in per_line.values() if v["verdict"] in ("agree", "disputed")]
    agree = round(sum(v == "agree" for v in verdicts) / len(verdicts), 3) if verdicts else 1.0
    if unreliable:  # the second listen itself looped or came back empty: no opinion, no suggestions
        per_line = {k: {**v, "verdict": "you" if v["verdict"] == "you" else "unheard", "alt": ""} for k, v in per_line.items()}
        extra, agree = [], None
    data = {"heard": heard, "lines": per_line, "agree": agree, "extra": extra, "inconclusive": unreliable}
    with db.tx() as c:
        c.execute("UPDATE moments SET relisten=? WHERE id=?", (json.dumps(data, ensure_ascii=False), moment_id))
    return data


def queue_order_sql():
    from .rank import order_sql
    return ("SELECT m.id FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status IN ('pending','journal','later') "
            "AND m.relisten IS NULL ORDER BY CASE m.status WHEN 'journal' THEN 0 ELSE 1 END, "
            + (order_sql() if db.ranked() else  # same order you'll see them in Triage
               "coalesce(m.priority,1), CASE WHEN m.score>=8 THEN 0 ELSE 1 END, m.score DESC, r.recorded_at DESC")
            + " LIMIT ?")
