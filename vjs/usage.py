"""Language-log usage checks: flag non-adult-standard forms in the kid's speech
(e.g. "didn't went", "where are me", wrong measure word). Flags start as
'suggested'; you confirm or dismiss them in review, and the stats page can show
either all flags or confirmed-only."""
from . import db
from .llm import chat_json

EN_KINDS = ["past-tense", "agreement", "pronoun", "plural", "negation", "word-order", "article",
            "preposition", "comparative", "other-grammar"]
ZH_KINDS = ["measure-word", "aspect-particle", "word-order", "negation", "other-grammar"]

SYSTEM = f"""You annotate a bilingual (English/Mandarin) preschooler's utterances for a language-development log.
Return one result for EVERY numbered utterance (same id). Flag ONLY grammatical or morphological errors — forms an adult native speaker would never produce.
English kinds: {", ".join(EN_KINDS)}. Mandarin kinds: {", ".join(ZH_KINDS)}.
"span" = the exact words as said, "correct" = the single adult form of that span (no alternatives, no slashes).

Flag examples:
- "They wasn't there." → agreement: "They wasn't" → "They weren't"
- "I didn't went to the beach." → past-tense: "didn't went" → "didn't go"
- "Daddy, where are me?" → pronoun: "where are me" → "where am I"
- "My 屁股 don't hurt." → agreement: "don't" → "doesn't"
- "Why is the vampire not a people?" → plural: "a people" → "a person"
- "我要一个书" → measure-word: "一个书" → "一本书"

Do NOT flag (give that utterance "errors": []): punctuation, capitalisation, apostrophes or spacing (the transcript is machine-made, those are artifacts), run-on or
cut-off sentences, missing words that look like speech-recognition dropouts, English words used inside Mandarin or Mandarin words inside English (code-switching —
never a "measure word" or "grammar" error), maths or facts that are simply wrong, vocabulary or style preferences ("awkward", "more natural", casual or colloquial speech),
anything you would hedge on ("likely", "may", "context suggests"). Only flag a clear, specific morphological or grammatical error in a short span (about 1–6 words). Also skip: baby words and family words (poopoo, tummy, 臭臭, 抱抱), names of people, toys or characters,
informal but grammatical speech ("gonna", "wanna", "Yeah"), sentence fragments and one-word answers, sound effects,
code-switching itself ("This is 晒太阳-ing" is fine), questions without inversion in casual speech, or anything you are unsure about.
Transcripts come from speech recognition; if a line looks garbled, give it "errors": []. Most utterances have no errors, but every utterance still gets a result.

Speaker labels come from voice pitch and are sometimes wrong. Also set "child_like": false when the line is clearly an adult
talking (work or business topics, adult vocabulary, parenting instructions, adult conversation about money, schedules, etc.);
otherwise true."""

SCHEMA = {
    "type": "object",
    "properties": {"results": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "child_like": {"type": "boolean"},
            "errors": {"type": "array", "items": {
                "type": "object",
                "properties": {
                    "lang": {"type": "string", "enum": ["en", "zh"]},
                    "kind": {"type": "string"},
                    "span": {"type": "string"},
                    "correct": {"type": "string"},
                    "explanation": {"type": "string"},
                },
                "required": ["lang", "kind", "span", "correct", "explanation"],
            }},
        },
        "required": ["id", "child_like", "errors"],
    }}},
    "required": ["results"],
}


def log_segments_sql():
    """Kid segments that belong in the language log (auto or verified)."""
    from .config import CFG
    return (
        "SELECT s.* FROM segments s WHERE (s.flags IS NULL OR s.flags='') AND ("
        " s.spk='kid' OR (s.spk IS NULL AND s.spk_guess='kid' AND coalesce(s.adult_like,0)=0 AND s.avg_logprob >= %f"
        "  AND s.rec_id NOT IN (SELECT id FROM recordings WHERE category IN ('work','media'))))"
        % CFG["log_auto_min_logprob"]
    )


def check_pending(limit=None, batch=20, verbose=True):
    todo = db.rows(
        log_segments_sql() + " AND s.id NOT IN (SELECT seg_id FROM usage_checked) "
        "AND length(coalesce(s.text_final, s.text)) >= 4 ORDER BY s.rec_id DESC, s.idx"
        + (f" LIMIT {int(limit)}" if limit else "")
    )
    done = 0
    for i in range(0, len(todo), batch):
        chunk = todo[i:i + batch]
        user = "\n".join(f"[{s['id']}] {s['text_final'] or s['text']}" for s in chunk)
        user += f"\n\nReturn exactly {len(chunk)} results, one per id: {[s['id'] for s in chunk]}"
        try:
            res = chat_json(SYSTEM, user, SCHEMA, max_tokens=3000)
        except Exception as e:
            print(f"    usage batch failed: {e}")
            continue
        ids = {s["id"] for s in chunk}
        answered = set()
        with db.tx() as c:
            for r in res.get("results", []):
                if r["id"] not in ids:
                    continue
                answered.add(r["id"])
                c.execute("UPDATE segments SET adult_like=? WHERE id=?", (0 if r.get("child_like", True) else 1, r["id"]))
                if not r.get("child_like", True):
                    continue
                for e in r["errors"]:
                    span, corr = e["span"].strip(), e["correct"].strip()
                    # drop hedged / non-answers: "poop / poopie", "none", or "fixes" identical to the span
                    if not span or not corr or "/" in corr or corr.lower() == span.lower() or len(corr) > 3 * len(span) + 12:
                        continue
                    if e["kind"] not in EN_KINDS + ZH_KINDS:
                        continue
                    from .usage_filter import reason, relabel
                    if reason(e["kind"], e["lang"], span, corr, e.get("explanation")):
                        continue                                    # screened on arrival (vjs/usage_filter.py)
                    e["kind"] = relabel(e["kind"], e["lang"], span)
                    c.execute(
                        "INSERT OR IGNORE INTO usage_flags(seg_id, lang, kind, span, correct, explanation) VALUES(?,?,?,?,?,?)",
                        (r["id"], e["lang"], e["kind"], e["span"], e["correct"], e["explanation"][:200]),
                    )
            # only answered lines count as checked; skipped ones are retried next run
            c.executemany("INSERT OR IGNORE INTO usage_checked(seg_id) VALUES(?)", [(s,) for s in answered])
        done += len(chunk)
        if verbose:
            print(f"    usage: {done}/{len(todo)}")
    return done
