"""Labels and notes on recordings (2026-10-14): "Biratnagar — my Nepal trip, hanging out with Robin". For recordings that aren't about her
(and any other) so they can be inventoried, found and sorted. Local only; lives in studio.db (`recording_label`).
A "place" is the Voice Memos location title without its trailing number ("Renrenxiang (Jiuxianqiao Branch) 3" → "Renrenxiang (Jiuxianqiao Branch)")."""
import json
import re

from . import db, edits


def place_of(title):
    t = re.sub(r"\s+\d+$", "", (title or "").strip())
    return t or "(no place)"


def get(rec_id):
    r = db.one("SELECT note, tags FROM recording_label WHERE rec_id=?", rec_id)
    return {"note": r["note"] or "", "tags": json.loads(r["tags"] or "[]")} if r else {"note": "", "tags": []}


def set_label(rec_ids, note=None, tags=None, mode="set"):
    """mode 'set': replace note/tags with what's given · 'add': add these tags (and set the note if given and none exists) on top."""
    n = 0
    with db.tx() as c:
        for rid in rec_ids:
            cur = get(rid)
            new_note = cur["note"] if note is None else (note if mode == "set" or not cur["note"] else cur["note"])
            tg = list(cur["tags"]) if mode == "add" else []
            for t in (tags or []):
                t = t.strip()
                if t and t not in tg:
                    tg.append(t)
            if mode == "set" and tags is None:
                tg = cur["tags"]
            c.execute("INSERT INTO recording_label(rec_id, note, tags) VALUES(?,?,?) ON CONFLICT(rec_id) DO UPDATE SET note=excluded.note, tags=excluded.tags, at=datetime('now','localtime')",
                      (rid, new_note, json.dumps(tg, ensure_ascii=False)))
            edits.log(c, "label", json.dumps(cur, ensure_ascii=False), json.dumps({"note": new_note, "tags": tg}, ensure_ascii=False), None, None, "label")
            n += 1
    return {"labelled": n}


def all_tags():
    seen = {}
    for r in db.rows("SELECT tags FROM recording_label"):
        for t in json.loads(r["tags"] or "[]"):
            seen[t] = seen.get(t, 0) + 1
    return sorted(seen, key=lambda t: -seen[t])


def places(everything=False):
    """One row per place: how many recordings, hours, date range, kinds, labels, what's been decided. Default = recordings not about her."""
    from . import sortrec
    ids = None if everything else set(sortrec.scope_ids()) | {r["id"] for r in db.rows("SELECT id FROM recordings WHERE status='delete_candidate'")}
    rows = db.rows("SELECT r.id, r.title, r.recorded_at, r.duration, r.status, r.archive, coalesce(s.user_kind, s.kind) kind, s.keep, l.note, l.tags "
                   "FROM recordings r LEFT JOIN recording_summary s ON s.rec_id=r.id LEFT JOIN recording_label l ON l.rec_id=r.id")
    g = {}
    for r in rows:
        if ids is not None and r["id"] not in ids:
            continue
        p = g.setdefault(place_of(r["title"]), {"place": place_of(r["title"]), "n": 0, "hours": 0.0, "first": r["recorded_at"], "last": r["recorded_at"], "kinds": {}, "delete_candidates": 0,
                                                "kept": 0, "labelled": 0, "tags": [], "notes": [], "ids": []})
        p["n"] += 1; p["hours"] += (r["duration"] or 0) / 3600.0; p["ids"].append(r["id"])
        p["first"] = min(p["first"], r["recorded_at"]); p["last"] = max(p["last"], r["recorded_at"])
        if r["kind"]:
            p["kinds"][r["kind"]] = p["kinds"].get(r["kind"], 0) + 1
        p["delete_candidates"] += r["status"] == "delete_candidate"
        p["kept"] += bool(r["archive"])
        if r["note"] or r["tags"] and r["tags"] != "[]":
            p["labelled"] += 1
            for t in json.loads(r["tags"] or "[]"):
                if t not in p["tags"]:
                    p["tags"].append(t)
            if r["note"] and r["note"] not in p["notes"]:
                p["notes"].append(r["note"])
    out = sorted(g.values(), key=lambda p: (-p["n"], p["place"]))
    for p in out:
        p["hours"] = round(p["hours"], 1)
    return out
