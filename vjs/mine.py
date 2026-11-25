"""Nominate journal-worthy moments from a transcribed recording.

The model only *points at* segment ranges and says why (the reason is shown to
you in review and never published). All published text comes from the
transcript as you corrected it.
"""
import json
import random
import re
from pathlib import Path

from . import db
from .config import CFG
from .llm import chat_json

TAGS = ["funny", "code-switch", "grammar-slip", "new-word", "sweet", "milestone",
        "conversation", "question", "pretend-play", "song", "reasoning"]

SYSTEM = f"""You help a parent find memorable moments of their bilingual (Mandarin/English) preschool daughter in long home recordings.
You get numbered transcript lines. Each has a speaker GUESS from voice pitch (KID, MOM, DAD, OTHER; "?" = unsure). Pitch guesses are often
wrong (excited adults and TV voices read as KID), so judge from the words themselves.

Step 1 — "context" of this stretch of audio:
  family = the child and/or parents talking together at home, in the car, at bedtime, playing
  work   = work calls/meetings, creator/business/AI-chatbot/tech talk, a parent on the phone or dictating
  media  = mostly TV, YouTube, news, music, or an audiobook/story playing in the background
  mixed  = a real mix

Step 2 — "media_lines": ids of lines that are NOT live household speech: TV/cartoon dialogue, YouTube, news anchors
("白宫发言人说…"), song lyrics from a recording, and especially the Chinese children's story audiobook that plays at night —
long polished narration like "翼龙飞飞说：…", "从前，有一只…", "小手洗干净，天气真热呀" — and English storybook narration.
Read-aloud by a parent from a book is also not her speech (it may be kept as context, never as the quote).

Step 3 — "moments": contiguous line ranges [from,to] that contain HER OWN spontaneous speech plus just enough adjacent
lines (e.g. the question she answered) to make sense.
Scoring (be stingy — most batches have no moment above 6):
  10 = would be one of the best quotes of the year          9 = clearly quotable: funny logic, an inventive phrase, a sweet line, a real first
  7-8 = charming but ordinary kid talk                      6 = only worth logging, not the journal
Examples of great: "This is 晒太阳-ing." · "I think we don't need some popsicles. Our home has these." · "妈妈我没有不知道，我只是forgot了。"
Never nominate: work talk, adult-only conversation, media_lines, a parent reading aloud, routine commands, one-word answers, crying, garbled lines.
Tags: {", ".join(TAGS)}.
"why" (<=15 words) says what is notable in her words. Never guess at backstory, feelings or relationships.
At most 5 moments per batch."""

SCHEMA = {
    "type": "object",
    "properties": {
        "context": {"type": "string", "enum": ["family", "work", "media", "mixed"]},
        "media_lines": {"type": "array", "items": {"type": "integer"}},
        "moments": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "from": {"type": "integer"}, "to": {"type": "integer"},
                "score": {"type": "integer"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "why": {"type": "string"},
            },
            "required": ["from", "to", "score", "tags", "why"],
        }},
    },
    "required": ["context", "media_lines", "moments"],
}

PRIORITY = {"family": 0, "mixed": 1, "work": 2, "media": 2}

VERIFY_SYSTEM = """You double-check candidate journal moments for a parent. The speaker labels came from voice pitch and are often
wrong: an excited adult woman, a TV voice or an audiobook narrator often gets labeled KID.
For each numbered candidate decide:
  source: "live_kid"  = a 3-5-year-old child is really speaking, live, in the room (words, grammar and topic fit a preschooler)
          "adult"     = the KID lines are really an adult (work, money, relationships, adult vocabulary, fluent complex sentences)
          "media"     = TV, YouTube, news, song, or a story/audiobook narration (polished narrative prose, quoted characters)
          "read_aloud"= someone reading a book aloud
  score 1-10 for a family journal, stingy: 10 best-of-year, 9 clearly quotable, 7-8 charming but ordinary, <=6 not journal-worthy.
Judge from the words: short, simple, kid-grammar lines ("No, I do it", "Beef is 牛肉", "Now it's mine!") are live_kid even if an
adult is also talking. Only say adult / media / read_aloud when the words clearly show it."""

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {"id": {"type": "integer"},
                       "source": {"type": "string", "enum": ["live_kid", "adult", "media", "read_aloud"]},
                       "score": {"type": "integer"}},
        "required": ["id", "source", "score"]}}},
    "required": ["results"],
}


