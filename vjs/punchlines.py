"""Overnight punchline pass (2026-10-13): for every waiting card, the local LLM names HER single best line and a 3–5 word
reason. Stored on the moment (punch_seg = segments.id of one of her real lines — so the text is verbatim by construction —
and punch_tag). Quick-judge shows just that line + the line before it, so a card can be judged in ~3 seconds.

Resumable: cards the pass has looked at have punch_at set. Cards are processed in Triage order (best-ranked first), so a
partial run is still useful. `./vjs.sh punchlines [--limit N] [--redo] [--batch 10]`."""
import json
import time

from . import db, llm

SYSTEM = """You help a parent find the best moments in recordings of their young daughter (age ~5, bilingual Mandarin/English).
For each card (a short stretch of conversation) pick HER single best line — the one a parent would most want to quote in a
family journal — by its numeric id. It must be one of the lines marked HER. Prefer: invented or over-regularised words
("winned", "you-yong'd"), code-switching inside a sentence, sass or bossiness, sweet or funny things, surprising reasoning,
clever wordplay, pretend-play lines with a payoff. Pick the PAYOFF line, not the setup. If nothing she says is quotable,
return best_seg = 0.
Also give ONE tag of 3 to 5 words, no commas, saying why (e.g. "invented word", "code-switch mid-sentence", "sassy comeback",
"sweet to dad", "surprising reasoning", "funny misunderstanding", "pretend play payoff"). Be strict and literal: use
"code-switch" only if HER chosen line itself mixes Chinese and English; "invented word" only if she actually says a non-standard
word; otherwise use a plain description such as "sweet moment" or "funny line". Never invent text; only choose ids from the card.
Finally give quote_score 1-5 = how likely this parent would publish THIS CARD as a journal quote: 5 = certainly (a clear punchline or
standout moment with enough setup to understand it), 4 = probably, 3 = maybe, 2 = unlikely, 1 = no (routine talk, noise, or the
funny part is missing). Most cards deserve 1-2; reserve 4-5 for the best. Compare with the loved and passed-on examples you are shown."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["cards"], "properties": {"cards": {"type": "array", "items": {
    "type": "object", "additionalProperties": False, "required": ["id", "best_seg", "tag", "quote_score"],
    "properties": {"id": {"type": "integer"}, "best_seg": {"type": "integer"}, "tag": {"type": "string"},
                   "quote_score": {"type": "integer", "enum": [1, 2, 3, 4, 5]}}}}}}


def _card_lines(m, leakfree=False):
    """The card's lines. leakfree=True (used for already-decided cards in a backtest) uses only the machine's own speaker guess and the
    raw transcript and ignores lines the owner added or split, so nothing he did while deciding can leak into the score."""
    if leakfree:
        return db.rows("SELECT id, coalesce(spk_guess, 'other') spk, text t FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? "
                       "AND coalesce(flags,'') IN ('','media') AND coalesce(src,'') NOT IN ('manual','split') ORDER BY idx", m["rec_id"], m["seg_from"], m["seg_to"])
    return db.rows("SELECT id, coalesce(spk, spk_guess, 'other') spk, coalesce(text_final, text) t FROM segments WHERE rec_id=? "
                   "AND idx BETWEEN ? AND ? AND coalesce(flags,'') IN ('','media') ORDER BY idx", m["rec_id"], m["seg_from"], m["seg_to"])


def _examples():
    """Taste examples: her lines from cards the owner published (loved) and from cards he passed on (rejected / log-only), a few each.
    Their card ids are saved (kv punch_examples) so the backtest can leave them out."""
    pos = db.rows("SELECT m.id mid, l.text FROM moment_lines l JOIN moments m ON m.id=l.moment_id WHERE m.status='published' AND l.include=1 "
                  "AND l.speaker='kid' AND length(l.text) BETWEEN 25 AND 90 ORDER BY m.star DESC, m.id DESC LIMIT 60")
    neg = db.rows("SELECT m.id mid, l.text FROM moment_lines l JOIN moments m ON m.id=l.moment_id WHERE m.status='rejected' AND l.include=1 "
                  "AND l.speaker='kid' AND length(l.text) BETWEEN 25 AND 90 ORDER BY m.id DESC LIMIT 60")
    def pick(rows, n):
        out, seen = [], set()
        for r in rows:
            if r["text"] not in seen and len(out) < n:
                seen.add(r["text"]); out.append(r)
        return out
    P, N = pick(pos, 8), pick(neg, 6)
    db.kv_set("punch_examples", [r["mid"] for r in P + N])
    return {"loved": [r["text"] for r in P], "passed": [r["text"] for r in N]}


import re
CJK = re.compile(r"[\u3400-\u9fff]")
LATIN = re.compile(r"[A-Za-z]{2,}")


def _clean_tag(tag, line):
    """One short tag; a code-switch claim is only kept if the chosen line really mixes Chinese and English."""
    t = " ".join((tag or "").replace('"', "").split(",")[0].split()[:5])[:40]
    if "code-switch" in t.lower() and not (CJK.search(line) and LATIN.search(line)):
        t = "funny line"
    return t or None


def _prompt(batch, examples):
    parts = ["Her lines the parent LOVED enough to publish: " + " | ".join(f'"{e}"' for e in examples["loved"]),
             "Her lines on cards the parent PASSED on: " + " | ".join(f'"{e}"' for e in examples["passed"]), ""]
    for m, lines in batch:
        parts.append(f"CARD {m['id']}:")
        for l in lines[:16]:
            who = "HER" if l["spk"] == "kid" else "ADULT"
            parts.append(f"  [{l['id']}] {who}: {l['t'][:170]}")
    return "\n".join(parts)


VERSION = 2   # v1 = best line + tag; v2 adds quote_score and taste examples, and runs on decided cards leak-free for backtesting
DECIDED = "m.status IN ('journal','ready','published','log','rejected')"


def cards_for(scope="pending", redo=False):
    """scope: pending | decided | both (decided first, so a backtest can start early). Cards already at VERSION are skipped unless redo."""
    from .rank import order_sql
    todo = "" if redo else f" AND (m.punch_v IS NULL OR m.punch_v < {VERSION})"
    out = []
    if scope in ("decided", "both"):
        out += db.rows(f"SELECT m.* FROM moments m WHERE {DECIDED} AND m.id NOT IN ({db.SWEPT_SQL}){todo} ORDER BY m.id")
    if scope in ("pending", "both"):
        order = order_sql() if db.ranked() else "coalesce(m.priority,1), m.score DESC"
        out += db.rows(f"SELECT m.* FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status='pending'{todo} ORDER BY {order}")
    return out


def run(limit=None, redo=False, batch=10, scope="pending", verbose=True):
    llm.ensure_ready()
    todo = cards_for(scope, redo)[: limit or None]
    ex = _examples()
    if verbose:
        print(f"  {len(todo)} cards to look at (scope {scope}), {batch} per call")
    t0, done, picked = time.time(), 0, 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        lf = lambda m: m["status"] != "pending"      # decided cards: machine guesses + raw text only
        data = [(m, _card_lines(m, lf(m))) for m in chunk]
        data = [(m, ls) for m, ls in data if ls]
        res = {}
        if data:
            try:
                out = llm.chat_json(SYSTEM, _prompt(data, ex), SCHEMA, max_tokens=1100)
                res = {c["id"]: c for c in out.get("cards", [])}
            except Exception as e:
                print(f"    batch failed ({e}); will retry next run")
                continue
        with db.tx() as c:
            for m in chunk:
                ls = {l["id"]: l for l in _card_lines(m, lf(m))}
                r = res.get(m["id"])
                if not r:
                    continue
                seg = tag = None
                if r["best_seg"] and r["best_seg"] in ls and ls[r["best_seg"]]["spk"] == "kid":   # must be one of HER real lines
                    seg = r["best_seg"]
                    tag = _clean_tag(r["tag"], ls[seg]["t"])
                c.execute("UPDATE moments SET punch_seg=?, punch_tag=?, punch_score=?, punch_v=?, punch_at=datetime('now','localtime') WHERE id=?",
                          (seg, tag, float(r["quote_score"]), VERSION, m["id"]))
                picked += seg is not None
        done += len(chunk)
        if verbose:
            rate = done / max(1, time.time() - t0)
            print(f"  {done}/{len(todo)} cards · {picked} with a punchline · ETA {int((len(todo) - done) / rate / 60)} min", flush=True)
    return {"looked_at": done, "with_punchline": picked}
