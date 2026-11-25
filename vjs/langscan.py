"""Wrong-language scan: whisper sometimes *translates* Chinese speech into fluent English. For English lines on
the cards you'll see next, decode the audio forced to Mandarin; whisper's language confidence comes back with
it. Confident-Chinese lines get the Chinese reading saved as a suggestion (segments.lang_hint) — never applied
automatically. ~1 s per line."""
import json
import time

from . import db
from .asr import decode_clip
from .textutil import CJK, lang_of, zh_punct

PROMPT = "以下是普通话的句子。"   # keeps whisper in Simplified characters


def scan(cards=600, p_min=0.6, verbose=True):
    from .rank import order_sql
    ms = db.rows(f"SELECT m.* FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status='pending' "
                 f"ORDER BY {order_sql() if db.ranked() else 'coalesce(m.priority,1), m.score DESC, r.recorded_at DESC'} LIMIT ?",
                 int(cards))
    n = found = 0
    t0 = time.time()
    for k, m in enumerate(ms, 1):
        rec = db.one("SELECT path FROM recordings WHERE id=?", m["rec_id"])
        segs = db.rows("SELECT id, start, end, text FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? AND text_final IS NULL "
                       "AND lang_hint IS NULL AND coalesce(flags,'') IN ('','media') AND length(text) >= 6",
                       m["rec_id"], m["seg_from"], m["seg_to"])
        for s in segs:
            if lang_of(s["text"]) != "en":
                continue
            try:
                zh, probs = decode_clip(rec["path"], s["start"], s["end"], "zh", prompt=PROMPT, with_probs=True)
            except Exception as e:
                print(f"    scan failed on line {s['id']}: {e}")
                continue
            p = round(float(probs.get("zh", 0)), 2)
            hint = {"p_zh": p}
            if p >= p_min and CJK.search(zh or ""):
                hint["zh"] = zh_punct(zh)
                found += 1
            with db.tx() as c:
                c.execute("UPDATE segments SET lang_hint=? WHERE id=?", (json.dumps(hint, ensure_ascii=False), s["id"]))
            n += 1
        if verbose and k % 20 == 0:
            print(f"    lang scan: {k}/{len(ms)} cards · {n} English lines checked · {found} look Chinese · {time.time() - t0:.0f}s", flush=True)
    if verbose:
        print(f"  lang scan done: {n} lines checked, {found} likely Chinese speech written as English")
    return found
