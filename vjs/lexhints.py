"""Recurring-word hints from the owner's own corrections (ground truth), shown as suggestions — never applied silently.

Problem (the owner 2026-10-25): one word she says many times ("Pirate Park") is mis-heard differently each time; the owner fixes it once
or twice, other times Whisper was already right. Past corrections X→Y are the best evidence of what she actually says, so
they are propagated as *flagged suggestions* onto other lines that contain the same mis-hearing.

Gate (same discipline as rank.py): `backtest()` replays the owner's logged text edits in time order, using only EARLIER edits as the
lexicon, and measures how often a suggestion for a line matched what he then did. Suggestions are only shown for pairs whose
support clears the gate (min_support, and min precision overall); nothing here rewrites a transcript.
"""
import difflib
import re
from collections import defaultdict

from . import db
from .edits import TOK, MACHINE_VIA

# speaker/determiner confusions are not word hearing errors — never propagate these
SKIP_WRONG = {"the", "a", "an", "yeah", "that", "there", "him", "her", "get", "time", "going to", "high"}


def _pairs(before, after):
    a, b = TOK.findall(before or ""), TOK.findall(after or "")
    sm = difflib.SequenceMatcher(None, [x.lower() for x in a], [x.lower() for x in b])
    out = []
    join = lambda toks: "".join(toks) if all(re.match(r"[㐀-鿿]", t) for t in toks) else " ".join(toks)
    for op, i1, i2, j1, j2 in sm.get_opcodes():
        if op == "replace" and 1 <= i2 - i1 <= 3 and 1 <= j2 - j1 <= 3:
            w, r = join(a[i1:i2]), join(b[j1:j2])
            if w.lower() != r.lower() and w.lower() not in SKIP_WRONG and not r.startswith("-"):
                out.append((w.lower(), r))
    return out


def _has(text, wrong):
    return re.search(r"(?<![A-Za-z0-9'])" + re.escape(wrong) + r"(?![A-Za-z0-9'])", (text or "").lower()) is not None


def _edits():
    ks = db.rows("SELECT id, seg_id, before, after FROM edits WHERE kind='text' AND seg_id IS NOT NULL AND coalesce(via,'') NOT IN (%s) ORDER BY id"
                 % ",".join("?" * len(MACHINE_VIA)), *MACHINE_VIA)
    return ks


def backtest(min_support=2):
    """Replay edits in order. For each edit, would a hint built from earlier edits have proposed what the owner did?"""
    support = defaultdict(set)       # (wrong,right) -> {seg_ids that earlier edits fixed this way}
    proposed = hit = covered = total_pairs = 0
    for e in _edits():
        actual = set(_pairs(e["before"], e["after"]))
        total_pairs += len(actual)
        for (w, r), segs in support.items():
            if len(segs) >= min_support and _has(e["before"], w):
                proposed += 1
                if (w, r) in actual:
                    hit += 1
        for pr in actual:
            if len(support.get(pr, ())) >= min_support:
                covered += 1
        for pr in actual:
            support[pr].add(e["seg_id"])
    prec = hit / proposed if proposed else None
    return {"min_support": min_support, "edits": len(_edits()), "pairs_fixed": total_pairs, "suggestions_made": proposed, "suggestions_matched_owner": hit,
            "precision": None if prec is None else round(prec, 3), "pairs_predictable": covered, "recall": round(covered / total_pairs, 3) if total_pairs else None}


def build(min_support=2, apply=False):
    """Suggestions on lines the owner has NOT edited: [(seg_id, wrong, right, support)]. Stored in lex_hints (flagged, dismissable)."""
    support = defaultdict(set)
    for e in _edits():
        for pr in _pairs(e["before"], e["after"]):
            support[pr].add(e["seg_id"])
    good = {pr: len(s) for pr, s in support.items() if len(s) >= min_support}
    out = []
    if good:
        rows = db.rows("SELECT id, text FROM segments WHERE text_final IS NULL AND (flags IS NULL OR flags='') AND length(text)>0")
        for r in rows:
            for (w, rt), n in good.items():
                if _has(r["text"], w):
                    out.append((r["id"], w, rt, n))
    if apply:
        with db.tx() as c:
            c.execute("CREATE TABLE IF NOT EXISTS lex_hints(seg_id INTEGER, wrong TEXT, right TEXT, support INTEGER, status TEXT DEFAULT 'suggested', at TEXT DEFAULT (datetime('now','localtime')), PRIMARY KEY(seg_id, wrong, right))")
            for sid, w, rt, n in out:
                c.execute("INSERT OR IGNORE INTO lex_hints(seg_id, wrong, right, support) VALUES(?,?,?,?)", (sid, w, rt, n))
    return out, good
