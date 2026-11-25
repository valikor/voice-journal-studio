"""Recordings that aren't about her (2026-10-14): see what's in them, keep what matters, set aside the rest — never delete.

Scope (read-only rules, same ones the pipeline enforces): recorded before 2023 · 2024 recordings outside the allow-list (`rules.no_kid`)
· category work / media / quiet · mined recordings with no line of hers. Recordings that already produced a kept/published moment are
never in scope.

`./vjs.sh sort --summarize` (LLM; run it behind other model jobs) writes one row per recording to `recording_summary`: kind
(family archive / work meeting / personal note / TV-media / noise-empty), a 3-line summary, who is talking, topics, and "worth keeping?"
with a reason. Recordings with a transcript use it; never-transcribed ones are sampled — three ~40 s stretches of the densest speech
are transcribed (whisper) and summarised. Everything stays in studio.db; work content is never written anywhere else.

Choices (Recordings → Sort the rest): keep as family archive / work / personal (queues a transcription-only pass so it is readable in the
Reader and searchable; never mined), or "delete candidate": the file is MOVED to `~/Documents/Recordings/_to_delete/`
(same-volume rename, status `delete_candidate`, logged). `./vjs.sh sort --restore [<id>|--all]` moves it back. the owner empties the folder."""
import json
import os
import re
import subprocess
from pathlib import Path

import numpy as np

from . import db, edits
from .config import CFG

KINDS = ["family archive", "work meeting", "personal note", "TV-media", "noise-empty"]
TRASH = Path(os.environ.get("VJS_TRASH") or "~/Documents/Recordings/_to_delete").expanduser()   # VJS_TRASH: tests only
SR8 = 8000


def scope_ids():
    from .rules import no_kid
    recs = db.rows("SELECT id, recorded_at, title, status, category, kid_ok FROM recordings")
    keepers = {r["rec_id"] for r in db.rows("SELECT DISTINCT rec_id FROM moments WHERE status IN ('journal','ready','published')")}
    her = {r["rec_id"] for r in db.rows("SELECT DISTINCT rec_id FROM segments WHERE coalesce(spk,spk_guess)='kid' AND coalesce(flags,'') IN ('','media')")}
    out = []
    for r in recs:
        if r["id"] in keepers or r["status"] in ("delete_candidate",):
            continue
        has_tx = r["status"] in ("mined", "analyzed", "transcribed")
        if no_kid(r) or r["category"] in ("work", "media", "quiet") or (has_tx and r["id"] not in her):
            out.append(r["id"])
    return out


# ---------------------------------------------------------------- finding the densest speech
def _density_from_audio(path, dur):
    """Seconds-resolution speech density straight from the audio (cheap, no model): fraction of frames well above the noise floor."""
    r = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-i", str(path), "-ac", "1", "-ar", str(SR8), "-f", "s16le", "-"],
                       capture_output=True, timeout=1800)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32)
    n = len(x) // SR8
    if n < 5:
        return np.zeros(0)
    db_ = 20 * np.log10(np.sqrt((x[:n * SR8].reshape(n, SR8) ** 2).mean(1)) + 1.0)
    floor = np.percentile(db_, 15)
    return (db_ > floor + 8).astype(float)   # 1 second "voiced" if clearly above the floor


def _density_from_segments(rec_id, dur):
    n = int(dur or 0)
    d = np.zeros(max(n, 1))
    for s in db.rows("SELECT start, end FROM segments WHERE rec_id=? AND coalesce(flags,'') IN ('','media')", rec_id):
        a, b = int(s["start"]), min(int(s["end"]) + 1, len(d))
        d[a:b] = 1
    return d


def pick_windows(density, k=3, win=40):
    """k non-overlapping windows of `win` seconds with the most speech, in time order."""
    if len(density) <= win:
        return [0]
    cs = np.concatenate([[0], np.cumsum(density)])
    score = cs[win:] - cs[:-win]
    taken, out = np.zeros(len(density), bool), []
    for _ in range(k):
        s2 = np.where(taken[: len(score)], -1, score)
        for t in np.flatnonzero(taken):
            s2[max(0, t - win): t + 1] = -1
        i = int(np.argmax(s2))
        if s2[i] <= 2:
            break
        out.append(i); taken[i:i + win] = True
    return sorted(out) or [0]


