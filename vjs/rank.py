"""Overnight re-ranking of the Triage pile.

The miner's 1–10 scores don't predict what the owner keeps (21–24% at every score, n=225), and small models
are poorly calibrated on absolute scores but decent at comparisons. So: show Qwen ~10 cards at a time,
ask for the 3 best and 3 weakest, repeat over several random groupings, and average the points.

`backtest()` replays this on cards you've already decided and compares the keep rate at the top of the
pile with today's order. The new order only goes live (kv rank_enabled) if it clearly wins."""
import random
import time

from . import db
from .llm import chat_json

SYSTEM = """You help a parent choose which recorded moments of their bilingual (Mandarin/English) daughter, about 5, go into a family journal.
The parent keeps moments where the child says something memorable: funny, surprising, clever or odd reasoning, a vivid turn of phrase,
a sweet or telling remark, a striking bilingual mix — something quotable in a line or two.
The parent does NOT keep: ordinary logistics, routine back-and-forth, stretches where adults do most of the talking,
garbled or nonsensical transcripts, or moments where the child barely speaks.
Transcripts come from speech recognition; speaker labels can be wrong.
You get several candidate moments. Return the ids of the 3 best (best first) and the 3 weakest."""

SCHEMA = {"type": "object", "properties": {
    "best": {"type": "array", "items": {"type": "integer"}},
    "worst": {"type": "array", "items": {"type": "integer"}}},
    "required": ["best", "worst"]}

KEPT = ("journal", "ready", "published")


def card_text(m, limit=420):
    segs = db.rows("SELECT coalesce(spk, spk_guess, 'other') spk, coalesce(text_final, text) t FROM segments "
                   "WHERE rec_id=? AND idx BETWEEN ? AND ? AND coalesce(flags,'') IN ('','media') ORDER BY idx",
                   m["rec_id"], m["seg_from"], m["seg_to"])
    s = " / ".join(f"{x['spk'].upper()}: {x['t']}" for x in segs)
    return s[:limit] + ("…" if len(s) > limit else "")


def _examples(backtest):
    from .mine import _journal_examples, _review_examples
    kept = _journal_examples(10)
    rejected = []
    if not backtest:  # in a backtest your own decisions would give the answers away
        kept += _review_examples(f"status IN {KEPT}", 6)
        rejected = _review_examples("status='rejected'", 6)
    s = "Quotes this parent KEPT:\n" + "\n".join(f"- {q}" for q in kept)
    if rejected:
        s += "\n\nMoments this parent SKIPPED:\n" + "\n".join(f"- {q}" for q in rejected)
    return s


def tournament(moments, rounds=5, group=10, backtest=False, verbose=True, seed=7):
    """moments: rows with id, rec_id, seg_from, seg_to → {id: points per appearance}."""
    ex = _examples(backtest)
    texts = {m["id"]: card_text(m) for m in moments}
    pts = {m["id"]: 0.0 for m in moments}
    seen = {m["id"]: 0 for m in moments}
    ids = list(texts)
    calls, t0 = 0, time.time()
    for r in range(rounds):
        random.Random(seed + r).shuffle(ids)
        groups = [ids[i:i + group] for i in range(0, len(ids), group)]
        for g in groups:
            if len(g) < 4:
                continue
            user = ex + "\n\nCandidates:\n" + "\n".join(f"[{i}] {texts[i]}" for i in g)
            try:
                res = chat_json(SYSTEM, user, SCHEMA, max_tokens=300)
            except Exception as e:
                print(f"    rank batch failed: {e}")
                continue
            gs = set(g)
            best = [i for i in res.get("best", []) if i in gs][:3]
            worst = [i for i in res.get("worst", []) if i in gs and i not in best][:3]
            for k, i in enumerate(best):
                pts[i] += 3 - k
            for i in worst:
                pts[i] -= 1
            for i in g:
                seen[i] += 1
            calls += 1
            if verbose and calls % 10 == 0:
                print(f"    round {r + 1}/{rounds} · {calls} comparisons · {time.time() - t0:.0f}s", flush=True)
    return {i: pts[i] / max(1, seen[i]) for i in ids}


def _top_rate(order, kept, frac=0.2):
    k = max(1, int(len(order) * frac))
    return sum(1 for i in order[:k] if i in kept) / k


