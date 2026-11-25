"""AI notes ("why it was picked") that cannot disagree with the card.

Defect found 2026-10-25: notes were written once, from the first-pass transcript and the model's imagination, then shown
next to a card the owner had since edited. Result: notes that quote words nobody said, claim Mandarin over an English card,
attribute Dad's line to her, or "confidently declare" things. Whisper's logprob does not predict which cards are wrong,
so the fix is a verifier, not a confidence cut-off.

* A note is tied to a hash of the card's CURRENT text (final text, speakers, visible lines). Hash changed = stale.
* `verify()` checks a note against that text: every quoted phrase is on the card (fuzzy), phrases attributed to her are
  hers, language claims match the lines, no unsupported confidence adverbs, no rare content word the card lacks.
* A note that fails (or is stale and fails) is replaced by a neutral EXTRACTIVE description built only from the card.
  The original is kept in moments.why_orig, so nothing is lost and `notes --restore` undoes it.
* `regen()` (GPU time) asks the local model for a new note from the current text, then runs the same verifier.
"""
import difflib
import hashlib
import json
import re
from collections import Counter

from . import db

BANNED = re.compile(r"\b(confident(?:ly)?|boldly|proudly|firmly|expertly|masterfully|assertively|decisively|emphatically|authoritatively)\b", re.I)
CJK = re.compile(r"[㐀-鿿]")
LATIN = re.compile(r"[A-Za-z]{2,}")
STOP = set("""the a an and or but if of to in on at by for with from as is are was were be been being it its this that these those
she her hers he his him they them their there here then than so such not no yes very just also into onto over under about after
before while when where which who whom what why how can could would should will may might do does did done has have had""".split())
SPEECH = re.compile(r"\b(says?|said|declares?|asks?|asked|calls?|tells?|told|exclaims?|insists?|sings?|sang|answers?|replies|reply|announces?|states?|demands?|invites?|offers?)\b", re.I)
HER = re.compile(r"\b(she|her|the child|toddler|kid)\b", re.I)


def norm(t):
    return re.sub(r"[^\w㐀-鿿]+", " ", (t or "").lower()).strip()


def card_lines(m):
    """The card's own lines as they stand now: [{'speaker','text'}] — hidden/junk lines excluded, final text preferred."""
    rows = db.rows("SELECT id, coalesce(spk, spk_guess, 'other') spk, coalesce(text_final, text) t, flags FROM segments "
                   "WHERE rec_id=? AND idx BETWEEN ? AND ? AND (flags IS NULL OR flags='' OR flags='media') ORDER BY idx",
                   m["rec_id"], m["seg_from"], m["seg_to"])
    drafted = db.rows("SELECT seg_id, include FROM moment_lines WHERE moment_id=?", m["id"])
    off = {d["seg_id"] for d in drafted if not d["include"]}
    return [{"speaker": r["spk"], "text": (r["t"] or "").strip()} for r in rows if r["id"] not in off and (r["t"] or "").strip()]


def text_hash(lines):
    return hashlib.sha1("\n".join(f"{l['speaker']}:{norm(l['text'])}" for l in lines).encode()).hexdigest()[:16]


def _quotes(note):
    out = []
    for m in re.finditer(r"(?<![\w])['‘“\"](.+?)['’”\"](?![\w])", note):
        q = m.group(1).strip()
        if len(norm(q)) >= 3:
            out.append((m.start(), q))
    return out


def _match(q, lines):
    """Best fuzzy match of quote q against each line → (ratio, speaker) of the best line."""
    nq = norm(q)
    best = (0.0, None)
    for l in lines:
        nl = norm(l["text"])
        if not nl:
            continue
        if nq in nl or nl in nq and len(nl) >= 6:
            return (1.0, l["speaker"])
        r = difflib.SequenceMatcher(None, nq, nl).find_longest_match(0, len(nq), 0, len(nl)).size / max(1, len(nq))
        if r > best[0]:
            best = (r, l["speaker"])
    joined = " ".join(norm(l["text"]) for l in lines)
    if nq in joined:  # a quote that spans two lines
        return (1.0, best[1])
    qt = nq.split()   # repeated/dropped filler words ("don't, don't look at me") still count: coverage of the quote's words by the card's
    if qt:
        have = set(joined.split())
        cov = sum(t in have for t in qt) / len(qt)
        if cov >= 0.85:
            return (max(best[0], 0.9), best[1] or "kid")
    return best


_GENERIC = None


def generic_vocab():
    """Words that appear in many notes are describing-words ("natural", "play", "imaginative"); rare words are claims about content."""
    global _GENERIC
    if _GENERIC is None:
        c = Counter()
        for r in db.rows("SELECT why FROM moments WHERE why IS NOT NULL AND why<>''"):
            c.update(set(re.findall(r"[a-z]{4,}", (r["why"] or "").lower())))
        _GENERIC = {w for w, n in c.items() if n >= 25}
    return _GENERIC