def _evidence(rec):
    """(text for the model, sample_start, source)"""
    rid = rec["id"]
    has = db.one("SELECT count(*) n FROM segments WHERE rec_id=?", rid)["n"]
    if has:
        dens = _density_from_segments(rid, rec["duration"])
        starts = pick_windows(dens)
        segs = db.rows("SELECT start, end, coalesce(spk,spk_guess,'other') sp, coalesce(text_final,text) t FROM segments WHERE rec_id=? "
                       "AND coalesce(flags,'') IN ('','media') ORDER BY start", rid)
        parts = []
        for a in starts:
            parts.append(" ".join(f"{x['t']}" for x in segs if a <= x["start"] < a + 60)[:1200])
        head = " ".join(x["t"] for x in segs[:12])[:600]
        spk = {}
        for x in segs:
            spk[x["sp"]] = spk.get(x["sp"], 0) + 1
        text = f"[opening] {head}\n" + "\n".join(f"[~{a // 60}:{a % 60:02d}] {t}" for a, t in zip(starts, parts))
        return text[:4200], float(starts[0]), "transcript", spk
    # never transcribed: sample the audio
    from . import asr
    from .audio import clip
    dens = _density_from_audio(rec["path"], rec["duration"])
    starts = pick_windows(dens)
    chunks = []
    for a in starts:
        wav = clip(rec["path"], a, a + 40, fmt="wav")
        segs = asr.transcribe_wav(wav, asr.asr_prompt())
        chunks.append(f"[~{a // 60}:{a % 60:02d}] " + " ".join(s["text"] for s in segs if "halluc" not in (s.get("flags") or ""))[:1200])
    return "\n".join(chunks)[:4200], float(starts[0]), "sample", {}


