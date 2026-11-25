"""Recording Reader backend (2026-10-14): one recording as a continuous transcript — no card boundaries — with the cards, her
punchline picks and review status drawn over it, and a way to make/replace/skip cards from any span of lines.

Statuses used here (never trained on, never shown as a decision): 'superseded' (a waiting card replaced by a span you kept),
'reader_skip' (marker card for a stretch you skipped, so the Reader knows it was reviewed), 'adhoc' (an invisible helper card so the
existing edit endpoints have a card to log against). Everything is logged in `edits` and undoable (`span_undo`)."""
import json

from . import db, edits

REVIEWED = ("journal", "ready", "published", "log", "rejected", "auto_skipped", "thin", "no_kid", "superseded", "reader_skip")
WAITING = ("pending", "later")
HIDDEN_OK = ("", "media")   # same visibility rule the cards use


def recordings(month=None, year=None, kind=None, q=None, limit=400):
    hs = _has_summary()
    eff = "coalesce(s.user_kind, s.kind)" if hs else "NULL"
    where, args = ["r.status != 'delete_candidate'"], []
    if month: where.append("substr(r.recorded_at,1,7)=?"); args.append(month)
    if year: where.append("substr(r.recorded_at,1,4)=?"); args.append(str(year))
    if kind:
        where.append(f"{eff}=?"); args.append(kind)
    elif not q:   # work meetings stay out of the way unless you ask for them
        where.append(f"(r.category IS NOT 'work' AND coalesce({eff},'') != 'work meeting')")
    if q:
        where.append("(r.title LIKE ? OR r.filename LIKE ? OR r.id IN (SELECT rec_id FROM recording_label WHERE note LIKE ? OR tags LIKE ?)" + (" OR s.summary LIKE ? OR s.topics LIKE ?" if hs else "") + ")")
        args += [f"%{q}%", f"%{q}%", f"%{q}%", f"%{q}%"] + ([f"%{q}%", f"%{q}%"] if hs else [])
    join = "LEFT JOIN recording_summary s ON s.rec_id=r.id" if hs else ""
    return db.rows(f"""SELECT r.id, r.filename, r.recorded_at, r.duration, r.title, r.category, r.status, {eff} kind,
        (SELECT count(*) FROM moments m WHERE m.rec_id=r.id AND m.status IN ('pending','later')) waiting,
        (SELECT count(*) FROM moments m WHERE m.rec_id=r.id AND m.status IN ('journal','ready','published')) kept,
        (SELECT count(*) FROM moments m WHERE m.rec_id=r.id AND m.punch_seg IS NOT NULL AND m.status NOT IN ('removed','merged','adhoc')) punch,
        (SELECT count(*) FROM segments s2 WHERE s2.rec_id=r.id AND coalesce(s2.spk,s2.spk_guess)='kid' AND coalesce(s2.flags,'') IN ('','media')) her_lines
        FROM recordings r {join} WHERE {' AND '.join(where)} ORDER BY r.recorded_at DESC LIMIT ?""", *args, limit)


def _has_summary():
    return bool(db.one("SELECT 1 FROM sqlite_master WHERE name='recording_summary'"))


def recording(rec_id):
    r = db.one("SELECT id, filename, recorded_at, duration, title, category, status FROM recordings WHERE id=?", rec_id)
    if not r:
        return None
    segs = db.rows("SELECT id, idx, start s, end e, coalesce(spk, spk_guess, 'other') sp, spk IS NOT NULL conf, coalesce(text_final, text) t, "
                   "text_final IS NOT NULL x, coalesce(flags,'') f, src FROM segments WHERE rec_id=? ORDER BY start, idx", rec_id)
    moms = db.rows("SELECT id, status, seg_from, seg_to, score, punch_seg, punch_tag, why FROM moments WHERE rec_id=? "
                   "AND status NOT IN ('removed','adhoc','merged','empty')", rec_id)
    summ = db.one("SELECT coalesce(user_kind, kind) kind, summary, topics, speakers, keep, keep_reason FROM recording_summary WHERE rec_id=?", rec_id) if _has_summary() else None
    return {"rec": r, "segments": segs, "moments": moms, "summary": summ}


