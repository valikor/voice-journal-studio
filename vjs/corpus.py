"""Corpus view: where every recording and card stands, and whether review is keeping up with arrivals.

Human decisions per week come from moments.triaged_at, minus cards the machine filed (sweep / auto-skip), so the pace
is *your* pace. 'Arriving' is moments.created_at (by week). The ETA uses the last 4 complete weeks."""
import datetime as dt

from . import db

KEPT = ("journal", "ready", "published")
OPEN = ("pending",)                                   # still waiting for a human decision
PILES = ("thin", "no_kid", "auto_skipped", "later", "empty", "merged", "superseded", "reader_skip")


def _machine_ids():
    return {r["moment_id"] for r in db.rows(
        "SELECT moment_id FROM edits e WHERE kind IN ('sweep','autoskip') AND id=(SELECT max(id) FROM edits WHERE moment_id=e.moment_id "
        "AND kind IN ('sweep','sweep-undo','autoskip','autoskip-restore'))")}


def _monday(d):
    return d - dt.timedelta(days=d.weekday())


def corpus(first_year=2023):
    rec = db.rows("SELECT substr(recorded_at,1,7) mo, count(*) n, sum(coalesce(duration,0))/3600.0 h, "
                  "sum(CASE WHEN status='skipped' THEN coalesce(duration,0) ELSE 0 END)/3600.0 h_skipped FROM recordings GROUP BY 1")
    cards = db.rows("SELECT substr(r.recorded_at,1,7) mo, m.status, count(*) n FROM moments m JOIN recordings r ON r.id=m.rec_id GROUP BY 1,2")
    cell = {}
    for r in rec:
        cell[r["mo"]] = {"month": r["mo"], "recordings": r["n"], "hours": round(r["h"], 1), "hours_skipped": round(r["h_skipped"], 1),
                         "waiting": 0, "kept": 0, "published": 0, "log": 0, "rejected": 0, "piles": {p: 0 for p in PILES}}
    for r in cards:
        c = cell.setdefault(r["mo"], {"month": r["mo"], "recordings": 0, "hours": 0, "hours_skipped": 0, "waiting": 0, "kept": 0,
                                      "published": 0, "log": 0, "rejected": 0, "piles": {p: 0 for p in PILES}})
        st, n = r["status"], r["n"]
        if st in OPEN: c["waiting"] += n
        elif st in KEPT: c["kept"] += n
        if st == "published": c["published"] += n
        if st == "log": c["log"] += n
        if st == "rejected": c["rejected"] += n
        if st in PILES: c["piles"][st] += n
    for c in cell.values():  # share of cards that still need a human decision (filed-away piles don't count against you)
        live = c["waiting"] + c["kept"] + c["log"] + c["rejected"]
        c["unreviewed"] = round(c["waiting"] / live, 3) if live else 0.0
    months = sorted(cell.values(), key=lambda c: c["month"])
    recent = [c for c in months if c["month"] and int(c["month"][:4]) >= first_year]
    older = [c for c in months if c["month"] and int(c["month"][:4]) < first_year]
    older_by_year = {}
    for c in older:
        y = older_by_year.setdefault(c["month"][:4], {"year": c["month"][:4], "recordings": 0, "hours": 0.0, "waiting": 0, "kept": 0, "published": 0})
        for k in ("recordings", "hours", "waiting", "kept", "published"):
            y[k] += c[k]
    piles = {p: db.one("SELECT count(*) n FROM moments WHERE status=?", p)["n"] for p in PILES}
    piles["recordings_skipped_by_date"] = db.one("SELECT count(*) n FROM recordings WHERE status='skipped'")["n"]
    # weekly flow
    machine = _machine_ids()
    arrive, decide = {}, {}
    # "arriving" = cards from audio *recorded* that week (a backlog processed in one night isn't new inflow)
    for r in db.rows("SELECT m.id, r.recorded_at created_at, m.triaged_at, m.status FROM moments m JOIN recordings r ON r.id=m.rec_id"):
        if r["created_at"] and r["status"] not in ("merged", "empty", "removed"):
            w = _monday(dt.date.fromisoformat(r["created_at"][:10])); arrive[w] = arrive.get(w, 0) + 1
        if r["triaged_at"] and r["status"] in KEPT + ("log", "rejected", "later") and r["id"] not in machine:
            w = _monday(dt.date.fromisoformat(r["triaged_at"][:10])); decide[w] = decide.get(w, 0) + 1
    today = _monday(dt.date.today())
    weeks = [today - dt.timedelta(weeks=k) for k in range(25, -1, -1)]
    flow = [{"week": w.isoformat(), "arrived": arrive.get(w, 0), "decided": decide.get(w, 0)} for w in weeks]
    last4 = [f for f in flow[-5:-1]]  # last 4 complete weeks
    pace = sum(f["decided"] for f in last4) / 4
    inflow = sum(f["arrived"] for f in last4) / 4
    waiting = db.one("SELECT count(*) n FROM moments WHERE status='pending'")["n"]
    unprocessed = db.one("SELECT count(*) n, coalesce(sum(duration),0)/3600.0 h FROM recordings WHERE status IN ('new','error')")
    eta = {"waiting": waiting, "decided_per_week": round(pace, 1), "arriving_per_week": round(inflow, 1)}
    if pace > inflow:
        weeks_left = waiting / (pace - inflow)
        eta.update(weeks=round(weeks_left, 1), date=(dt.date.today() + dt.timedelta(weeks=weeks_left)).isoformat())
    else:
        eta.update(weeks=None, date=None, note="At the last 4 weeks' pace new cards arrive faster than you decide them.")
    return {"months": recent, "older_years": sorted(older_by_year.values(), key=lambda y: y["year"]), "piles": piles,
            "flow": flow, "eta": eta, "unprocessed": {"recordings": unprocessed["n"], "hours": round(unprocessed["h"], 1)}}
