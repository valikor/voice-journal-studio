"""Feature #1 (2026-10-07): fewer false positives — score each Triage card by how likely the owner is to keep it,
learned from his own decisions, with a trust check on the quote itself.

Trust (per card, no model): does the line the picker praised actually appear in her lines? did the second listen
agree on her lines? is the speaker really her (confirmed or confident voice match)? does she say enough?
Cards that fail are "needs ears" and sink in Triage — they're the misquotes.

Learned score: logistic regression on a local text embedding of the card (nomic, via LM Studio) + the trust numbers +
the tournament rank, trained on every card the owner decided (kept = journal/ready/published vs log/rejected). Validated
with 5-fold cross-validation grouped by recording, against the current order. It goes live (kv learned_enabled) only
if it beats the tournament on keeps-in-the-top-fifth. Retrain any time: `./vjs.sh learn`."""
import json
import re

import numpy as np

from . import db

KEPT = ("journal", "ready", "published")
DECIDED = ("journal", "ready", "published", "log", "rejected")


def _norm(t):
    return re.sub(r"[^\w㐀-鿿 ]", "", (t or "").lower())


def trust(m):
    from .autopolish import _quoted
    segs = db.rows("SELECT id, coalesce(text_final, text) t, coalesce(spk, spk_guess) sp, spk, spk_conf FROM segments "
                   "WHERE rec_id=? AND idx BETWEEN ? AND ? AND coalesce(flags,'') IN ('','media') ORDER BY idx",
                   m["rec_id"], m["seg_from"], m["seg_to"])
    kid = [s for s in segs if s["sp"] == "kid"]
    kid_text = _norm(" ".join(s["t"] for s in kid))
    quotes = [q for q in _quoted(m["why"]) if len(_norm(q)) >= 3]
    found = [q for q in quotes if _norm(q) and _norm(q) in _norm(" ".join(s["t"] for s in segs))]
    rl = json.loads(m["relisten"]) if m["relisten"] else {}
    v = [((rl.get("lines") or {}).get(str(s["id"])) or {}).get("verdict") for s in kid]
    judged = [x for x in v if x in ("agree", "disputed", "you")]
    agree = (sum(x in ("agree", "you") for x in judged) / len(judged)) if judged else None
    sure = [s for s in kid if s["spk"] == "kid" or (s["spk_conf"] or 0) >= 0.6]
    t = {"quote_found": (len(found) / len(quotes)) if quotes else None, "agree": agree,
         "voice_sure": (len(sure) / len(kid)) if kid else 0.0, "kid_chars": len(kid_text), "kid_lines": len(kid),
         "lines": len(segs), "inconclusive": bool(rl.get("inconclusive"))}
    t["ok"] = ((t["quote_found"] is None or t["quote_found"] >= 0.5) and (agree is None or agree >= 0.5)
               and t["voice_sure"] >= 0.5 and t["kid_chars"] >= 15)
    return t


def _numeric(m, t):
    return [t["quote_found"] if t["quote_found"] is not None else 0.5, t["agree"] if t["agree"] is not None else 0.5,
            t["voice_sure"], min(t["kid_chars"], 400) / 400, min(t["kid_lines"], 15) / 15, min(t["lines"], 30) / 30,
            float(t["inconclusive"]), float(t["ok"]), (m["rank"] if m["rank"] is not None else 0.0) / 3, (m["score"] or 0) / 10]


def _features(ms):
    from .compare import _embed
    from .rank import card_text
    ts = [trust(m) for m in ms]
    texts = [card_text(m) or "(empty)" for m in ms]
    emb = np.vstack([_embed(texts[i:i + 64]) for i in range(0, len(texts), 64)])
    num = np.array([_numeric(m, t) for m, t in zip(ms, ts)])
    return np.hstack([emb, num * 4]), ts  # scale numerics up so they matter next to 768 unit-norm dims


def _top(scores, kept, frac=0.2):
    o = np.argsort(-scores)[:max(1, int(len(scores) * frac))]
    return kept[o].mean()


