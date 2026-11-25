"""Every human correction goes through here: saved immediately, logged (before → after), and
learned from. The log doubles as ground truth for measuring transcript accuracy."""
import difflib
import re

from . import db

TOK = re.compile(r"[㐀-鿿]|[A-Za-z0-9']+", re.U)


# what counts as "the owner worked on this card" (system actions like merge/autoclean/autoskip/sweep don't)
MACHINE_VIA = ("relisten", "backfill", "format-fix", "format-fix-live", "repair", "autoclean", "claude")  # not the owner's hands
USER_EDIT_KINDS = ("text", "speaker", "rendered", "split", "addline", "hide", "unhide", "include", "note_public", "note_private")


def log(c, kind, before, after, seg_id=None, moment_id=None, via=""):
    c.execute("INSERT INTO edits(kind, seg_id, moment_id, before, after, via) VALUES(?,?,?,?,?,?)",
              (kind, seg_id, moment_id, before, after, via))
    if kind in USER_EDIT_KINDS and via not in MACHINE_VIA:
        if moment_id:
            c.execute("UPDATE moments SET touched_at=datetime('now','localtime') WHERE id=?", (moment_id,))
        elif seg_id:  # a line edit with no card attached (Quality-check, Reader, format fixes): every card over that line is touched
            c.execute("UPDATE moments SET touched_at=datetime('now','localtime') WHERE id IN ("
                      "SELECT m.id FROM moments m JOIN segments s ON s.rec_id=m.rec_id AND s.idx BETWEEN m.seg_from AND m.seg_to "
                      "WHERE s.id=? AND m.status IN ('pending','later'))", (seg_id,))


def backfill_touched():
    """One-off reconcile: cards that already have your edits logged get touched_at from the log. Idempotent."""
    ks = ",".join(f"'{k}'" for k in USER_EDIT_KINDS)
    vs = ",".join(f"'{v}'" for v in MACHINE_VIA)
    with db.tx() as c:
        c.execute(f"UPDATE moments SET touched_at=(SELECT max(e.at) FROM edits e WHERE e.moment_id=moments.id AND e.kind IN ({ks}) AND coalesce(e.via,'') NOT IN ({vs})) "
                  f"WHERE touched_at IS NULL AND id IN (SELECT moment_id FROM edits WHERE moment_id IS NOT NULL AND kind IN ({ks}) AND coalesce(via,'') NOT IN ({vs}))")
        c.execute(f"UPDATE moments SET touched_at=(SELECT max(e.at) FROM edits e JOIN segments s ON s.id=e.seg_id "
                  f"WHERE e.moment_id IS NULL AND e.kind IN ({ks}) AND s.rec_id=moments.rec_id AND s.idx BETWEEN moments.seg_from AND moments.seg_to) "
                  f"WHERE touched_at IS NULL AND status IN ('pending','later') AND EXISTS (SELECT 1 FROM edits e JOIN segments s ON s.id=e.seg_id "
                  f"WHERE e.moment_id IS NULL AND e.kind IN ({ks}) AND s.rec_id=moments.rec_id AND s.idx BETWEEN moments.seg_from AND moments.seg_to)")


def edited_but_new():
    """Invariant: no card with your edits may sit in the New pile. Returns offending ids (should be [])."""
    return [r["id"] for r in db.rows("SELECT id FROM moments WHERE status='pending' AND touched_at IS NULL AND id IN ("
                                     "SELECT moment_id FROM edits WHERE moment_id IS NOT NULL AND kind IN (%s) AND coalesce(via,'') NOT IN (%s))" % (",".join(f"'{k}'" for k in USER_EDIT_KINDS), ",".join(f"'{v}'" for v in MACHINE_VIA)))]


def learn(c, before, after):
    """Word-level replacements ("muzzles"→"muscles", "his home"→"Kid's Cove") become household vocabulary."""
    a, b = TOK.findall(before or ""), TOK.findall(after or "")
    sm = difflib.SequenceMatcher(None, [x.lower() for x in a], [x.lower() for x in b])
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op != "replace" or not (1 <= i2 - i1 <= 4 and 1 <= j2 - j1 <= 4):
            continue
        join = lambda toks: "".join(toks) if all(re.match(r"[㐀-鿿]", t) for t in toks) else " ".join(toks)
        wrong, right = join(a[i1:i2]), join(b[j1:j2])
        if wrong.lower() == right.lower() or right.startswith("-"):
            continue
        c.execute("INSERT INTO lexicon(wrong, right, n, last) VALUES(?,?,1,datetime('now','localtime')) "
                  "ON CONFLICT(wrong, right) DO UPDATE SET n=n+1, last=excluded.last", (wrong, right))


def set_segment(seg_id, text=None, speaker=None, via="", moment_id=None):
    """Correct one transcript line. Also updates any Polish draft line built from it (unless that
    draft line was hand-edited separately)."""
    from .polish import emoji
    from .textutil import lang_of, render_line
    s = db.one("SELECT id, text, text_final, spk, spk_guess FROM segments WHERE id=?", seg_id)
    if not s:
        return
    cur_text = s["text_final"] or s["text"]
    cur_spk = s["spk"] or s["spk_guess"]
    with db.tx() as c:
        if text is not None and text.strip() and text.strip() != cur_text:
            text = text.strip()
            c.execute("UPDATE segments SET text_final=?, lang=?, q=1.0 WHERE id=?", (text, lang_of(text), seg_id))
            log(c, "text", cur_text, text, seg_id, moment_id, via)
            learn(c, cur_text, text)
            for l in db.rows("SELECT id, speaker, edited FROM moment_lines WHERE seg_id=?", seg_id):
                c.execute("UPDATE moment_lines SET text=?, rendered=? WHERE id=?",
                          (text, render_line(emoji(l["speaker"]), text), l["id"]))
        if speaker and speaker != s["spk"]:
            c.execute("UPDATE segments SET spk=? WHERE id=?", (speaker, seg_id))
            log(c, "speaker", cur_spk, speaker, seg_id, moment_id, via)
            for l in db.rows("SELECT id, rendered FROM moment_lines WHERE seg_id=?", seg_id):
                r = l["rendered"] or ""
                if "\n" not in r:  # a box you split into several lines is yours — don't rewrite it
                    r = re.sub(r"^\S+", emoji(speaker), r)
                c.execute("UPDATE moment_lines SET speaker=?, rendered=? WHERE id=?", (speaker, r, l["id"]))


def stats():
    return {
        "total": db.one("SELECT count(*) n FROM edits")["n"],
        "by_kind": db.rows("SELECT kind, count(*) n FROM edits GROUP BY kind"),
        "recent": db.rows("SELECT at, kind, before, after, via FROM edits ORDER BY id DESC LIMIT 20"),
        "lexicon": db.rows("SELECT wrong, right, n FROM lexicon ORDER BY n DESC, last DESC LIMIT 60"),
    }
