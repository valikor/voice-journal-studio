"""Finder colour labels as instructions (2026-10-20). An ORANGE label on a memo means "work, not for the journal" (e.g. the owner practising
for an interview). Such a recording is not transcribed or mined: it is set to 'skipped' (restore any time from the 📥 Inbox) when it hasn't
been processed yet, and in every case it is marked category 'work', labelled, and kept out of the journal, the Reader's default list and
the language log. Applied when a file is ingested, when the pipeline starts (for memos labelled while queued), and with `./vjs.sh orange`.
Everything stays in studio.db; nothing about the content leaves the Mac."""
import json
import re
import subprocess
from pathlib import Path

from . import db, edits

NOTE = "Orange label in Finder: work (not for the journal)"


def tags_of(path):
    try:
        out = subprocess.run(["mdls", "-raw", "-name", "kMDItemUserTags", str(path)], capture_output=True, text=True, timeout=15).stdout
    except Exception:
        return []
    return [t.strip().lower() for t in re.findall(r'"?([^",()\n]+)"?', out.replace("(null)", "")) if t.strip()]


def is_orange(path):
    return any(t.startswith("orange") for t in tags_of(path))


def mark(rec_id, process=False):
    """Mark one recording as work. If it hasn't been processed yet (status new) it is skipped instead of transcribed."""
    r = db.one("SELECT id, status, filename FROM recordings WHERE id=?", rec_id)
    if not r:
        return None
    with db.tx() as c:
        if r["status"] in ("new", "transcribed", "analyzed"):
            c.execute("UPDATE recordings SET status='skipped', error='orange label (work) - not processed' WHERE id=?", (rec_id,))
        c.execute("UPDATE recordings SET category='work', prior='work' WHERE id=?", (rec_id,))
        c.execute("UPDATE moments SET status='no_kid' WHERE rec_id=? AND status='pending'", (rec_id,))
        c.execute("INSERT INTO recording_label(rec_id, note, tags) VALUES(?,?,?) ON CONFLICT(rec_id) DO UPDATE SET note=CASE WHEN coalesce(note,'')='' THEN excluded.note ELSE note END, "
                  "tags=excluded.tags", (rec_id, NOTE, json.dumps(["work", "orange label"])))
        c.execute("INSERT OR IGNORE INTO recording_summary(rec_id, source) VALUES(?, 'orange')", (rec_id,))
        c.execute("UPDATE recording_summary SET user_kind='work meeting', user_at=datetime('now','localtime') WHERE rec_id=? AND coalesce(user_kind,'')=''", (rec_id,))
        edits.log(c, "orange", r["status"], "work", None, None, "finder-tag")
    return r["filename"]


def refresh(days=45, verbose=True):
    """Apply orange labels. Looks at the files in the Inbox (that's where you label them, even when the processed original has been filed
    elsewhere) and at recordings still waiting; matches Inbox files to recordings by filename."""
    inbox = Path.home() / "Desktop" / "Voice Memos Inbox"
    by_name = {r["filename"]: r for r in db.rows("SELECT id, filename, category, status, path FROM recordings")}
    cand = {}
    for f in inbox.glob("*"):
        if f.suffix.lower() in (".m4a", ".qta", ".wav", ".mp3") and f.name in by_name and is_orange(f):
            cand[by_name[f.name]["id"]] = f.name
    for r in db.rows("SELECT id, path, filename FROM recordings WHERE recorded_at >= date('now', ?) OR status IN ('new','transcribed','analyzed')", f"-{days} day"):
        if r["id"] not in cand and Path(r["path"]).exists() and is_orange(Path(r["path"])):
            cand[r["id"]] = r["filename"]
    done = []
    for rid, name in cand.items():
        cur = by_name[name]
        if cur["category"] == "work" and db.one("SELECT 1 FROM recording_label WHERE rec_id=? AND note LIKE 'Orange label%'", rid):
            continue   # already applied
        if mark(rid):
            done.append(name)
    if verbose and done:
        print(f"  orange label: {len(done)} recording(s) marked as work, not for the journal: " + ", ".join(done))
    return done