def verify(note, lines, generic=None, detail=False):
    """→ (ok, [hard reasons]); with detail=True → (ok, hard, soft). hard = contradicts the card (replace the note);
    soft = interpretive wording the card doesn't show (trim the interpretation, keep the facts). `lines` = card_lines(m)."""
    why, soft = [], []
    if not note or not note.strip():
        return (False, ["empty"], []) if detail else (False, ["empty"])
    if BANNED.search(note):
        why.append("unsupported confidence adverb: " + BANNED.search(note).group(0))
    card = " ".join(l["text"] for l in lines)
    her = " ".join(l["text"] for l in lines if l["speaker"] == "kid")
    has_zh, has_en = bool(CJK.search(card)), bool(LATIN.search(card))
    qs = _quotes(note)
    # 1) quotes must be on the card; 2) ones attributed to her must be hers
    for pos, q in qs:
        r, spk = _match(q, lines)
        if r < 0.8:
            if LATIN.search(q) and not CJK.search(q) and has_zh:  # an English rendering of her Chinese: a translation, not a quote
                soft.append(f"translation not checkable: {q[:40]}")
            else:
                why.append(f"quote not on the card: {q[:40]}")
            continue
        before = note[max(0, pos - 80):pos]
        if HER.search(before) and SPEECH.search(before) and spk and spk != "kid":
            kid_r = max([_match(q, [l])[0] for l in lines if l["speaker"] == "kid"] or [0])
            if kid_r < 0.7:
                why.append(f"attributed to her but spoken by {spk}: {q[:40]}")
    # 3) language claims
    for sent in re.split(r"(?<=[.;!?])\s+|\s+[—–]\s+", note):
        claims_zh = re.search(r"\b(Mandarin|Chinese)\b", sent, re.I)
        claims_en = re.search(r"\bin English\b|\bswitches? to English\b|\bEnglish\b", sent, re.I)
        sq = [q for _, q in _quotes(sent)]
        if claims_zh:
            if sq and not any(CJK.search(q) for q in sq) and re.search(r"\bin (Mandarin|Chinese)\b", sent, re.I):
                why.append("says Mandarin/Chinese but the quoted words are English")
            elif not has_zh and not sq:
                why.append("mentions Mandarin/Chinese but the card has no Chinese")
        if claims_en and sq and all(CJK.search(q) and not LATIN.search(q) for q in sq) and re.search(r"\bin English\b", sent, re.I):
            why.append("says English but the quoted words are Chinese")
    # 4) rare content words in the note (outside quotes) must be on the card
    gen = generic if generic is not None else generic_vocab()
    body = re.sub(r"(?<![\w])['‘“\"].+?['’”\"](?![\w])", " ", note)
    cardw = set(re.findall(r"[a-z]{3,}", card.lower()))
    stems = {w[:5] for w in cardw}
    miss = []
    for w in set(re.findall(r"[a-z]{5,}", body.lower())):
        if w in STOP or w in gen or w in cardw or w[:5] in stems:
            continue
        miss.append(w)
    if len(miss) >= 2 or (miss and not qs):
        soft.append("content not on the card: " + ", ".join(sorted(miss)[:4]))
    return ((not why), why, soft) if detail else ((not why), why)


def extractive(m, lines):
    """Neutral description built only from the card: her best line, who else is speaking, language mix. Never interpretive."""
    her = [l for l in lines if l["speaker"] == "kid"]
    adults = sorted({l["speaker"] for l in lines if l["speaker"] != "kid"})
    best = None
    if m.get("punch_seg"):
        s = db.one("SELECT coalesce(text_final,text) t FROM segments WHERE id=?", m["punch_seg"])
        if s and any(norm(s["t"]) == norm(l["text"]) for l in her):
            best = s["t"].strip()
    if not best and her:
        best = max(her, key=lambda l: len(l["text"]))["text"]
    if not best:
        return "Mostly adult speech; her part is very short."
    best = best if len(best) <= 90 else best[:87].rsplit(" ", 1)[0] + "…"
    lang = "Chinese and English" if CJK.search(" ".join(l["text"] for l in her)) and LATIN.search(" ".join(l["text"] for l in her)) else ("Chinese" if CJK.search(best) else "English")
    who = (" with " + " & ".join(a.title() for a in adults)) if adults else ""
    return f"Her words: “{best}” ({lang}{who})."


