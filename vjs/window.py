"""Better-window suggestion (2026-10-16): cards often start too late (the setup is missing) or run on after the payoff. Given her best
line (punch_seg) the ideal card is: up to SETUP lines before it (each within SETUP_GAP seconds of the next, so it's the same exchange),
the punchline, and at most one reaction line after it (within REACT_GAP s). If that differs from the card by at least one line,
the Triage card offers it as a one-click "use" (it never changes anything by itself). Deterministic — no model call."""
from . import db

SETUP, SETUP_GAP, REACT_GAP = 3, 6.0, 4.0


def suggest(m):
    if not m.get("punch_seg") or m["status"] not in ("pending", "later"):
        return None
    segs = db.rows("SELECT id, idx, start, end, coalesce(spk,spk_guess,'other') sp FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? "
                   "AND coalesce(flags,'') IN ('','media') ORDER BY idx", m["rec_id"], m["seg_from"] - 8, m["seg_to"] + 8)
    pos = next((i for i, s in enumerate(segs) if s["id"] == m["punch_seg"]), None)
    if pos is None:
        return None
    lo = pos
    while lo > 0 and pos - lo < SETUP and segs[lo]["start"] - segs[lo - 1]["end"] <= SETUP_GAP:
        lo -= 1
    hi = pos
    if pos + 1 < len(segs) and segs[pos + 1]["start"] - segs[pos]["end"] <= REACT_GAP:
        hi = pos + 1
    new_from, new_to = segs[lo]["idx"], segs[hi]["idx"]
    cur = [x for x in segs if m["seg_from"] <= x["idx"] <= m["seg_to"]]
    cpos = next((i for i, x in enumerate(cur) if x["id"] == m["punch_seg"]), None)
    if cpos is None:
        return None
    lead, tail = cpos, len(cur) - 1 - cpos                      # lines before / after her best line inside the card today
    avail = pos - lo                                            # setup lines that actually exist just before it
    b = a = 0
    prev = segs[pos - 1] if pos > 0 else None
    if lead == 0 and avail >= 1 and prev and prev["sp"] != "kid" and segs[pos]["start"] - prev["end"] <= 3.0:
        b = min(avail, 2)                                       # her line opens the card but replies to an adult line just before it: the setup is missing
    elif lead >= 6:                                            # a long lead-in: start closer to the point
        b = -(lead - SETUP)
    if tail >= 3:                                              # the card runs on after the payoff
        a = -(tail - 1)
    if b == 0 and a == 0:
        return None
    start_i = sum(1 for x in segs if x["idx"] < m["seg_from"] and x["idx"] >= segs[max(0, pos - lead - b)]["idx"]) if False else None
    parts = []
    if b > 0: parts.append(f"+{b} line{'s' if b != 1 else ''} before (the setup)")
    if b < 0: parts.append(f"starts {-b} line{'s' if b != -1 else ''} later")
    if a < 0: parts.append(f"ends {-a} line{'s' if a != -1 else ''} earlier")
    # translate line counts into the range action's idx deltas
    vis_from = [x for x in segs if x["idx"] < m["seg_from"]]
    new_from_idx = vis_from[-b]["idx"] if b > 0 and len(vis_from) >= b else (cur[-b]["idx"] if b < 0 else m["seg_from"])
    new_to_idx = cur[len(cur) - 1 + a]["idx"] if a < 0 else m["seg_to"]
    return {"text": " · ".join(parts), "before": m["seg_from"] - new_from_idx, "after": new_to_idx - m["seg_to"]}


def new_from_delta(cur_from, new_from):
    return cur_from - new_from   # positive = extend earlier (range action's "before")
