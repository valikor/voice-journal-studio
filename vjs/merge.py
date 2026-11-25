"""Merge nearby Triage cards into one exchange (2026-10-13 pilot) — fully reversible.

Cards in the same recording whose audio sits within GAP seconds of each other are usually one conversation cut into
pieces. The best-ranked card of a group becomes the *keeper* and grows to cover the whole exchange; the others are
set to status 'merged' (hidden from Triage, nothing deleted). Every merge is recorded in `moment_merges` with the
keeper's old range / second-listen / why, so `undo(batch)` puts each card back exactly as it was. Only cards that
are still 'pending' and have no Polish lines are touched; an undo skips groups whose keeper you've since decided
(the merged card is then yours to keep or fix like any other)."""
import json
import time
from collections import defaultdict

from . import db, edits

GAP = 30.0        # seconds between one card's end and the next one's start
MAX_SPAN = 120.0  # a merged exchange never grows past this
MAX_CARDS = 4


def _ensure():
    with db.tx() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS moment_merges(
            id INTEGER PRIMARY KEY, batch TEXT, keeper_id INTEGER, absorbed_id INTEGER, keeper_from INTEGER, keeper_to INTEGER,
            keeper_relisten TEXT, keeper_why TEXT, absorbed_why TEXT, at TEXT DEFAULT (datetime('now','localtime')), undone INTEGER DEFAULT 0)""")


def _pending():
    return db.rows("""SELECT m.id, m.rec_id, m.seg_from, m.seg_to, m.score, m.rank, m.why, m.relisten,
        (SELECT start FROM segments s WHERE s.rec_id=m.rec_id AND s.idx=m.seg_from) t0,
        (SELECT end FROM segments s WHERE s.rec_id=m.rec_id AND s.idx=m.seg_to) t1
        FROM moments m WHERE m.status='pending' AND NOT EXISTS (SELECT 1 FROM moment_lines l WHERE l.moment_id=m.id)""")


def plan(top_ids=None):
    """Groups of cards to merge. top_ids (a set) restricts the pilot to groups whose keeper is among those cards."""
    by = defaultdict(list)
    for m in _pending():
        if m["t0"] is not None and m["t1"] is not None:
            by[m["rec_id"]].append(m)
    taken = {(r["rec_id"], r["seg_from"], r["seg_to"]) for r in db.rows("SELECT rec_id, seg_from, seg_to FROM moments")}
    groups = []
    for rec, items in by.items():
        items.sort(key=lambda m: m["t0"])
        cur = [items[0]]
        for m in items[1:] + [None]:
            end = max(x["t1"] for x in cur)
            if m is not None and m["t0"] - end <= GAP and m["t1"] - cur[0]["t0"] <= MAX_SPAN and len(cur) < MAX_CARDS:
                cur.append(m)
                continue
            if len(cur) > 1:
                keeper = max(cur, key=lambda x: (x["rank"] if x["rank"] is not None else -9, x["score"] or 0))
                lo, hi = min(x["seg_from"] for x in cur), max(x["seg_to"] for x in cur)
                if (rec, lo, hi) not in taken and (top_ids is None or keeper["id"] in top_ids):
                    groups.append({"rec": rec, "keeper": keeper, "cards": cur, "from": lo, "to": hi,
                                   "span": round(max(x["t1"] for x in cur) - cur[0]["t0"], 1)})
            cur = [m] if m is not None else []
    return groups


def apply(groups, batch=None):
    _ensure()
    batch = batch or time.strftime("%Y%m%d-%H%M%S")
    n_cards = 0
    with db.tx() as c:
        for g in groups:
            k = g["keeper"]
            for x in g["cards"]:
                if x["id"] == k["id"]:
                    continue
                c.execute("INSERT INTO moment_merges(batch, keeper_id, absorbed_id, keeper_from, keeper_to, keeper_relisten, keeper_why, absorbed_why) "
                          "VALUES(?,?,?,?,?,?,?,?)", (batch, k["id"], x["id"], k["seg_from"], k["seg_to"], k["relisten"], k["why"], x["why"]))
                c.execute("UPDATE moments SET status='merged' WHERE id=? AND status='pending'", (x["id"],))
                edits.log(c, "merge", f"m{x['id']} pending", f"into m{k['id']}", None, x["id"], "merge-pilot")
                n_cards += 1
            c.execute("UPDATE moments SET seg_from=?, seg_to=?, relisten=NULL WHERE id=?", (g["from"], g["to"], k["id"]))  # second listen redoes itself
            edits.log(c, "merge", f"m{k['id']} {k['seg_from']}-{k['seg_to']}", f"{g['from']}-{g['to']}", None, k["id"], "merge-pilot")
    return {"batch": batch, "groups": len(groups), "cards_absorbed": n_cards}


def undo(batch):
    _ensure()
    rows = db.rows("SELECT * FROM moment_merges WHERE batch=? AND undone=0 ORDER BY id", batch)
    restored = skipped = 0
    seen = set()
    with db.tx() as c:
        for r in rows:
            k = db.one("SELECT status FROM moments WHERE id=?", r["keeper_id"])
            if not k or k["status"] != "pending":
                skipped += 1  # keeper already decided: leave the merged card as it is
                c.execute("UPDATE moment_merges SET undone=2 WHERE id=?", (r["id"],))
                continue
            if r["keeper_id"] not in seen:
                seen.add(r["keeper_id"])
                c.execute("UPDATE moments SET seg_from=?, seg_to=?, relisten=?, why=? WHERE id=?",
                          (r["keeper_from"], r["keeper_to"], r["keeper_relisten"], r["keeper_why"], r["keeper_id"]))
            c.execute("UPDATE moments SET status='pending' WHERE id=? AND status='merged'", (r["absorbed_id"],))
            c.execute("UPDATE moment_merges SET undone=1 WHERE id=?", (r["id"],))
            edits.log(c, "merge-undo", f"m{r['absorbed_id']} merged", "pending", None, r["absorbed_id"], "merge-pilot")
            restored += 1
    return {"restored": restored, "skipped_decided": skipped}


def batches():
    _ensure()
    return db.rows("SELECT batch, count(*) cards, count(DISTINCT keeper_id) groups, sum(undone=0) active, min(at) at "
                   "FROM moment_merges GROUP BY batch ORDER BY batch DESC")
