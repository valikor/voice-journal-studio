"""Keeper scorer (2026-10-16): does the punchline pass (her best line, its tag, the 1-5 "would you publish it" rating) predict what
the owner actually keeps — beyond what cheap structure already does? Backtest first; ordering changes only if it wins.

Data: every card the owner decided (kept = journal/ready/published; not kept = log/rejected), minus machine-filed (sweep) cards and the
cards used as examples in the punch prompt, scored by the v2 punch pass WITHOUT his corrections (vjs/punchlines.py, leak-free).
Validation: 5-fold CV grouped by recording (so a conversation never sits in both train and test), repeated 4x with different splits.
Models: structure only (vjs/lightning._struct) vs structure + punch features vs + card embedding. Gate to go live: AUC at least +0.03
over structure-only AND keep rate in the top fifth at least +0.03. Live = writes P(keep) to moments.learned and turns learned_enabled on
(the existing hook Triage's order already uses). `./vjs.sh keepers` runs it."""
import re

import numpy as np

from . import db
from .lightning import _struct

KEPT = ("journal", "ready", "published")
CJK = re.compile(r"[㐀-鿿]")
LATIN = re.compile(r"[A-Za-z]{2,}")
TAGS = {"invented": "invent", "codeswitch": "code-switch", "sass": "sass", "sweet": "sweet", "funny": "funn", "reasoning": "reason", "pretend": "pretend"}
GATE_AUC, GATE_TOP = 0.03, 0.03


def _punch_feats(m):
    seg = db.one("SELECT id, idx, coalesce(text_final, text) t FROM segments WHERE id=?", m["punch_seg"]) if m["punch_seg"] else None
    her = db.rows("SELECT id, idx FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? AND coalesce(spk_guess,'other')='kid' "
                  "AND coalesce(flags,'') IN ('','media') ORDER BY idx", m["rec_id"], m["seg_from"], m["seg_to"])
    t = (seg["t"] if seg else "") or ""
    tag = (m["punch_tag"] or "").lower()
    f = [float(m["punch_score"] or 0) / 5, float(seg is not None), np.log1p(len(t)) / 5,
         float(bool(CJK.search(t) and LATIN.search(t))), float(bool(CJK.search(t))), min(t.count("!"), 3) / 3,
         float(bool(seg) and bool(her) and seg["id"] == her[-1]["id"]),                  # the payoff is her last line
         (np.log1p(seg["idx"] - m["seg_from"]) / 3) if seg else 0.0,                      # how much setup precedes it
         min(len(her), 12) / 12]
    f += [float(key in tag) for key in TAGS.values()]
    return f


def _tournament(ms):
    """The tournament's score for each card from the last ranking backtest (NaN where it has none, e.g. published cards)."""
    have = {r["moment_id"]: r["score"] for r in db.rows("SELECT moment_id, score FROM card_scores WHERE kind='tournament_bt'")} \
        if db.one("SELECT 1 FROM sqlite_master WHERE name='card_scores'") else {}
    return np.array([have.get(m["id"], np.nan) for m in ms], dtype=float)


def _load(min_cards=1):
    ex = set(db.kv_get("punch_examples", []))
    ms = db.rows("SELECT * FROM moments WHERE status IN ('journal','ready','published','log','rejected') AND punch_v=2 "
                 f"AND id NOT IN ({db.SWEPT_SQL})")
    ms = [m for m in ms if m["id"] not in ex]
    S = np.array([_struct(m)[0] for m in ms], dtype=float) if ms else np.zeros((0, 9))
    P = np.array([_punch_feats(m) for m in ms], dtype=float) if ms else np.zeros((0, 16))
    y = np.array([m["status"] in KEPT for m in ms], dtype=int)
    g = np.array([m["rec_id"] for m in ms])
    return ms, S, P, y, g