# ---------------------------------------------------------------- spans → cards
def _bounds(rec_id, a_id, b_id):
    a = db.one("SELECT idx FROM segments WHERE id=? AND rec_id=?", int(a_id), rec_id)
    b = db.one("SELECT idx FROM segments WHERE id=? AND rec_id=?", int(b_id), rec_id)
    if not a or not b:
        raise ValueError("segment not in this recording")
    return min(a["idx"], b["idx"]), max(a["idx"], b["idx"])


def ensure_card(rec_id, lo, hi, status, why="made from a span in the Reader", c=None):
    """The card for exactly [lo, hi] (created if needed). Returns (id, created, previous_status)."""
    ex = db.one("SELECT id, status FROM moments WHERE rec_id=? AND seg_from=? AND seg_to=?", rec_id, lo, hi)
    own = c is None
    ctx = db.tx() if own else None
    cur = ctx.__enter__() if own else c
    try:
        if ex:
            prev = ex["status"]
            if status and prev != status:
                cur.execute("UPDATE moments SET status=?, triaged_at=datetime('now','localtime') WHERE id=?", (status, ex["id"]))
            return ex["id"], False, prev
        cur.execute("INSERT INTO moments(rec_id, seg_from, seg_to, score, tags, why, status, triaged_at) VALUES(?,?,?,?,?,?,?,datetime('now','localtime'))",
                    (rec_id, lo, hi, 10, '["reader"]', why, status))
        return cur.execute("SELECT last_insert_rowid()").fetchone()[0], True, None
    finally:
        if own:
            ctx.__exit__(None, None, None)


def span_action(rec_id, a_id, b_id, action):
    """action: keep (journal) | log | skip. Returns what's needed to undo it."""
    lo, hi = _bounds(rec_id, a_id, b_id)
    overlap = db.rows("SELECT id, status FROM moments WHERE rec_id=? AND status IN ('pending','later') AND seg_from<=? AND seg_to>=?", rec_id, hi, lo)
    out = {"lo": lo, "hi": hi, "action": action, "superseded": [], "new": None}
    with db.tx() as c:
        if action in ("keep", "log"):
            status = "journal" if action == "keep" else "log"
            mid, created, prev = ensure_card(rec_id, lo, hi, status, c=c)
            out["new"] = {"id": mid, "created": created, "prev": prev}
            for o in overlap:
                if o["id"] != mid:
                    c.execute("UPDATE moments SET status='superseded' WHERE id=?", (o["id"],))
                    edits.log(c, "supersede", o["status"], f"by m{mid}", None, o["id"], "reader")
                    out["superseded"].append({"id": o["id"], "prev": o["status"]})
            edits.log(c, "reader-span", prev or "", f"{action} {lo}-{hi}", None, mid, "reader")
        elif action == "skip":
            for o in overlap:
                c.execute("UPDATE moments SET status='rejected', triaged_at=datetime('now','localtime') WHERE id=?", (o["id"],))
                edits.log(c, "reader-skip", o["status"], "rejected", None, o["id"], "reader")
                out["superseded"].append({"id": o["id"], "prev": o["status"]})
            mid, created, prev = ensure_card(rec_id, lo, hi, "reader_skip", why="stretch skipped in the Reader", c=c)
            out["new"] = {"id": mid, "created": created, "prev": prev}
        else:
            raise ValueError("unknown action")
    if action == "keep":
        try:
            from .server import enqueue
            enqueue(out["new"]["id"])
        except Exception as e:
            print("enqueue skipped:", e)
    return out


def span_undo(u):
    with db.tx() as c:
        n = u.get("new")
        if n:
            if n["created"]:
                c.execute("UPDATE moments SET status='removed' WHERE id=?", (n["id"],))
            elif n.get("prev"):
                c.execute("UPDATE moments SET status=? WHERE id=?", (n["prev"], n["id"]))
            edits.log(c, "reader-undo", "", str(n["id"]), None, n["id"], "reader")
        for s in u.get("superseded", []):
            c.execute("UPDATE moments SET status=? WHERE id=?", (s["prev"], s["id"]))
            edits.log(c, "reader-undo", "", f"m{s['id']} back to {s['prev']}", None, s["id"], "reader")
    return {"ok": True}


def card_for_edit(rec_id, a_id, b_id):
    """A card covering exactly this span, so the normal edit endpoints (segment/split/addline/hide) have something to log against."""
    lo, hi = _bounds(rec_id, a_id, b_id)
    mid, created, prev = ensure_card(rec_id, lo, hi, "adhoc", why="helper card for Reader edits")
    return mid
