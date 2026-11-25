"""Recover speech the first pass lost.

gap-fill        The original VAD setting (threshold 0.3) sent only about half the audio to
                whisper and dropped many short kid utterances ("Dad, are we almost done?").
                For recordings transcribed that way, re-run a sensitive VAD, decode only the
                speech that no existing segment covers, and insert what comes back.
language rescue Short chunks sometimes get decoded in the wrong language ("Que, que, papá,
                ¿qué?"). Re-decode such lines forced to Mandarin and to English and keep the
                version whisper is more confident in.

Segment idx values are renumbered afterwards; moments are re-pointed via segment ids so
anything already triaged keeps exactly the same lines.
"""
import numpy as np
import soundfile as sf

from . import db
from .asr import SR, _clean, _flag, _post, pack_chunks, vad_spans
from .audio import drop_wav, wav16k
from .config import CFG
from .textutil import lang_of

OK = {"zh", "en", "chinese", "english"}


def _best_forced(audio_slice, prompt):
    """Decode forced zh and forced en; return (text, lang, logprob) of the more confident one."""
    a = np.concatenate([np.zeros(int(0.2 * SR), np.float32), audio_slice, np.zeros(int(0.3 * SR), np.float32)])
    best = None
    for lang in ("zh", "en"):
        res = _post(a, lang, prompt)
        segs = res.get("segments", [])
        text = _clean(" ".join(s.get("text", "") for s in segs))
        if not text:
            continue
        lp = float(np.mean([s.get("avg_logprob") or -2 for s in segs]))
        if best is None or lp > best[2]:
            best = (text, lang, lp)
    return best


def rescue_languages(rec_id, audio, prompt):
    """Re-decode lines whisper put in a third language (Spanish, Danish, Portuguese…)."""
    rows = db.rows("SELECT id, start, end, text, asr_lang FROM segments WHERE rec_id=? AND "
                   "(lower(coalesce(asr_lang,'')) NOT IN ('', 'zh', 'en', 'chinese', 'english') OR flags LIKE '%foreign%')", rec_id)
    n = 0
    for s in rows:
        a = audio[int(max(0, s["start"] - 0.2) * SR): int((s["end"] + 0.3) * SR)]
        if len(a) < SR * 0.25:
            continue
        best = _best_forced(a, prompt)
        if not best or best[2] < -1.2:
            continue  # both forced readings are junk → leave it flagged
        with db.tx() as c:
            flags = ",".join(f for f in (db.one("SELECT flags FROM segments WHERE id=?", s["id"])["flags"] or "").split(",")
                             if f and f != "foreign")
            c.execute("UPDATE segments SET text=?, lang=?, asr_lang=?, avg_logprob=?, flags=? WHERE id=?",
                      (best[0], lang_of(best[0]), best[1] + "*", best[2], flags, s["id"]))
        n += 1
    return n


def _uncovered(spans, segs, min_len=0.4):
    """Parts of VAD speech spans that no existing segment covers."""
    out = []
    covered = sorted((s["start"] - 0.15, s["end"] + 0.15) for s in segs)
    for a, b in spans:
        cur = a
        for ca, cb in covered:
            if cb <= cur or ca >= b:
                continue
            if ca > cur:
                out.append((cur, ca))
            cur = max(cur, cb)
        if cur < b:
            out.append((cur, b))
    return [(a, b) for a, b in out if b - a >= min_len]


def _renumber(rec_id):
    """Re-sort idx by start time and re-point moments at the same segments."""
    with db.tx() as c:
        bounds = {m["id"]: (db.one("SELECT id FROM segments WHERE rec_id=? AND idx=?", rec_id, m["seg_from"]),
                            db.one("SELECT id FROM segments WHERE rec_id=? AND idx=?", rec_id, m["seg_to"]))
                  for m in db.rows("SELECT id, seg_from, seg_to FROM moments WHERE rec_id=?", rec_id)}
        c.execute("UPDATE segments SET idx = -idx - 1 WHERE rec_id=?", (rec_id,))
        order = db.rows("SELECT id FROM segments WHERE rec_id=? ORDER BY start, id", rec_id)
        c.executemany("UPDATE segments SET idx=? WHERE id=?", [(i, s["id"]) for i, s in enumerate(order)])
        for mid, (a, b) in bounds.items():
            if a and b:
                na = db.one("SELECT idx FROM segments WHERE id=?", a["id"])["idx"]
                nb = db.one("SELECT idx FROM segments WHERE id=?", b["id"])["idx"]
                c.execute("UPDATE moments SET seg_from=?, seg_to=? WHERE id=?", (na, nb, mid))