def _auc(scores, kept):
    pos = [s for i, s in scores.items() if i in kept]
    neg = [s for i, s in scores.items() if i not in kept]
    if not pos or not neg:
        return 0.5
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def backtest(verbose=True):
    """Replay on decided cards (not published ones — those are the examples). Passes if the top fifth of the
    new order holds clearly more of your keeps than the top fifth of today's order."""
    ms = db.rows("SELECT m.*, r.recorded_at FROM moments m JOIN recordings r ON r.id=m.rec_id "
                 f"WHERE m.status IN ('journal','ready','log','rejected') AND m.id NOT IN ({db.SWEPT_SQL})")
    kept = {m["id"] for m in ms if m["status"] in KEPT}
    if verbose:
        print(f"  backtest on {len(ms)} cards you decided ({len(kept)} kept)")
    new = tournament(ms, backtest=True, verbose=verbose)
    with db.tx() as c:   # kept so vjs/keepers.py can stack the tournament with the other signals
        c.execute("CREATE TABLE IF NOT EXISTS card_scores(moment_id INTEGER, kind TEXT, score REAL, at TEXT DEFAULT (datetime('now','localtime')), PRIMARY KEY(moment_id, kind))")
        c.executemany("INSERT OR REPLACE INTO card_scores(moment_id, kind, score) VALUES(?,?,?)", [(i, "tournament_bt", float(v)) for i, v in new.items()])
    new_order = sorted(new, key=lambda i: -new[i])
    # today's Triage order: work stretches last, AI score, newest first
    old_order = [m["id"] for m in sorted(ms, key=lambda m: (m["priority"] if m["priority"] is not None else 1, -m["score"],
                                                           -int(m["recorded_at"][:10].replace("-", ""))))]
    res = {"n": len(ms), "kept": len(kept), "base": round(len(kept) / max(1, len(ms)), 3),
           "old_top20": round(_top_rate(old_order, kept), 3), "new_top20": round(_top_rate(new_order, kept), 3),
           "old_auc": round(_auc({m["id"]: m["score"] for m in ms}, kept), 3), "new_auc": round(_auc(new, kept), 3)}
    res["passed"] = res["new_top20"] >= res["old_top20"] + 0.08 and res["new_auc"] >= res["old_auc"] + 0.05
    db.kv_set("rank_backtest", res)
    if verbose:
        print(f"  keep rate overall {res['base']:.0%} · top fifth: today's order {res['old_top20']:.0%} → new {res['new_top20']:.0%}"
              f" · AUC {res['old_auc']} → {res['new_auc']} · {'PASSED' if res['passed'] else 'did not pass'}")
    return res


def apply(verbose=True):
    ms = db.rows("SELECT * FROM moments WHERE status='pending'")
    if verbose:
        print(f"  ranking {len(ms)} cards waiting in Triage")
    scores = tournament(ms, backtest=False, verbose=verbose)
    with db.tx() as c:
        c.executemany("UPDATE moments SET rank=? WHERE id=?", [(v, i) for i, v in scores.items()])
    return len(scores)


def order_sql():
    """Triage 'best first' order: work/media stretches still last; then the tournament; new unranked cards after.
    With the learned scorer live (vjs/learn.py): cards failing the trust check sink, then P(keep)."""
    if True:  # trust_ok: 0 = she barely speaks (vjs/learn.filter_thin) → bottom; learned score only if it passed its backtest
        learned = "m.learned IS NULL, m.learned DESC, " if db.kv_get("learned_enabled", False) else ""
        return ("CASE WHEN coalesce(m.priority,1)>=2 THEN 1 ELSE 0 END, coalesce(m.trust_ok,1) DESC, " + learned +
                "m.rank IS NULL, m.rank DESC, m.score DESC, r.recorded_at DESC, m.seg_from")
    return ("CASE WHEN coalesce(m.priority,1)>=2 THEN 1 ELSE 0 END, m.rank IS NULL, m.rank DESC, m.score DESC, "
            "r.recorded_at DESC, m.seg_from")


def run(verbose=True):
    res = backtest(verbose)
    apply(verbose)   # always refresh the waiting cards' scores (the keeper scorer stacks on them); only the *gate* decides whether Triage uses them alone
    if not res["passed"]:
        db.kv_set("rank_enabled", False)
        print("  new ranking didn't clearly beat today's order on its own — Triage order unchanged by it")
        return res
    db.kv_set("rank_enabled", True)
    print("  Triage now shows the best-ranked cards first")
    return res