def ensure(m):
    """Cheap, no model: keep the note only if it verifies against the card's current text. Returns the (possibly replaced) note."""
    lines = card_lines(m)
    h = text_hash(lines)
    if m.get("why_hash") == h and m.get("why_state") in ("ok", "extractive", "llm", "trimmed"):
        return m.get("why") or ""
    ok, why, soft = verify(m.get("why") or "", lines, detail=True)
    with db.tx() as c:
        if ok:
            note = m.get("why") or ""
            if soft:  # facts check out; drop the interpretive tail ("— a milestone in …") the card can't support
                head = re.split(r"\s+[—–]\s+|,\s+(?:showing|suggesting|indicating|reflecting|demonstrating)\b", note)[0].rstrip(" ,;:")
                if head and head != note.rstrip(".") and len(head.split()) >= 4 and verify(head, lines, detail=True)[0]:
                    c.execute("UPDATE moments SET why_orig=coalesce(why_orig, why), why=?, why_hash=?, why_state='trimmed' WHERE id=?", (head + ".", h, m["id"]))
                    m["why"] = head + "."
                    return m["why"]
            c.execute("UPDATE moments SET why_hash=?, why_state='ok' WHERE id=?", (h, m["id"]))
            return note
        new = extractive(m, lines)
        c.execute("UPDATE moments SET why_orig=coalesce(why_orig, why), why=?, why_hash=?, why_state='extractive' WHERE id=?", (new, h, m["id"]))
        c.execute("INSERT INTO edits(kind, moment_id, before, after, via) VALUES('note_auto',?,?,?,?)", (m["id"], (m.get("why") or "")[:300], new, "; ".join(why)[:200]))
    m["why"] = new
    return new


def audit(statuses=("pending", "journal", "ready", "later"), limit=None):
    """Consistency rate of notes against current card text (no writes)."""
    rows = db.rows("SELECT * FROM moments WHERE status IN (%s) AND why IS NOT NULL AND why<>''" % ",".join("?" * len(statuses)), *statuses)
    if limit:
        rows = rows[:limit]
    gen, bad, reasons = generic_vocab(), [], Counter()
    for m in rows:
        orig = m.get("why_orig") or m["why"]
        ok, why, soft = verify(orig, card_lines(m), gen, detail=True)
        if not ok:
            bad.append(m["id"])
            for w in why:
                reasons[w.split(":")[0]] += 1
        elif soft:
            reasons["(soft) interpretation not on the card"] += 1
    return {"checked": len(rows), "inconsistent": len(bad), "rate_ok": round(1 - len(bad) / max(1, len(rows)), 3), "reasons": dict(reasons), "ids": bad}


def apply_all(statuses=("pending", "journal", "ready", "later", "log")):
    n = 0
    for m in db.rows("SELECT * FROM moments WHERE status IN (%s) AND why IS NOT NULL AND why<>''" % ",".join("?" * len(statuses)), *statuses):
        before = m["why"]
        if ensure(m) != before:
            n += 1
    return n


def restore():
    with db.tx() as c:
        c.execute("UPDATE moments SET why=why_orig, why_orig=NULL, why_state=NULL, why_hash=NULL WHERE why_orig IS NOT NULL")


def regen(limit=200, statuses=("pending", "journal"), verbose=True):
    """GPU: rewrite failed/extractive notes from the CURRENT card text with the local model, then verify. Falls back to extractive."""
    from . import llm
    llm.ensure_ready()
    schema = {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}
    sysm = ("You write one short factual note (max 20 words) about a toddler recording snippet for her parents. "
            "Use ONLY what is in the numbered lines. Quote her words exactly if you quote. Do not guess feelings, backstory or setting. "
            "Do not say which language she speaks unless a line's language tag says so. Never use confidence adverbs such as 'confidently' or 'boldly'. "
            "Lines marked KID are hers; DAD/MOM are adults — never attribute an adult line to her.")
    done = fixed = 0
    rows = db.rows("SELECT * FROM moments WHERE status IN (%s) AND why_state='extractive' ORDER BY score DESC LIMIT ?" % ",".join("?" * len(statuses)), *statuses, limit)
    for m in rows:
        lines = card_lines(m)
        user = "\n".join(f"{i+1}. {l['speaker'].upper()} [{'zh' if CJK.search(l['text']) and not LATIN.search(l['text']) else 'mixed' if CJK.search(l['text']) else 'en'}]: {l['text']}" for i, l in enumerate(lines))
        note = None
        for _ in range(2):
            try:
                out = llm.chat_json(sysm, user, schema, max_tokens=200, temperature=0.1)
                cand = (out.get("note") or "").strip()
            except Exception:
                break
            ok, _w = verify(cand, lines)
            if ok:
                note = cand
                break
        done += 1
        if note:
            fixed += 1
            with db.tx() as c:
                c.execute("UPDATE moments SET why=?, why_hash=?, why_state='llm' WHERE id=?", (note, text_hash(lines), m["id"]))
        if verbose and done % 25 == 0:
            print(f"  {done}/{len(rows)} · {fixed} regenerated and verified")
    return {"tried": done, "regenerated": fixed}