def loud_spans(audio, min_len=0.35):
    """Stretches clearly above the room's noise floor. Silero VAD misses a lot of her speech in long
    recordings, so gap-finding uses plain loudness; whisper's own no-speech handling and the junk
    filters deal with non-speech sounds."""
    import librosa
    hop = 512
    rms = librosa.feature.rms(y=audio, frame_length=1024, hop_length=hop)[0]
    floor = np.percentile(rms, 20)
    on = rms > max(floor * 3.0, 0.004)
    spans, start = [], None
    for i, v in enumerate(on):
        t = i * hop / SR
        if v and start is None:
            start = t
        elif not v and start is not None:
            spans.append((start, t)); start = None
    if start is not None:
        spans.append((start, len(audio) / SR))
    merged = []
    for a, b in spans:  # bridge short dips inside a sentence
        if merged and a - merged[-1][1] < 0.35:
            merged[-1] = (merged[-1][0], b)
        else:
            merged.append((a, b))
    return [(a, b) for a, b in merged if b - a >= min_len]


def gapfill(rec, verbose=True):
    from .asr import asr_prompt; prompt = asr_prompt()
    wav = wav16k(rec["path"], rec["id"])
    audio, _ = sf.read(str(wav), dtype="float32")
    segs = db.rows("SELECT start, end FROM segments WHERE rec_id=?", rec["id"])
    gaps = _uncovered(loud_spans(audio), segs)
    new = []
    for cs, ce in pack_chunks(gaps, max_len=18.0, max_gap=1.5):
        cs0, ce0 = max(0, cs - 2.0), min(len(audio) / SR, ce + 2.0)  # context helps whisper with short kid lines
        a = np.concatenate([np.zeros(int(0.2 * SR), np.float32), audio[int(cs0 * SR): int(ce0 * SR)],
                            np.zeros(int(0.3 * SR), np.float32)])
        try:
            res = _post(a, "auto", prompt)
        except Exception as e:
            print(f"    gap chunk failed: {e}")
            continue
        lang = (res.get("language") or "")[:12]
        for s in res.get("segments", []):
            text = _clean(s.get("text"))
            if not text:
                continue
            st, en = cs0 + max(0.0, float(s["start"]) - 0.2), cs0 + max(0.0, float(s["end"]) - 0.2)
            mid = (st + en) / 2
            if not any(a - 0.3 <= mid <= b + 0.3 for a, b in gaps):
                continue  # context audio, not a gap
            if any(x["start"] - 0.1 <= mid <= x["end"] + 0.1 for x in segs + new):
                continue  # already transcribed
            new.append({"start": round(st, 2), "end": round(en, 2), "text": text, "avg_logprob": s.get("avg_logprob"),
                        "no_speech": s.get("no_speech_prob"), "asr_lang": lang + ("" if prompt else "+rd")})
    new = _flag(new)
    base = (db.one("SELECT max(idx) m FROM segments WHERE rec_id=?", rec["id"])["m"] or 0) + 1000
    with db.tx() as c:
        c.executemany(
            "INSERT INTO segments(rec_id, idx, start, end, text, lang, avg_logprob, no_speech, flags, asr_lang, src) VALUES(?,?,?,?,?,?,?,?,?,?,'gap')",
            [(rec["id"], base + i, s["start"], s["end"], s["text"], lang_of(s["text"]), s["avg_logprob"], s["no_speech"],
              s["flags"], s["asr_lang"]) for i, s in enumerate(new)])
    _renumber(rec["id"])
    mark_dups(rec["id"])
    rescued = rescue_languages(rec["id"], audio, prompt)
    fix_translation_pairs(rec["id"])
    from .speaker import analyze
    analyze(rec["id"], wav)
    # moments you already kept: drop their Polish drafts; Polish rebuilds them (with the recovered lines) when opened
    with db.tx() as c:
        c.execute("DELETE FROM moment_lines WHERE moment_id IN (SELECT id FROM moments WHERE rec_id=? AND status IN ('journal','later','log'))", (rec["id"],))
    from . import media
    media.flag_text(rec["id"])
    with db.tx() as c:
        c.execute("UPDATE recordings SET vad_v=2 WHERE id=?", (rec["id"],))
    drop_wav(rec["id"])
    if verbose:
        print(f"    +{len(new)} recovered line(s) from {sum(b - a for a, b in gaps):.0f}s of missed speech; {rescued} line(s) language-rescued")
    return len(new)


