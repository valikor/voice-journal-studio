"""Inbox preview (2026-10-17): look at what's waiting to be processed and skip what isn't worth GPU time — before the pipeline gets to it.
Per queued recording: date, length, size, how much of it is speech (no model: loudness above the noise floor), the densest 30 s to
sample, and (optional, on demand) a short transcript sample summarised by the local model. Skip = status 'skipped' (undo: back to 'new');
Delete candidate = moved to _to_delete/ like in Sort the rest. The pipeline re-checks each recording's status just before it starts it."""
import os
from pathlib import Path

import numpy as np

from . import db, edits, sortrec


def _ensure():
    with db.tx() as c:
        c.execute("CREATE TABLE IF NOT EXISTS inbox_preview(rec_id TEXT PRIMARY KEY, speech_pct REAL, sample_start REAL, db_mean REAL, at TEXT DEFAULT (datetime('now','localtime')))")


def queue():
    _ensure()
    rows = db.rows("SELECT r.id, r.filename, r.recorded_at, r.duration, r.path, r.status, r.error, r.title, p.speech_pct, p.sample_start, s.kind, s.summary "
                   "FROM recordings r LEFT JOIN inbox_preview p ON p.rec_id=r.id LEFT JOIN recording_summary s ON s.rec_id=r.id "
                   "WHERE r.status IN ('new','analyzed','transcribed') OR (r.status='skipped' AND r.error LIKE 'skipped in the Inbox%') "
                   "OR (r.status='delete_candidate' AND r.path LIKE '%_to_delete%') ORDER BY r.recorded_at DESC")
    out = []
    for r in rows:
        if r["status"] in ("new", "analyzed", "transcribed") and not Path(r["path"]).exists():
            continue
        if r["status"] == "delete_candidate" and (r["recorded_at"] or "") < "2026-10-14":
            continue   # only recent set-asides are shown here
        try:
            r["mb"] = round(os.path.getsize(r["path"]) / 1e6, 1)
        except OSError:
            r["mb"] = None
        out.append(r)
    return out


def measure(rec_id):
    """Speech share + densest 30 s, from the audio alone (cached)."""
    _ensure()
    r = db.one("SELECT path, duration FROM recordings WHERE id=?", rec_id)
    dens = sortrec._density_from_audio(r["path"], r["duration"])
    if not len(dens):
        pct, start = 0.0, 0.0
    else:
        pct = float(dens.mean()) * 100
        start = float(sortrec.pick_windows(dens, k=1, win=30)[0])
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO inbox_preview(rec_id, speech_pct, sample_start) VALUES(?,?,?)", (rec_id, pct, start))
    return {"speech_pct": pct, "sample_start": start}


def measure_all():
    n = 0
    for r in queue():
        if r["speech_pct"] is None and r["status"] == "new":
            try:
                measure(r["id"]); n += 1
            except Exception as e:
                print(f"measure failed {r['filename']}: {e}")
    return n


def set_aside(ids, mode):
    """mode: skip | delete | restore"""
    res = {"changed": 0, "errors": []}
    for rid in ids:
        r = db.one("SELECT * FROM recordings WHERE id=?", rid)
        if not r:
            continue
        try:
            if mode == "skip":
                with db.tx() as c:
                    c.execute("UPDATE recordings SET status='skipped', error='skipped in the Inbox preview' WHERE id=? AND status IN ('new','analyzed','transcribed')", (rid,))
                    edits.log(c, "inbox-skip", r["status"], "skipped", None, None, "inbox")
            elif mode == "delete":
                sortrec._move_to_trash(r)
            elif mode == "restore":
                if r["status"] == "delete_candidate":
                    sortrec.restore(rid)
                with db.tx() as c:
                    c.execute("UPDATE recordings SET status='new', error=NULL WHERE id=? AND (status='skipped' OR status='delete_candidate' OR status='new')", (rid,))
                    edits.log(c, "inbox-restore", r["status"], "new", None, None, "inbox")
            res["changed"] += 1
        except Exception as e:
            res["errors"].append(f"{r['filename']}: {e}")
    return res


def describe(rec_id):
    """On demand: transcribe ~3 samples of the densest speech and summarise (same as Sort-the-rest summaries). Shares the model servers
    with whatever else is running, so it can take a minute."""
    from .llm import chat_json, ensure_ready
    ensure_ready()
    r = db.one("SELECT * FROM recordings WHERE id=?", rec_id)
    text, start, source, spk = sortrec._evidence(r)
    user = (f"Recorded {r['recorded_at']}, {int((r['duration'] or 0) // 60)} min, title: {r['title'] or '(none)'}.\n\n{text}") if text.strip() else ""
    if not user:
        out = {"kind": "noise-empty", "summary": "No speech found.", "speakers": "", "topics": [], "keep": "no", "reason": "nothing intelligible"}
    else:
        out = chat_json(sortrec.SYSTEM, user, sortrec.SCHEMA, max_tokens=500)
    import json
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO recording_summary(rec_id, kind, summary, topics, speakers, keep, keep_reason, source, sample_start) VALUES(?,?,?,?,?,?,?,?,?)",
                  (rec_id, out["kind"], out["summary"][:600], json.dumps(out["topics"], ensure_ascii=False), out["speakers"][:200], out["keep"], out["reason"][:300], source, start))
    return out