def _cv(make, X, y, g, reps=4):
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    aucs, tops, pred_all = [], [], np.zeros(len(y))
    for r in range(reps):
        rng = np.random.RandomState(r); ug = np.unique(g); perm = dict(zip(ug, rng.permutation(len(ug)))); gg = np.array([perm[x] for x in g])
        p = np.zeros(len(y))
        for tr, te in GroupKFold(5).split(X, y, gg):
            p[te] = make().fit(X[tr], y[tr]).predict_proba(X[te])[:, 1]
        k = max(1, int(len(y) * 0.2)); top = np.argsort(-p)[:k]
        aucs.append(roc_auc_score(y, p)); tops.append(y[top].mean()); pred_all += p / reps
    return float(np.mean(aucs)), float(np.std(aucs)), float(np.mean(tops)), pred_all


def backtest(verbose=True):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    ms, S, P, y, g = _load()
    if len(ms) < 120 or y.sum() < 20:
        return {"ready": False, "n": len(ms), "reason": f"only {len(ms)} decided cards have a v2 punch score yet (run ./vjs.sh punchlines --scope decided)"}
    lr = lambda C=0.3: (lambda: make_pipeline(StandardScaler(), LogisticRegression(C=C, max_iter=3000)))
    res = {"ready": True, "n": len(ms), "kept": int(y.sum()), "base": round(float(y.mean()), 3)}
    a0, s0, t0, p0 = _cv(lr(), S, y, g)
    a1, s1, t1, p1 = _cv(lr(), np.hstack([S, P]), y, g)
    a2, s2, t2, _ = _cv(lr(), P, y, g)
    res.update(struct_auc=round(a0, 3), struct_top20=round(t0, 3), combined_auc=round(a1, 3), combined_sd=round(s1, 3), combined_top20=round(t1, 3),
               punch_only_auc=round(a2, 3), rating_alone_auc=round(float(roc_auc_score(y, P[:, 0])), 3) if len(set(P[:, 0])) > 1 else None)
    # what auto-skip would lose if it cut the bottom quarter by this score
    cut = np.argsort(p1)[: int(len(y) * 0.25)]
    res["bottom25_keep_rate"] = round(float(y[cut].mean()), 3)
    res["bottom25_gold_lost"] = round(float(y[cut].sum() / max(1, y.sum())), 3)
    # stacking: add the tournament score, on the cards that have one (it is only computed for cards other than the published examples)
    T = _tournament(ms)
    ok = ~np.isnan(T)
    if ok.sum() >= 150 and y[ok].sum() >= 20:
        a_s, _, t_s, _ = _cv(lr(), S[ok], y[ok], g[ok])
        a_t, _, t_t, _ = _cv(lr(), T[ok].reshape(-1, 1), y[ok], g[ok])
        a_all, sd_all, t_all, p_all = _cv(lr(), np.hstack([S[ok], P[ok], T[ok].reshape(-1, 1)]), y[ok], g[ok])
        res["stack"] = {"n": int(ok.sum()), "kept": int(y[ok].sum()), "struct_auc": round(a_s, 3), "struct_top20": round(t_s, 3), "tournament_auc": round(a_t, 3), "tournament_top20": round(t_t, 3),
                        "all_auc": round(a_all, 3), "all_sd": round(sd_all, 3), "all_top20": round(t_all, 3)}
        # the bar is the BEST single signal (usually the tournament), not the weakest: stacking must beat it, or it only adds noise
        best_a, best_t = max(a_s, a_t), max(t_s, t_t)
        res["stack"]["best_single"] = "tournament" if a_t >= a_s else "structure"
        res["stack_passed"] = bool(a_all >= best_a + GATE_AUC and t_all >= best_t + GATE_TOP)
    res["passed"] = bool(a1 >= a0 + GATE_AUC and t1 >= t0 + GATE_TOP)
    # which punch features matter (standardised LR coefficients on all data)
    names = ["rating", "has_pick", "len", "mixed_zh_en", "has_zh", "excl", "payoff_last", "setup", "her_lines"] + list(TAGS)
    coef = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=3000)).fit(P, y)[-1].coef_[0]
    res["top_features"] = [(names[i], round(float(coef[i]), 2)) for i in np.argsort(-np.abs(coef))[:6]]
    db.kv_set("keeper_backtest", res)
    if verbose:
        print(f"  {res['n']} decided cards ({res['kept']} kept, {res['base']:.0%}) — leak-free punch scores, CV grouped by recording")
        print(f"  structure only      AUC {res['struct_auc']}   top-fifth keep rate {res['struct_top20']:.0%}")
        print(f"  structure + punch   AUC {res['combined_auc']} ± {res['combined_sd']}   top-fifth keep rate {res['combined_top20']:.0%}")
        print(f"  punch features alone AUC {res['punch_only_auc']} · LLM 1-5 rating alone AUC {res['rating_alone_auc']}")
        print(f"  bottom quarter by this score holds {res['bottom25_keep_rate']:.0%} keepers (a cut there loses {res['bottom25_gold_lost']:.0%} of the gold)")
        print(f"  strongest features: {res['top_features']}")
        print(f"  → punch features: {'PASSED the gate' if res['passed'] else 'did not clear the gate (needs +%.2f AUC and +%.2f top-fifth)' % (GATE_AUC, GATE_TOP)}")
        if res.get("stack"):
            st = res["stack"]
            print(f"  stacking on {st['n']} cards ({st['kept']} kept) that have a tournament score:")
            print(f"    structure {st['struct_auc']} / top-fifth {st['struct_top20']:.0%}  ·  tournament alone {st['tournament_auc']} / {st['tournament_top20']:.0%}  ·  ALL THREE {st['all_auc']} ± {st['all_sd']} / {st['all_top20']:.0%}")
            print(f"    → vs the best single signal ({st['best_single']}): {'PASSED the gate' if res['stack_passed'] else 'did not clear the gate — stacking adds no value over it'}")
    return res