def gapfill_all(remine=True):
    from .pipeline import _status
    todo = db.rows("SELECT * FROM recordings WHERE status='mined' AND coalesce(vad_v,1) < 2 "
                   "ORDER BY CASE WHEN prior='family' THEN 0 ELSE 1 END, recorded_at DESC")
    for i, rec in enumerate(todo, 1):
        print(f"[gap-fill {i}/{len(todo)}] {rec['filename']}")
        _status(state="running", current=rec["filename"], done=i - 1, total=len(todo), detail="recovering missed speech")
        try:
            gapfill(rec)
            if remine:
                from .mine import mine_recording
                with db.tx() as c:
                    c.execute("DELETE FROM moment_lines WHERE moment_id IN (SELECT id FROM moments WHERE rec_id=? AND status='pending')", (rec["id"],))
                    c.execute("DELETE FROM moments WHERE rec_id=? AND status='pending'", (rec["id"],))
                mine_recording(rec["id"])
        except Exception as e:
            import traceback
            traceback.print_exc()
            drop_wav(rec["id"])
    from . import media
    media.flag_repeats()
    _status(state="idle", detail="gap-fill done", current="")


# ---------------------------------------------------------------- cleanup (2026-09-24, from the owner's review notes)
import difflib
import re

_NORM = re.compile(r"[\W_]+", re.U)
_HZ = re.compile(r"[\u4e00-\u9fff]")


def _n(t):
    return _NORM.sub("", (t or "").lower())


def mark_gap_origin():
    """Retro: segments added by the first gap-fill run are the id batch after the original transcription."""
    n = 0
    with db.tx() as c:
        for r in db.rows("SELECT id FROM recordings WHERE vad_v=2"):
            ids = [x["id"] for x in db.rows("SELECT id FROM segments WHERE rec_id=? ORDER BY id", r["id"])]
            cut = next((ids[i] for i in range(1, len(ids)) if ids[i] - ids[i - 1] > 1), None)
            if cut:
                n += c.execute("UPDATE segments SET src='gap' WHERE rec_id=? AND id>=? AND src IS NULL", (r["id"], cut)).rowcount
    return n


def mark_dups(rec_id, window=None):
    """A gap-fill line is a duplicate only if it covers the SAME stretch of audio as a similar line
    ("horse stay" / "horse day"). Kids really do repeat themselves a few seconds apart, and the
    2026-09-24 quality check showed a ±6 s rule hid real speech ("Hello.", "Okay.", "I should just eat
    some carrots, right?")."""
    segs = db.rows("SELECT id, start, end, text, flags, src FROM segments WHERE rec_id=? ORDER BY start", rec_id)
    n = 0
    with db.tx() as c:
        for g in segs:
            if g["src"] != "gap" or "dup" in (g["flags"] or ""):
                continue
            a = _n(g["text"])
            if len(a) < 2:
                continue
            # context echo: gap-fill decodes with 2 s of neighbouring audio, and whisper sometimes repeats the neighbour's
            # words in the silence just after it, stretched over several seconds ("Look at me!" over 4 s that held nothing).
            # Words already said by a line within 1.5 s, spread thinner than 1 word/s → not new speech.
            _tk = lambda t: re.findall(r"[a-z']+|[\u3400-\u9fff]", (t or "").lower())
            gw, gd = _tk(g["text"]), max(0.1, g["end"] - g["start"])
            if len(gw) >= 2 and len(gw) / gd < 1.0:
                ech = next((s for s in segs if s["id"] != g["id"] and s["src"] != "gap"
                            and (abs(s["end"] - g["start"]) < 1.5 or abs(g["end"] - s["start"]) < 1.5)
                            and set(gw) <= set(_tk(s["text"]))), None)
                if ech:
                    from .media import add_flag
                    add_flag(c, g["id"], g["flags"], "dup"); n += 1
                    continue
            for s in segs:
                if s["id"] == g["id"]:
                    continue
                overlap = min(g["end"], s["end"]) - max(g["start"], s["start"])
                if overlap < 0.4 * min(g["end"] - g["start"], s["end"] - s["start"]):
                    continue  # different moment in time → a real repeat, not a duplicate
                if s["src"] == "gap" and s["id"] > g["id"]:
                    continue  # among gap lines, the later one is the duplicate
                b = _n(s["text"])
                short, long_ = sorted((a, b), key=len)
                if (len(short) >= 4 and short in long_) or difflib.SequenceMatcher(None, a, b).ratio() >= 0.6:
                    from .media import add_flag
                    fuller_gap = len(a) > len(b) + 3 and b in a and s["src"] != "gap"
                    if fuller_gap and not db.one("SELECT 1 FROM segments WHERE id=? AND (text_final IS NOT NULL OR spk IS NOT NULL)", s["id"]):
                        add_flag(c, s["id"], s["flags"], "dup")  # the recovered line is the complete version
                    else:
                        add_flag(c, g["id"], g["flags"], "dup")
                    n += 1
                    break
    return n


