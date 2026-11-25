"""Who can be in a recording — rules the OWNER configures (config.local.json "rules"), enforced after every intake and on demand (`./vjs.sh rules`).
Examples: nothing before the year the child started talking; one year where only listed recordings contain her; in work recordings a child/mom guess
in the middle of work talk is almost certainly wrong. Never touches lines the owner labelled himself."""
from . import db
from .config import CFG



def no_kid(r):
    """True if this recording cannot contain the child, per the owner's config ("rules": {"kid_first_year": 2023, "year_allowlist": {"2024": ["title", ...]}}).
    With no rules configured nothing is excluded."""
    if r.get("kid_ok"):   # the owner confirmed the child is in this one
        return False
    cfg = CFG.get("rules") or {}
    y = (r["recorded_at"] or "")[:4]
    first = cfg.get("kid_first_year")
    if first and y and y < str(first):
        return True
    allow = (cfg.get("year_allowlist") or {}).get(y)
    return allow is not None and (r["title"] or "") not in set(allow)


def enforce(verbose=True):
    recs = db.rows("SELECT id, recorded_at, title, status, category, prior, kid_ok FROM recordings")
    nk = [r for r in recs if no_kid(r)]
    fixed = dropped = skipped = work_fixed = 0
    with db.tx() as c:
        for r in nk:
            fixed += c.execute("UPDATE segments SET spk_guess='other', spk_conf=0.3 WHERE rec_id=? AND spk IS NULL AND spk_guess='kid'",
                               (r["id"],)).rowcount
            dropped += c.execute("UPDATE moments SET status='no_kid' WHERE rec_id=? AND status='pending'", (r["id"],)).rowcount
            if r["status"] == "new" and not db.one("SELECT archive FROM recordings WHERE id=?", r["id"])["archive"]:  # nothing for the journal in it — don't spend GPU hours on it
                c.execute("UPDATE recordings SET status='skipped', error='no child by the owner''s rule' WHERE id=?", (r["id"],)); skipped += 1
        # work recordings: kid/mom guesses surrounded by work talk → someone else
        for r in recs:
            if r["category"] != "work" and r["prior"] != "work":
                continue
            segs = db.rows("SELECT id, spk, spk_guess FROM segments WHERE rec_id=? AND coalesce(flags,'') IN ('','media') ORDER BY idx", r["id"])
            for i, s in enumerate(segs):
                if s["spk"] or s["spk_guess"] not in ("kid", "mom"):
                    continue
                around = [x["spk"] or x["spk_guess"] for x in segs[max(0, i - 5):i] + segs[i + 1:i + 6]]
                if around and sum(a in ("kid", "mom") for a in around) <= len(around) // 3:  # mostly adult/work talk around it
                    c.execute("UPDATE segments SET spk_guess='other', spk_conf=0.3 WHERE id=?", (s["id"],)); work_fixed += 1
    # work moments whose 'her' lines are now gone → out of Triage
    from .learn import trust
    with db.tx() as c:
        for m in db.rows("SELECT m.* FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status='pending' AND (r.category='work' OR r.prior='work')"):
            if trust(m)["kid_lines"] == 0:
                c.execute("UPDATE moments SET status='no_kid' WHERE id=?", (m["id"],)); dropped += 1
    if verbose:
        print(f"  rules: {len(nk)} recordings can't contain the child · {fixed} 'child' guesses → someone else · "
              f"{work_fixed} Child/Mom guesses inside work talk → someone else · {dropped} Triage card(s) removed (status no_kid) · "
              f"{skipped} unprocessed recording(s) skipped")


def allow_kid(rec_ids):
    """the owner found her in a recording the rule had set aside. Reversible with deny_kid(). What changes: the recording is exempt from the
    rule; cards the rule parked (status no_kid) go back to Triage; lines the voice/pitch models had called her (before the rule
    rewrote them to 'other' at confidence 0.3) go back to her — only lines nobody labelled and not clearly an adult voice; a recording
    that was skipped unprocessed is queued for transcription at the next ./vjs.sh run."""
    from . import edits
    pitch = db.one("SELECT 1")  # placeholder so the function reads top-down
    res = {"recordings": 0, "cards_back": 0, "lines_back": 0, "queued": 0}
    with db.tx() as c:
        for rid in rec_ids:
            r = db.one("SELECT id, status, error FROM recordings WHERE id=?", rid)
            if not r:
                continue
            c.execute("UPDATE recordings SET kid_ok=1 WHERE id=?", (rid,)); res["recordings"] += 1
            res["lines_back"] += c.execute("UPDATE segments SET spk_guess='kid' WHERE rec_id=? AND spk IS NULL AND spk_guess='other' AND spk_conf=0.3 "
                                           "AND coalesce(adult_like,0)=0 AND (f0_med IS NULL OR f0_med >= 240)", (rid,)).rowcount
            for m in db.rows("SELECT id FROM moments WHERE rec_id=? AND status='no_kid'", rid):
                c.execute("UPDATE moments SET status='pending' WHERE id=?", (m["id"],)); res["cards_back"] += 1
                edits.log(c, "kid-ok", "no_kid", "pending", None, m["id"], "rules")
            if r["status"] == "skipped" and (r["error"] or "").startswith("no child"):
                c.execute("UPDATE recordings SET status='new', error=NULL WHERE id=?", (rid,)); res["queued"] += 1
    return res


def deny_kid(rec_ids):
    """Undo allow_kid: the rule applies again (cards parked, lines back to 'someone else', unprocessed recordings skipped)."""
    with db.tx() as c:
        for rid in rec_ids:
            c.execute("UPDATE recordings SET kid_ok=0 WHERE id=?", (rid,))
    enforce(verbose=False)
    return {"recordings": len(rec_ids)}


def candidates(year=None, rec=None):
    """Recordings the rule currently sets aside (optionally one year, or the year of one recording)."""
    recs = db.rows("SELECT id, recorded_at, title, status, category, prior, kid_ok FROM recordings")
    if rec:
        y = next((r["recorded_at"][:4] for r in recs if r["id"] == rec), None)
        year = year or y
    return [r for r in recs if no_kid(r) and (not year or (r["recorded_at"] or "")[:4] == str(year))]
