"""Auto-skip the bottom of the pile (2026-10-13) — conservative and fully reversible.

`./vjs.sh autoskip --pct 25` moves the lowest 25% of waiting cards (by the current Triage order, unranked cards count as
lowest, starred cards are never touched) to status 'auto_skipped' — never 'rejected', never deleted. Every card is logged in
`edits` (kind 'autoskip', with its previous status). Restore: `./vjs.sh autoskip --restore [--month 2026-03] [--top 30]`
(--top 30 = only the best-ranked 30% of the auto-skipped pile). Cards in the pile open in Triage (stage "Auto-skipped") and
can be kept or restored one by one.

Safety net: Daily 5 slips up to 5 random auto-skipped cards into each session (unlabelled). If the owner keeps more than 1 in 10 of the
last 40 sampled, the Library shows "auto-skip may be too aggressive" and suggests restoring the top slice. The cut-off is
never tightened automatically."""
import math

from . import db, edits

SAMPLE_WINDOW = 40
WARN_RATE = 0.10
MIN_SAMPLES = 20   # don't warn on a handful of cards


def _ordered_pending(extra=""):
    from .rank import order_sql
    order = order_sql() if db.ranked() else "coalesce(m.priority,1), m.score DESC, r.recorded_at DESC, m.seg_from"
    return db.rows(f"SELECT m.id, m.status, m.star FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status='pending' {extra} ORDER BY {order}")


def run(pct=25, dry=False, by="order"):
    """by='order' (default, as specified): bottom of the current Triage order. by='model': bottom by the leak-free keep-predictor
    (vjs/lightning.py) — on 795 past decisions its bottom 25% held 11% keepers (loses ~13% of the gold) vs ~18% for the order."""
    pend = _ordered_pending()
    n = len(pend)
    k = int(round(n * pct / 100.0))
    if by == "model":
        from . import lightning
        st = lightning.model(force=True)
        p = {m["id"]: float(x) for m, x in zip(st["pend"], st["pp"])}
        low = sorted(pend, key=lambda m: p.get(m["id"], 0.0))[:k]
        bottom = [m for m in low if not m["star"]]
    else:
        bottom = [m for m in pend[n - k:] if not m["star"]] if k else []
    if not dry:
        with db.tx() as c:
            for m in bottom:
                c.execute("UPDATE moments SET status='auto_skipped' WHERE id=? AND status='pending'", (m["id"],))
                edits.log(c, "autoskip", m["status"], "auto_skipped", None, m["id"], f"pct={pct} by={by}")
    left = db.one("SELECT count(*) n FROM moments WHERE status='pending'")["n"]
    return {"waiting_before": n, "pct": pct, "moved": len(bottom), "waiting_now": left if not dry else n,
            "auto_skipped_total": db.one("SELECT count(*) n FROM moments WHERE status='auto_skipped'")["n"]}


def restore(month=None, top=None):
    where = "AND substr(r.recorded_at,1,7)='%s'" % month.replace("'", "") if month else ""
    from .rank import order_sql
    order = order_sql() if db.ranked() else "coalesce(m.priority,1), m.score DESC, r.recorded_at DESC, m.seg_from"
    rows = db.rows(f"SELECT m.id FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status='auto_skipped' {where} ORDER BY {order}")
    if top:
        rows = rows[: int(math.ceil(len(rows) * top / 100.0))]
    with db.tx() as c:
        for r in rows:
            prev = db.one("SELECT before FROM edits WHERE moment_id=? AND kind='autoskip' ORDER BY id DESC LIMIT 1", r["id"])
            c.execute("UPDATE moments SET status=? WHERE id=? AND status='auto_skipped'", ((prev or {}).get("before") or "pending", r["id"]))
            edits.log(c, "autoskip-restore", "auto_skipped", (prev or {}).get("before") or "pending", None, r["id"], f"month={month} top={top}")
    return {"restored": len(rows), "auto_skipped_left": db.one("SELECT count(*) n FROM moments WHERE status='auto_skipped'")["n"]}


# ---- safety-net bookkeeping
def record_sample(mid, decision, via):
    """Called when a card that was auto-skipped gets a decision. via = 'sample' (slipped into Daily 5) or 'pile'."""
    log = db.kv_get("autoskip_log", [])
    log.append({"id": mid, "decision": decision, "via": via, "at": db.one("SELECT datetime('now','localtime') t")["t"]})
    db.kv_set("autoskip_log", log[-400:])


def sample_stats():
    smp = [x for x in db.kv_get("autoskip_log", []) if x["via"] == "sample"][-SAMPLE_WINDOW:]
    kept = sum(1 for x in smp if x["decision"] == "journal")
    rate = kept / len(smp) if smp else None
    warn = bool(smp) and len(smp) >= MIN_SAMPLES and rate > WARN_RATE
    return {"pile": db.one("SELECT count(*) n FROM moments WHERE status='auto_skipped'")["n"], "sampled": len(smp), "kept": kept,
            "rate": None if rate is None else round(rate, 3), "warn": warn,
            "suggest": "./vjs.sh autoskip --restore --top 30" if warn else None}