def _split(t):
    return [p.strip() for p in re.split(r"(?<=[.!?。！？,，])\s*", t) if p.strip()]


def _pair_parts(t):
    ps = _split(t)
    en = [p for p in ps if not _HZ.search(p) and len(re.findall(r"[A-Za-z]+", p)) >= 2]
    zh = [p for p in ps if _HZ.search(p) and not re.search(r"[A-Za-z]{3,}", p) and len(_HZ.findall(p)) >= 3]
    return en, zh


def fix_translation_pairs(rec_id=None, min_p=0.6, max_other=0.1):
    """Whisper sometimes adds its own translation ("Oh, I know this one, Elsa. 哦,我知道这个, 这是Elsa.").
    Ask whisper which language the audio is; if it's clearly one, keep only that half. Real code-switching
    (both languages plausible) is left alone. Never touches a line you corrected."""
    from .asr import decode_clip
    q = ("SELECT s.id, s.start, s.end, s.text, s.asr_lang, r.path FROM segments s JOIN recordings r ON r.id=s.rec_id "
         "WHERE s.lang='mixed' AND s.text_final IS NULL AND coalesce(s.flags,'') IN ('','media') "
         "AND coalesce(s.asr_lang,'') NOT LIKE '%+tr'" + (" AND s.rec_id=?" if rec_id else ""))
    n = 0
    for s in db.rows(q, *([rec_id] if rec_id else [])):
        en, zh = _pair_parts(s["text"])
        if not (en and (zh or any(_HZ.search(p) for p in _split(s["text"])))):
            continue
        try:
            _, probs = decode_clip(s["path"], s["start"], s["end"], "auto", "", with_probs=True)
        except Exception:
            continue
        pe, pz = float(probs.get("en", 0)), float(probs.get("zh", 0))
        ps = _split(s["text"])
        if pe >= min_p and pz <= max_other:    # English audio: drop the Chinese sentences whisper added
            keep = " ".join(p for p in ps if not _HZ.search(p))
        elif pz >= min_p and pe <= max_other:  # Mandarin audio: drop the English sentences
            keep = " ".join(p for p in ps if p not in en)
        else:
            keep = None                        # genuinely mixed — leave it
        keep = re.sub(r"[,，]\s*$", ".", keep).strip() if keep else None
        with db.tx() as c:
            if keep:
                c.execute("UPDATE segments SET text=?, lang=?, asr_lang=coalesce(asr_lang,'')||'+tr' WHERE id=?",
                          (keep, lang_of(keep), s["id"]))
                n += 1
            else:
                c.execute("UPDATE segments SET asr_lang=coalesce(asr_lang,'')||'+tr' WHERE id=?", (s["id"],))
    return n


def recompute_dups():
    with db.tx() as c:
        c.execute("UPDATE segments SET flags=trim(replace(replace(','||flags||',', ',dup,', ','), ',,', ','), ',') WHERE flags LIKE '%dup%'")
    return sum(mark_dups(r["id"]) for r in db.rows("SELECT id FROM recordings WHERE vad_v=2"))


def cleanup_all():
    print(f"  gap-fill lines identified: {mark_gap_origin()}")
    d = sum(mark_dups(r["id"]) for r in db.rows("SELECT id FROM recordings WHERE vad_v=2"))
    print(f"  duplicate lines hidden: {d}")
    print(f"  translation pairs reduced to the spoken language: {fix_translation_pairs()}")


