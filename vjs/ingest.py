"""Find new recordings in the configured source folders and register them."""
import hashlib
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from . import db
from .audio import probe_duration
from .config import CFG

FNAME_DT = re.compile(r"(20\d{2})(\d{2})(\d{2})[ _-]?(\d{2})(\d{2})(\d{2})")
APPLE_EPOCH = 978307200


def _voice_memo_titles():
    p = CFG.get("voice_memos_db")
    if not p or not os.path.exists(p):
        return {}
    try:
        c = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        out = {}
        for path, title, dur, zdate in c.execute("SELECT ZPATH, ZENCRYPTEDTITLE, ZDURATION, ZDATE FROM ZCLOUDRECORDING"):
            if path:
                when = datetime.fromtimestamp(zdate + APPLE_EPOCH) if zdate else None
                out[os.path.basename(path)] = (title, dur, when)
        return out
    except Exception:
        return {}


def _prior_hints():
    """Old review_index.tsv summaries → 'family' / 'work' / 'other' (processing order only)."""
    import csv
    p = CFG.get("prior_summaries")
    if not p or not os.path.exists(p):
        return {}
    out = {}
    for r in csv.DictReader(open(p, encoding="utf-8"), delimiter="\t"):
        s = (r.get("llm_summary") or "").lower()
        if any(w in s for w in ("daughter", "wife", "family", "bedtime", "kid")):
            out[r["file"]] = "family"
        elif any(w in s for w in ("work", "meeting", "colleague", "1:1", "manager", "team", "project", "interview")):
            out[r["file"]] = "work"
        elif s:
            out[r["file"]] = "other"
    return out


def recorded_at(path: Path):
    m = FNAME_DT.search(path.name)
    if m:
        y, mo, d, h, mi, s = map(int, m.groups())
        try:
            return datetime(y, mo, d, h, mi, s)
        except ValueError:
            pass
    return datetime.fromtimestamp(path.stat().st_mtime)


def rec_id_for(path: Path):
    return hashlib.sha1(f"{path.name}|{path.stat().st_size}".encode()).hexdigest()[:16]


def scan(verbose=True):
    exts = {e.lower() for e in CFG["audio_exts"]}
    min_date = datetime.fromisoformat(CFG["min_date"])
    titles = _voice_memo_titles()
    known = {r["id"] for r in db.rows("SELECT id FROM recordings")}
    hints = _prior_hints()
    with db.tx() as c:
        for fn, h in hints.items():
            c.execute("UPDATE recordings SET prior=? WHERE filename=? AND prior IS NULL", (h, fn))
    # The Voice Memos DB date beats the filename (some filenames carry the wrong year).
    with db.tx() as c:
        for r in db.rows("SELECT id, filename, recorded_at FROM recordings"):
            when = titles.get(r["filename"], (None, None, None))[2]
            if when and when.isoformat(timespec="seconds") != r["recorded_at"]:
                c.execute("UPDATE recordings SET recorded_at=? WHERE id=?", (when.isoformat(timespec="seconds"), r["id"]))
                c.execute("UPDATE recordings SET status='new' WHERE id=? AND status='skipped' AND ?>=?",
                          (r["id"], when.isoformat(), min_date.isoformat()))
    added = 0
    from .storage import ARCHIVE
    roots = list(CFG["sources"]) + ([str(d) for d in sorted(ARCHIVE.iterdir()) if d.is_dir()] if ARCHIVE.exists() else [])
    for src in roots:
        root = Path(src)
        if not root.exists():
            continue
        try:
            files = sorted(root.iterdir())
        except PermissionError:
            if verbose:
                print(f"  ! no permission to read {root} (grant Full Disk Access to your terminal)")
            continue
        for f in files:
            if f.suffix.lower() not in exts or not f.is_file() or f.name.startswith("."):
                continue
            if f.stat().st_size < 20_000:
                continue
            rid = rec_id_for(f)
            if rid in known:
                continue
            from .storage import _fp, known_copy
            dup = known_copy(f)
            if dup:  # same audio already ingested under another name (re-export / rename) — don't process it twice
                if verbose:
                    print(f"  = {f.name} is the same recording as {dup} — skipped")
                continue
            title, dur, vm_when = titles.get(f.name, (None, None, None))
            when = vm_when or recorded_at(f)
            status = "skipped" if when < min_date else "new"
            if status == "new" and not dur:
                dur = probe_duration(f)
            with db.tx() as c:
                c.execute(
                    "INSERT OR IGNORE INTO recordings(id,path,filename,recorded_at,duration,title,status,fp) VALUES(?,?,?,?,?,?,?,?)",
                    (rid, str(f), f.name, when.isoformat(timespec="seconds"), dur, title, status, _fp(f)),
                )
            known.add(rid)
            try:   # an orange Finder label = work, not for the journal: don't process it (vjs/finder_tags.py)
                from .finder_tags import is_orange, mark
                if is_orange(f):
                    mark(rid)
                    if verbose:
                        print(f"  🟠 {f.name}: orange label - marked as work, not processed")
                    continue
            except Exception as e:
                print(f"  (orange-label check skipped: {e})")
            if status == "new":
                added += 1
    if verbose:
        print(f"  ingest: {added} new recording(s)")
    return added