def apply(verbose=True):
    """If the backtest passed: fit on every decided card, score the waiting ones (they need v2 punch scores) → moments.learned."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    res = db.kv_get("keeper_backtest", {})
    if res.get("stack_passed"):
        return _apply_stack(verbose)
    if not res.get("passed"):
        if verbose:
            print("  backtest not passed — Triage order unchanged")
        return 0
    ms, S, P, y, g = _load()
    clf = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=3000)).fit(np.hstack([S, P]), y)
    pend = db.rows("SELECT * FROM moments WHERE status='pending' AND punch_v=2")
    X = np.hstack([np.array([_struct(m)[0] for m in pend]), np.array([_punch_feats(m) for m in pend])])
    p = clf.predict_proba(X)[:, 1]
    with db.tx() as c:
        c.executemany("UPDATE moments SET learned=? WHERE id=?", [(float(v), m["id"]) for v, m in zip(p, pend)])
    db.kv_set("learned_enabled", True)
    if verbose:
        print(f"  scored {len(pend)} waiting cards; Triage now orders by this keeper score (after the work/short-line rules)")
    return len(pend)


def _apply_stack(verbose=True):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    ms, S, P, y, g = _load()
    T = _tournament(ms); ok = ~np.isnan(T)
    clf = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=3000)).fit(np.hstack([S[ok], P[ok], T[ok].reshape(-1, 1)]), y[ok])
    pend = db.rows("SELECT * FROM moments WHERE status='pending' AND punch_v=2 AND rank IS NOT NULL")
    X = np.hstack([np.array([_struct(m)[0] for m in pend]), np.array([_punch_feats(m) for m in pend]), np.array([[m["rank"]] for m in pend])])
    p = clf.predict_proba(X)[:, 1]
    with db.tx() as c:
        c.executemany("UPDATE moments SET learned=? WHERE id=?", [(float(v), m["id"]) for v, m in zip(p, pend)])
    db.kv_set("learned_enabled", True)
    if verbose:
        print(f"  scored {len(pend)} waiting cards with the stacked keeper score (structure + punchline + tournament); Triage now orders by it")
    return len(pend)