# ---------------------------------------------------------------- re-decode old recovered lines (2026-09-24)
def redecode_gap_lines(rec_id, verbose=True):
    """Recovered (gap-fill) lines from before the 2026-09-24 settings change were decoded with the vocabulary
    prompt and greedy search; the blind quality check found them usable only 30% of the time vs 60% for a
    ±2 s, prompt-free, beam-search re-decode. Re-decode them that way. Never touches a line you corrected."""
    from .relisten import similarity
    rec = db.one("SELECT * FROM recordings WHERE id=?", rec_id)
    rows = db.rows("SELECT id, start, end, text FROM segments WHERE rec_id=? AND src='gap' AND text_final IS NULL "
                   "AND coalesce(asr_lang,'') NOT LIKE '%+rd%' AND (flags IS NULL OR flags='' OR flags='media')", rec_id)
    if not rows:
        return 0
    wav = wav16k(rec["path"], rec_id)
    audio, _ = sf.read(str(wav), dtype="float32")
    changed = 0
    for s in rows:
        a0, b0 = max(0.0, s["start"] - 2.0), min(len(audio) / SR, s["end"] + 2.0)
        a = np.concatenate([np.zeros(int(0.2 * SR), np.float32), audio[int(a0 * SR): int(b0 * SR)], np.zeros(int(0.3 * SR), np.float32)])
        try:
            res = _post(a, "auto", "")
        except Exception:
            continue
        from .asr import HALLUCINATIONS
        segs = [{"start": a0 + max(0, float(x["start"]) - 0.2), "end": a0 + max(0, float(x["end"]) - 0.2), "text": _clean(x.get("text"))}
                for x in res.get("segments", [])]
        over = [x for x in segs if x["text"] and not HALLUCINATIONS.search(x["text"])
                and min(s["end"], x["end"]) - max(s["start"], x["start"]) > 0.15]
        new = ""
        if over:
            best = max(over, key=lambda x: similarity(s["text"], x["text"]))
            new = best["text"] if similarity(s["text"], best["text"]) >= 0.5 else " ".join(x["text"] for x in over)
        with db.tx() as c:
            if new and new != s["text"]:
                c.execute("UPDATE segments SET text=?, lang=?, asr_lang=coalesce(asr_lang,'')||'+rd' WHERE id=?",
                          (new, lang_of(new), s["id"]))
                changed += 1
            else:
                c.execute("UPDATE segments SET asr_lang=coalesce(asr_lang,'')||'+rd' WHERE id=?", (s["id"],))
    drop_wav(rec_id)
    mark_dups(rec_id)
    fix_translation_pairs(rec_id)
    from .quality import score_all
    score_all(rec_id)
    # drafts of moments you kept: rebuild so they show the better text (your own edits are preserved)
    from .polish import build_lines
    for m in db.rows("SELECT id FROM moments WHERE rec_id=? AND status IN ('journal','later')", rec_id):
        if db.one("SELECT 1 FROM moment_lines WHERE moment_id=?", m["id"]):
            build_lines(m["id"], force=True)
    with db.tx() as c:  # second listens were computed against the old text
        c.execute("UPDATE moments SET relisten=NULL WHERE rec_id=? AND status IN ('pending','journal','later')", (rec_id,))
    if verbose:
        print(f"    re-decoded {len(rows)} recovered line(s), {changed} changed")
    return changed


def redecode_all():
    todo = db.rows("SELECT id, filename FROM recordings WHERE status='mined' AND vad_v=2 AND EXISTS (SELECT 1 FROM segments s "
                   "WHERE s.rec_id=recordings.id AND s.src='gap' AND coalesce(s.asr_lang,'') NOT LIKE '%+rd%') "
                   "ORDER BY CASE WHEN prior='family' THEN 0 ELSE 1 END, recorded_at DESC")
    from .pipeline import _status
    for i, r in enumerate(todo, 1):
        print(f"[re-decode {i}/{len(todo)}] {r['filename']}", flush=True)
        _status(state="running", current=r["filename"], done=i - 1, total=len(todo), detail="re-decoding recovered lines")
        try:
            redecode_gap_lines(r["id"])
        except Exception:
            import traceback
            traceback.print_exc()
            drop_wav(r["id"])
    _status(state="idle", detail="re-decode done", current="")