def verify(cands, by_idx):
    """Second, skeptical pass: drop candidates that aren't the child speaking live; re-score the rest."""
    out = []
    for i in range(0, len(cands), 12):
        chunk = cands[i:i + 12]
        user = "\n\n".join(
            f"[{k}]\n" + "\n".join(_line(by_idx[j]) for j in range(a, b + 1) if j in by_idx)
            for k, (a, b, *_rest) in enumerate(chunk))
        user += f"\n\nReturn exactly {len(chunk)} results, ids 0..{len(chunk) - 1}."
        try:
            res = {r["id"]: r for r in chat_json(VERIFY_SYSTEM, user, VERIFY_SCHEMA, max_tokens=1500)["results"]}
        except Exception as e:
            print(f"    verify failed ({e}); keeping candidates unverified")
            out += chunk
            continue
        for k, c in enumerate(chunk):
            r = res.get(k)
            if r is None:
                out.append(c)
            elif r["source"] == "live_kid":
                out.append((c[0], c[1], min(c[2], int(r["score"])), *c[3:]))
    return out


def _journal_examples(n=14):
    """Short quotes from the published journal = what this parent actually keeps."""
    root = Path(CFG["journal_repo"]) / "src" / "content" / "entries"
    quotes = []
    for f in root.glob("*/*.md"):
        body = f.read_text(encoding="utf-8").split("---", 2)[-1]
        for line in body.splitlines():
            line = re.sub(r"<!--.*?-->", "", line).strip()
            if line.startswith(('"', "👧", "⭐")) and 12 <= len(line) <= 160:
                quotes.append(re.sub(r"\s*\*\[.*?\]\*", "", line))
    random.Random(len(quotes)).shuffle(quotes)
    return quotes[:n]


def _review_examples(status_sql, n):
    out = []
    for m in db.rows(f"SELECT rec_id, seg_from, seg_to FROM moments WHERE {status_sql} ORDER BY coalesce(triaged_at,'') DESC LIMIT ?", n):
        segs = db.rows("SELECT coalesce(spk, spk_guess) spk, coalesce(text_final, text) t FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? ORDER BY idx",
                       m["rec_id"], m["seg_from"], m["seg_to"])
        out.append(" / ".join(f"{SPK.get(s['spk'], 'OTHER')}: {s['t']}" for s in segs)[:220])
    return out


def system_prompt():
    kept = _review_examples("status IN ('journal','ready','published')", 8) + _journal_examples(14 if not db.one("SELECT 1 FROM moments WHERE status IN ('journal','ready','published') LIMIT 1") else 8)
    rejected = _review_examples("status='rejected'", 8)
    s = SYSTEM + "\n\nExamples of quotes this parent KEPT (match this taste; score similar moments 8-10):\n" + "\n".join(f"- {q}" for q in kept)
    if rejected:
        s += "\n\nExamples this parent REJECTED (score similar moments <=5):\n" + "\n".join(f"- {q}" for q in rejected)
    return s


SPK = {"kid": "KID", "mom": "MOM", "dad": "DAD", "other": "OTHER"}


def _line(s):
    spk = s["spk"] or s["spk_guess"] or "other"
    unsure = "" if s["spk"] or (s["spk_conf"] or 0) >= 0.6 else "?"
    return f"[{s['idx']}] ({SPK.get(spk, 'OTHER')}{unsure}) {s['text_final'] or s['text']}"