def train(verbose=True):
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.model_selection import GroupKFold
    ms = db.rows(f"SELECT * FROM moments WHERE status IN {DECIDED} AND id NOT IN ({db.SWEPT_SQL})")
    X, ts = _features(ms)
    y = np.array([m["status"] in KEPT for m in ms], dtype=int)
    groups = np.array([m["rec_id"] for m in ms])
    pred = np.zeros(len(ms))
    for tr, te in GroupKFold(n_splits=5).split(X, y, groups):
        clf = LogisticRegression(C=0.5, class_weight="balanced", max_iter=2000).fit(X[tr], y[tr])
        pred[te] = clf.predict_proba(X[te])[:, 1]
    rank = np.array([m["rank"] if m["rank"] is not None else 0.0 for m in ms])
    ok = np.array([t["ok"] for t in ts])
    res = {"n": len(ms), "kept": int(y.sum()), "base": round(float(y.mean()), 3),
           "rank_top20": round(float(_top(rank, y)), 3), "learned_top20": round(float(_top(pred, y)), 3),
           "rank_auc": round(float(roc_auc_score(y, rank)), 3), "learned_auc": round(float(roc_auc_score(y, pred)), 3),
           "keep_rate_trust_ok": round(float(y[ok].mean()), 3) if ok.any() else None,
           "keep_rate_needs_ears": round(float(y[~ok].mean()), 3) if (~ok).any() else None, "needs_ears_share": round(float((~ok).mean()), 3)}
    res["passed"] = res["learned_top20"] >= res["rank_top20"] + 0.05 and res["learned_auc"] > res["rank_auc"]
    db.kv_set("learned_backtest", res)
    if verbose:
        print(f"  {res['n']} decided cards ({res['kept']} kept, {res['base']:.0%}) · keeps in the top fifth: tournament {res['rank_top20']:.0%}"
              f" → learned {res['learned_top20']:.0%} · AUC {res['rank_auc']} → {res['learned_auc']} · {'PASSED' if res['passed'] else 'did not pass'}")
        print(f"  trust check: keep rate {res['keep_rate_trust_ok']:.0%} when it passes vs {res['keep_rate_needs_ears']:.0%} when it fails"
              f" ({res['needs_ears_share']:.0%} of cards fail)")
    if res["passed"]:
        clf = LogisticRegression(C=0.5, class_weight="balanced", max_iter=2000).fit(X, y)
        pend = db.rows("SELECT * FROM moments WHERE status='pending'")
        for i in range(0, len(pend), 200):
            chunk = pend[i:i + 200]
            Xp, tp = _features(chunk)
            p = clf.predict_proba(Xp)[:, 1]
            with db.tx() as c:
                c.executemany("UPDATE moments SET learned=?, trust_ok=? WHERE id=?",
                              [(float(pp), int(t["ok"]), m["id"]) for pp, t, m in zip(p, tp, chunk)])
        db.kv_set("learned_enabled", True)
        if verbose:
            print(f"  scored {len(pend)} waiting cards; Triage now orders by the learned score (trust failures last)")
    return res


# ---------------------------------------------------------------- what actually predicts keeps (2026-10-07 backtest)
# On 636 decided cards the strongest signal is simply how much she says (AUC 0.61); quote accuracy / second-listen
# agreement don't predict keeps (0.48–0.49 — the owner fixes the words). Cards where she says < 10 characters: 0 of 58 kept.
THIN = 10   # chars of her speech; below → filed as 'thin' (reversible, out of Triage)
SINK = 15   # below → bottom of Triage


def filter_thin(verbose=True):
    n_thin = n_sink = 0
    with db.tx() as c:
        for m in db.rows("SELECT * FROM moments WHERE status='pending'"):
            k = trust(m)["kid_chars"]
            if k < THIN:
                c.execute("UPDATE moments SET status='thin' WHERE id=?", (m["id"],)); n_thin += 1
            c.execute("UPDATE moments SET trust_ok=? WHERE id=?", (0 if k < SINK else 1, m["id"]))
            n_sink += k < SINK
    if verbose:
        print(f"  {n_thin} card(s) where she barely speaks filed as 'thin' (restore: UPDATE moments SET status='pending' WHERE status='thin');"
              f" {n_sink - n_thin} short one(s) sink to the bottom")
    return n_thin