SYSTEM = """You catalogue one person's archive of personal voice recordings. You get the date, length, any title, and machine-made
transcript excerpts from a few moments of ONE recording (the transcription can be wrong, and may mix English and Mandarin).
Return JSON: kind — one of: family archive (family life, relatives, events, children, home), work meeting (professional or business
conversation, calls, presentations), personal note (one person dictating thoughts, reminders, ideas), TV-media (TV, video, audiobook,
music, radio playing in the background), noise-empty (silence, noise, unintelligible).
summary — at most 3 short plain lines saying what the recording is. speakers — who seems to be talking (e.g. "father and grandmother").
topics — 3 to 6 short phrases. keep — "yes" (meaningful family memories, or useful personal/professional content), "maybe", or "no" (noise,
TV, trivial). reason — one sentence. Describe only what the excerpts show; if unsure say so. Be brief and neutral."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["kind", "summary", "speakers", "topics", "keep", "reason"], "properties": {
    "kind": {"type": "string", "enum": KINDS}, "summary": {"type": "string"}, "speakers": {"type": "string"},
    "topics": {"type": "array", "items": {"type": "string"}}, "keep": {"type": "string", "enum": ["yes", "maybe", "no"]}, "reason": {"type": "string"}}}


def summarize(limit=None, redo=False, verbose=True):
    from .llm import chat_json, ensure_ready
    ensure_ready()
    have = {r["rec_id"] for r in db.rows("SELECT rec_id FROM recording_summary")}
    ids = [i for i in scope_ids() if redo or i not in have]
    recs = [db.one("SELECT * FROM recordings WHERE id=?", i) for i in ids]
    recs.sort(key=lambda r: r["recorded_at"] or "", reverse=True)
    if limit:
        recs = recs[: int(limit)]
    if verbose:
        print(f"  {len(recs)} recording(s) to summarise ({len(ids)} in scope, {len(have)} already done)")
    done = 0
    for r in recs:
        try:
            if not Path(r["path"]).exists():
                print(f"    skip {r['filename']}: file missing"); continue
            text, start, source, spk = _evidence(r)
            if not text.strip():
                out = {"kind": "noise-empty", "summary": "No speech found.", "speakers": "", "topics": [], "keep": "no", "reason": "nothing intelligible"}
            else:
                user = (f"Recorded {r['recorded_at']}, {int((r['duration'] or 0) // 60)} min, title: {r['title'] or '(none)'}, "
                        f"category hint: {r['category'] or '(none)'}.\nSpeaker lines (voice guess): {spk or 'unknown'}\n\n{text}")
                out = chat_json(SYSTEM, user, SCHEMA, max_tokens=500)
            with db.tx() as c:
                c.execute("INSERT OR REPLACE INTO recording_summary(rec_id, kind, summary, topics, speakers, keep, keep_reason, source, sample_start) "
                          "VALUES(?,?,?,?,?,?,?,?,?)", (r["id"], out["kind"], out["summary"][:600], json.dumps(out["topics"], ensure_ascii=False),
                                                       out["speakers"][:200], out["keep"], out["reason"][:300], source, start))
            done += 1
            if verbose and done % 5 == 0:
                print(f"    {done}/{len(recs)}", flush=True)
        except Exception as e:
            print(f"    {r['filename']}: {e}")
    return {"summarised": done}


# ---------------------------------------------------------------- choices
def listing(kind=None, unsorted=False):
    ids = scope_ids()
    rows = db.rows("SELECT r.id, r.filename, r.recorded_at, r.duration, r.title, r.category, r.status, r.archive, s.kind, s.summary, s.topics, s.speakers, s.keep, "
                   "s.keep_reason, s.sample_start, s.user_kind FROM recordings r LEFT JOIN recording_summary s ON s.rec_id=r.id "
                   "WHERE r.id IN (%s) ORDER BY r.recorded_at DESC" % ",".join("'%s'" % i for i in ids)) if ids else []
    dc = db.rows("SELECT r.id, r.filename, r.recorded_at, r.duration, r.title, r.category, r.status, r.archive, s.kind, s.summary, s.topics, s.speakers, s.keep, "
                 "s.keep_reason, s.sample_start, s.user_kind FROM recordings r LEFT JOIN recording_summary s ON s.rec_id=r.id WHERE r.status='delete_candidate'")
    rows += dc
    from .labels import get as _lab, place_of as _place
    for r in rows:
        r["label"] = _lab(r["id"]); r["place"] = _place(r["title"])
        r["topics"] = json.loads(r["topics"]) if r["topics"] else []
        r["sample_start"] = r["sample_start"] if r["sample_start"] is not None else 0
    if kind:
        rows = [r for r in rows if (r["user_kind"] or r["kind"]) == kind]
    if unsorted:
        rows = [r for r in rows if not r["user_kind"] and r["status"] != "delete_candidate"]
    return rows


def choose(ids, choice):
    """choice: family archive | work meeting | personal note | delete candidate"""
    res = {"changed": 0, "moved": 0, "errors": []}
    for rid in ids:
        r = db.one("SELECT * FROM recordings WHERE id=?", rid)
        if not r:
            continue
        if choice == "delete candidate":
            try:
                _move_to_trash(r); res["moved"] += 1
            except Exception as e:
                res["errors"].append(f"{r['filename']}: {e}")
            continue
        with db.tx() as c:
            c.execute("INSERT OR IGNORE INTO recording_summary(rec_id, source) VALUES(?, 'manual')", (rid,))
            c.execute("UPDATE recording_summary SET user_kind=?, user_at=datetime('now','localtime') WHERE rec_id=?", (choice, rid))
            edits.log(c, "sort-keep", r["status"], choice, None, None, "sort")
            c.execute("UPDATE recordings SET archive=1 WHERE id=?", (rid,))
            if choice == "work meeting":
                c.execute("UPDATE recordings SET category='work' WHERE id=?", (rid,))
                c.execute("UPDATE moments SET status='no_kid' WHERE rec_id=? AND status='pending'", (rid,))   # never in front of the journal queue
            if r["status"] == "skipped":   # never transcribed: queue a transcription-only pass (the next ./vjs.sh run)
                c.execute("UPDATE recordings SET status='new', error=NULL WHERE id=?", (rid,))
        res["changed"] += 1
    return res


def _move_to_trash(r):
    src = Path(r["path"])
    if not src.exists():
        raise FileNotFoundError("file not found")
    TRASH.mkdir(parents=True, exist_ok=True)
    dst = TRASH / src.name
    if dst.exists():
        raise FileExistsError(f"{dst.name} already in _to_delete")
    os.rename(src, dst)   # same-volume rename only — never copy-then-delete
    with db.tx() as c:
        c.execute("INSERT INTO recording_moves(rec_id, from_path, to_path, prev_status) VALUES(?,?,?,?)", (r["id"], str(src), str(dst), r["status"]))
        c.execute("UPDATE recordings SET path=?, status='delete_candidate' WHERE id=?", (str(dst), r["id"]))
        edits.log(c, "sort-delete", str(src), str(dst), None, None, "sort")


def restore(rec_id=None):
    rows = db.rows("SELECT * FROM recording_moves WHERE restored=0" + ("" if rec_id is None else " AND rec_id=?"), *([] if rec_id is None else [rec_id]))
    n, err = 0, []
    for m in rows:
        try:
            src, dst = Path(m["to_path"]), Path(m["from_path"])
            if dst.exists():
                raise FileExistsError(f"{dst} exists")
            dst.parent.mkdir(parents=True, exist_ok=True)
            os.rename(src, dst)
            with db.tx() as c:
                c.execute("UPDATE recordings SET path=?, status=? WHERE id=?", (str(dst), m["prev_status"], m["rec_id"]))
                c.execute("UPDATE recording_moves SET restored=1 WHERE id=?", (m["id"],))
                edits.log(c, "sort-restore", str(src), str(dst), None, None, "sort")
            n += 1
        except Exception as e:
            err.append(f"{m['rec_id']}: {e}")
    return {"restored": n, "errors": err}


def status():
    summ = db.rows("SELECT coalesce(user_kind, kind) k, count(*) n FROM recording_summary GROUP BY 1")
    return {"in_scope": len(scope_ids()), "summarised": db.one("SELECT count(*) n FROM recording_summary WHERE kind IS NOT NULL")["n"],
            "by_kind": {r["k"]: r["n"] for r in summ}, "delete_candidates": db.one("SELECT count(*) n FROM recordings WHERE status='delete_candidate'")["n"]}