def mine_recording(rec_id, min_score=6, verbose=True):
    from .rules import no_kid
    r = db.one("SELECT recorded_at, title FROM recordings WHERE id=?", rec_id)
    if r and no_kid(r):
        return 0  # the owner: the child can't be in this recording
    from .media import add_flag
    # hard noise (repeat/foreign/halluc/loop) is hidden; the model's own earlier "media" guesses are not —
    # it re-judges every run, so one bad guess can't permanently hide real speech
    segs = [s for s in db.rows("SELECT * FROM segments WHERE rec_id=? ORDER BY idx", rec_id)
            if not (set((s["flags"] or "").split(",")) - {"", "media"})]
    if not segs:
        return 0
    by_idx = {s["idx"]: s for s in segs}
    n, ov = CFG["llm"]["window_lines"], CFG["llm"]["window_overlap"]
    found, contexts, media = [], [], set()
    system = system_prompt()
    i = 0
    while i < len(segs):
        win = segs[i:i + n]
        # Skip windows with no likely-kid speech at all — saves most of the LLM time on adult talk.
        if any((s["spk"] or s["spk_guess"]) == "kid" for s in win):
            valid = {s["idx"] for s in win}
            try:
                res = chat_json(system, "\n".join(_line(s) for s in win), SCHEMA)
            except Exception as e:
                print(f"    llm window failed: {e}")
                res = {"context": "mixed", "media_lines": [], "moments": []}
            ctx = res.get("context", "mixed")
            contexts.append((ctx, len(win)))
            media |= {int(x) for x in res.get("media_lines", []) if int(x) in valid}
            for m in res.get("moments", []):
                a, b = sorted((int(m["from"]), int(m["to"])))
                if a not in valid or b not in valid or b - a > 14:
                    continue
                sc = int(m["score"])
                if sc < min_score:
                    continue
                found.append((a, b, sc, [t for t in m["tags"] if t in TAGS], m["why"][:200], ctx))
        i += n - ov

    # recording category = where most of its (kid-bearing) audio falls
    weight = {}
    for ctx, w in contexts:
        weight[ctx] = weight.get(ctx, 0) + w
    category = max(weight, key=weight.get) if weight else "quiet"

    # a moment must still contain at least one live (non-media) kid line
    def live_kid(a, b):
        return any(by_idx.get(k) and k not in media and (by_idx[k]["spk"] or by_idx[k]["spk_guess"]) == "kid"
                   for k in range(a, b + 1))

    found = [f for f in found if live_kid(f[0], f[1])]
    found.sort()
    merged = []
    for a, b, sc, tags, why, ctx in found:
        if merged and a <= merged[-1][1]:
            pa, pb, psc, ptags, pwhy, pctx = merged[-1]
            merged[-1] = (pa, max(pb, b), max(psc, sc), sorted(set(ptags) | set(tags)),
                          pwhy if psc >= sc else why, pctx if PRIORITY.get(pctx, 1) <= PRIORITY.get(ctx, 1) else ctx)
        else:
            merged.append((a, b, sc, tags, why, ctx))
    n_cand = len(merged)
    merged = [m for m in verify(merged, by_idx) if m[2] >= min_score]
    # never duplicate something you've already triaged
    taken = [(r["seg_from"], r["seg_to"]) for r in db.rows(
        "SELECT seg_from, seg_to FROM moments WHERE rec_id=? AND status!='pending'", rec_id)]
    merged = [m for m in merged if not any(m[0] <= b and a <= m[1] for a, b in taken)]
    kept = {k for a, b, *_ in merged for k in range(a, b + 1)}
    with db.tx() as c:
        c.execute("UPDATE segments SET flags=trim(replace(replace(','||flags||',', ',media,', ','), ',,', ','), ',') "
                  "WHERE rec_id=? AND flags LIKE '%media%'", (rec_id,))
        for k in media - kept:  # 'media' only keeps a line out of the language log
            s = db.one("SELECT flags FROM segments WHERE id=?", by_idx[k]["id"])
            add_flag(c, by_idx[k]["id"], s["flags"], "media")
        c.execute("UPDATE recordings SET category=? WHERE id=?", (category, rec_id))
        for a, b, sc, tags, why, ctx in merged:
            # work always sinks; a verified live moment from a TV/audiobook stretch is still family, just less certain
            pr = 2 if "work" in (category, ctx) else (0 if ctx == "family" else 1)
            c.execute(
                "INSERT OR IGNORE INTO moments(rec_id, seg_from, seg_to, score, tags, why, priority) VALUES(?,?,?,?,?,?,?)",
                (rec_id, a, b, sc, json.dumps(tags), why, pr),
            )
    if verbose:
        print(f"    {category}: {n_cand} candidate(s) → {len(merged)} verified moment(s); {len(media)} media line(s) flagged")
    return len(merged)
