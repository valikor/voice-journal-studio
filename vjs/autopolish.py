"""Auto-polish: do the mechanical part of Polish for the owner, so a card arrives journal-ready and he only
judges it. Learned from his own Polish edits (edits table): punctuation and energy on whisper's run-ons
("--" for interruptions, "..." for pauses, "!" for excitement, quotes around invented words), trimming
false starts and trailing fragments, merging a thought whisper split in two, dropping lines that don't
serve the story, fixing speakers, and — never — correcting her grammar ("Mommy say that" stays).

Hard guard: the editor may punctuate, re-case, cut and merge, but may not ADD words unless the second
listen heard them. Anything else is rejected line by line and left as it was.

dry_run() writes proposals only; nothing touches moment_lines until apply() (not wired to the UI yet)."""
import json
import re
from datetime import datetime

from . import db
from .config import DATA
from .llm import chat_json
from .textutil import CJK, speech_only

SYSTEM = """You are the copy editor for a parent's private family journal of their bilingual (Mandarin/English) daughter, about 5.
Each moment is a short transcribed exchange (from speech recognition) that the parent already chose to keep.
Make it read like a great journal quote — true to what was said, and landing on her line.

Allowed:
- punctuation and capitalization; "--" where someone is cut off, "..." for a trailing pause, "!" where the energy is clearly there
- put single quotes around a word she invents or plays with ('killy')
- trim stutters, doubled words and false starts from ADULT lines; for HER lines keep her exact words and her grammar
  (never fix "Mommy say that" or "I goed"), only remove an exact accidental repeat
- merge a line into the previous one when it is the same speaker finishing the same sentence
- drop ADULT lines that don't serve the story: filler, garbled fragments, asides, anything after her punchline
- never drop or shorten the child's lines (except an exact accidental repeat); they are why the parent kept this
- change a speaker when the words clearly belong to someone else (a question to "Dad" is not said by Dad)
Not allowed: adding or changing ANY word, paraphrasing, translating, adding sounds. You may only delete, punctuate, re-case, merge, re-assign.
The moment should keep its setup and END on the child's best line.
Also: "note" = a short context note ONLY if the conversation makes the situation obvious (else ""), "star" = true only for a standout moment."""

SCHEMA = {"type": "object", "properties": {
    "lines": {"type": "array", "items": {"type": "object", "properties": {
        "id": {"type": "integer"},
        "keep": {"type": "boolean"},
        "merge_into_previous": {"type": "boolean"},
        "speaker": {"type": "string", "enum": ["kid", "mom", "dad", "other"]},
        "text": {"type": "string"}},
        "required": ["id", "keep", "merge_into_previous", "speaker", "text"]}},
    "note": {"type": "string"}, "star": {"type": "boolean"}, "comment": {"type": "string"}},
    "required": ["lines", "note", "star", "comment"]}

WORD = re.compile(r"[㐀-鿿]|[A-Za-z0-9]+(?:'[A-Za-z]+)?")


def _words(t):
    return [w.lower() for w in WORD.findall(speech_only(t or ""))]


def _not_subsequence(new, old):
    """Words in `new` that aren't an in-order subsequence of `old` (deleting is fine; anything else isn't)."""
    it, bad = iter(old), []
    for w in new:
        for o in it:
            if o == w:
                break
        else:
            bad.append(w)
            it = iter(())
    return bad


def _dup_of_prev(l, out):
    return bool(out) and _words(out[-1]["before"]) == _words(l["text"])


def _examples(n=10):
    """the owner's own Polish before → after pairs (first 'before', last 'after' per line)."""
    first, last = {}, {}
    for e in db.rows("SELECT seg_id, moment_id, before, after FROM edits WHERE kind='rendered' AND via='polish' ORDER BY id"):
        k = (e["moment_id"], e["seg_id"])
        first.setdefault(k, e["before"])
        last[k] = e["after"]
    out = []
    for k, b in first.items():
        a = last[k]
        if b and a and b != a and "{" not in a and "\n" not in a and len(a) < 160 and not CJK.search(a):
            out.append(f"{b}  →  {a}")
    return out[-n:]


def _quoted(why):
    """Phrases the moment-picker quoted in its note — its idea of the punchline."""
    return [q for q in re.findall(r"(?<![A-Za-z])'([^']{3,60})'(?![A-Za-z])|\"([^\"]{3,60})\"", why or "") for q in q if q]


def _find_missing(m, lines):
    """The picker's note quotes a line that isn't in the draft (e.g. 'My dad!'): look for it nearby."""
    have = " ".join(l["text"] or "" for l in lines).lower()
    missing, found = [], []
    for q in _quoted(m["why"]):
        qn = re.sub(r"[^\w㐀-鿿 ]", "", q.lower()).strip()
        if not qn or qn in re.sub(r"[^\w㐀-鿿 ]", "", have):
            continue
        missing.append(q)
        near = db.rows("SELECT id, idx, coalesce(text_final, text) t, coalesce(spk, spk_guess) spk FROM segments WHERE rec_id=? "
                       "AND idx BETWEEN ? AND ? AND idx NOT BETWEEN ? AND ? AND coalesce(flags,'') IN ('','media')",
                       m["rec_id"], m["seg_from"] - 12, m["seg_to"] + 12, m["seg_from"], m["seg_to"])
        for s in near:
            if qn in re.sub(r"[^\w㐀-鿿 ]", "", (s["t"] or "").lower()):
                found.append(dict(s))
    return missing, found


def propose(moment_id):
    m = db.one("SELECT * FROM moments WHERE id=?", moment_id)
    lines = db.rows("SELECT id, ord, seg_id, speaker, text, rendered, include, edited FROM moment_lines WHERE moment_id=? ORDER BY ord", moment_id)
    rl = json.loads(m["relisten"]) if m["relisten"] else {}
    alts = {int(k): (v.get("alt") or "") for k, v in (rl.get("lines") or {}).items()}
    missing, found = _find_missing(m, lines)
    ex = _examples()
    rows = []
    for l in lines:
        if l["edited"]:
            continue  # lines you already polished are yours
        alt = alts.get(l["seg_id"] or -1, "")
        rows.append(f"[{l['id']}] {l['speaker'].upper()}: {speech_only(l['text'])}")
    if not rows:
        return None
    try:
        from .notes import ensure as _note_ok
        m = dict(m); m["why"] = _note_ok(dict(m))  # never brief the editor with a note that contradicts the card
    except Exception:
        pass
    user = (f"Why the parent kept it: {m['why']}\n"
            + (f"The note quotes {missing} but that line is not in the draft"
               + (f"; it was found nearby: {[s['t'] for s in found]}" if found else " and was not found nearby") + ".\n" if missing else "")
            + ("\nHow this parent edits (before → after):\n" + "\n".join(f"- {e}" for e in ex) + "\n" if ex else "")
            + "\nDraft:\n" + "\n".join(rows)
            + f"\n\nReturn one entry per id, same order: {[l['id'] for l in lines if not l['edited']]}")
    res = chat_json(SYSTEM, user, SCHEMA, max_tokens=2500)
    by_id = {l["id"]: l for l in lines}
    out, rejected = [], []
    for r in res.get("lines", []):
        l = by_id.get(r["id"])
        if not l or l["edited"]:
            continue
        text = r["text"].strip()
        added = _not_subsequence(_words(text), _words(l["text"]))
        if added:  # the guard: deletions only — no new, changed or reordered words
            rejected.append({"id": l["id"], "tried": text, "added": added})
            text = l["text"]
        keep = r["keep"] and bool(_words(text))
        if l["speaker"] == "kid" and not keep and _words(l["text"]) and not _dup_of_prev(l, out):
            keep = True  # her lines are never cut by the editor
        if l["speaker"] == "kid" and len(_words(text)) < len(_words(l["text"])) - 1:
            rejected.append({"id": l["id"], "tried": text, "added": ["(shortened her line)"]}); text = l["text"]
        item = {"id": l["id"], "before": l["text"], "before_speaker": l["speaker"], "keep": keep,
                "merge": bool(r.get("merge_into_previous")), "speaker": r["speaker"], "text": text}
        out.append(item)
    return {"moment": moment_id, "why": m["why"], "missing": missing, "found_nearby": [s["t"] for s in found],
            "lines": out, "rejected": rejected, "note": res.get("note", ""), "star": bool(res.get("star")),
            "comment": res.get("comment", "")}


def _render(p):
    """Proposal → readable before/after."""
    emo = {"kid": "👧", "mom": "👩", "dad": "👨", "other": "🗣️"}
    before = "\n".join(f"    {emo.get(l['before_speaker'], '🗣️')} {l['before']}" for l in p["lines"])
    after, cur = [], None
    for l in p["lines"]:
        if not l["keep"]:
            continue
        if l["merge"] and after:
            after[-1] = after[-1].rstrip('"') + " " + l["text"] + '"'
            continue
        after.append(f'    {emo.get(l["speaker"], "🗣️")} "{l["text"]}"')
    if p["note"]:
        after[-1:] = [after[-1] + f" *[{p['note'].strip('[]*. ')}.]*"] if after else []
    if p["star"] and after:
        after[0] = "    ⭐ " + after[0].strip()
    s = f"### #{p['moment']} — {p['why']}\n\n**Before**\n```\n{before}\n```\n**After (proposed)**\n```\n" + "\n".join(after) + "\n```\n"
    if p["missing"]:
        s += f"- ⚠ Punchline quoted by the picker is missing from the draft: {p['missing']}" + (
            f" — found nearby: {p['found_nearby']} (would extend the moment)\n" if p["found_nearby"] else " — not in the transcript nearby (listen / insert)\n")
    if p["rejected"]:
        s += "- 🛡 Guard kept the original for: " + "; ".join(f"line {r['id']} (tried to add {r['added'][:4]})" for r in p["rejected"]) + "\n"
    if p["comment"]:
        s += f"- Editor's note: {p['comment']}\n"
    return s + "\n"


def dry_run(n=10, ids=None):
    ids = ids or [r["id"] for r in db.rows(
        "SELECT m.id FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status='journal' ORDER BY r.recorded_at DESC LIMIT ?", n)]
    props = []
    for i in ids:
        try:
            p = propose(i)
        except Exception as e:
            print(f"  #{i}: failed ({e})")
            continue
        if p:
            props.append(p)
            print(_render(p))
    out = DATA / "exports" / f"autopolish_dryrun_{datetime.now():%Y%m%d_%H%M}.md"
    out.write_text("# Auto-polish dry run (nothing applied)\n\n" + "".join(_render(p) for p in props), encoding="utf-8")
    (DATA / "exports" / (out.stem + ".json")).write_text(json.dumps(props, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"→ {out}")
    return props


# ---------------------------------------------------------------- Phase 1: safe clean-up (the owner, decision E, 2026-09-29)
# Applied automatically when a card enters Polish. Only mechanical fixes; never changes her words; every change is logged
# (edits.kind='autoclean') and undoable per card. Lines are hidden (include=0), never deleted.
PLACEHOLDER_SOUNDS = {"unintelligible", "inaudible", "unclear", "unsure", "?"}
_ELL_END = re.compile(r"(?:\s*\.){3}\s*$|\s*…\s*$")
_ELL_LEAD = re.compile(r"^\s*(?:\.\s*){3}|^\s*…\s*")
TERMINAL = tuple('.!?。！？…"\'”’-—~)*]')


def _is_placeholder(text):
    from .textutil import SOUND, _sound_text
    t = (text or "").strip()
    if re.search(r"[\w㐀-鿿]", speech_only(t)):
        return False
    sounds = [_sound_text(m).lower() for m in SOUND.finditer(t)]
    curly = [x.strip().lower() for x in re.findall(r"\{([^{}]*)\}?", t)]
    if not sounds and not curly:
        return True  # ".", "-", "…"
    return all(x in PLACEHOLDER_SOUNDS for x in sounds + curly)


def clean_text(t):
    t2 = (t or "").replace("✎", "")
    t2 = re.sub(r"\s*\.(?:\s*\.){2,}", "...", t2)            # "and . . ." → "and..."
    t2 = re.sub(r"^\s*-{1,2}\s+(?=[\w㐀-鿿])", "", t2)  # whisper's leading dialogue dash
    t2 = re.sub(r"\s{2,}", " ", t2).strip()
    if t2[:1].isascii() and t2[:1].isalpha() and t2[:1].islower() and not t2[1:2].isupper():  # but not iPad
        t2 = t2[0].upper() + t2[1:]
    t2 = re.sub(r"(?<![A-Za-z'])i(?=\s|'m|'ll|'ve|'d|[,.!?]|$)", "I", t2)          # "all i want" → "all I want"
    t2 = re.sub(r"([!?]\s+)([a-z])", lambda m: m.group(1) + m.group(2).upper(), t2)  # "Yum! what's" → "Yum! What's"
    return t2


def cleanup(moment_id):
    """Returns the list of changes made (for the 'what changed' strip)."""
    from .edits import log
    from .polish import emoji, translation_of
    from .textutil import is_mostly_chinese, render_line
    m = db.one("SELECT autoclean FROM moments WHERE id=?", moment_id)
    if not m or m["autoclean"] == -1:
        return []
    lines = [dict(l) for l in db.rows("SELECT * FROM moment_lines WHERE moment_id=? ORDER BY ord", moment_id)]
    orig = {l["id"]: (l["text"], l["include"]) for l in lines}
    changes = []
    for l in lines:  # 1. placeholder lines → hidden
        if not l["edited"] and l["include"] and _is_placeholder(l["text"]):
            l["include"] = 0
            changes.append(("hid", l["id"], l["text"]))
    live = [l for l in lines if l["include"]]
    segt = {r["id"]: r for r in db.rows("SELECT id, rec_id, start, end FROM segments WHERE id IN (%s)" % ",".join(str(l["seg_id"]) for l in lines if l["seg_id"]) )} if any(l["seg_id"] for l in lines) else {}

    def _contiguous(prev, cur):
        """No real pause between the two lines (hidden segments in between count as part of the same breath)."""
        a, b = segt.get(prev["seg_id"]), segt.get(cur["seg_id"])
        if not a or not b:
            return False
        rows = db.rows("SELECT start, end FROM segments WHERE rec_id=? AND start>=? AND end<=? ORDER BY start", a["rec_id"], a["start"], b["end"])
        return all(y["start"] - x["end"] <= 2.0 for x, y in zip(rows, rows[1:]))

    for prev, cur in zip(live, live[1:]):  # 2. one thought split in two (same speaker, no end punctuation, no real pause)
        # "...want them. . ." + ". . .to get one of them, okay?" — a trailing ellipsis answered by a leading one is a single thought
        ell = (not prev["edited"] and not cur["edited"] and prev["include"] and prev["speaker"] == cur["speaker"]
               and not CJK.search(prev["text"] + cur["text"]) and _ELL_END.search(prev["text"]) and _ELL_LEAD.match(cur["text"])
               and _contiguous(prev, cur))
        if ell:
            prev["text"] = _ELL_END.sub("", prev["text"].rstrip()).rstrip(" ,") + " " + _ELL_LEAD.sub("", cur["text"]).strip()
            cur["include"] = 0
            changes.append(("merged", cur["id"], cur["text"]))
            continue
        # the same audio decoded twice (two segments that overlap in time, e.g. "That is ready!" / "It's Friday."): keep the longer one
        a, b = segt.get(prev["seg_id"]), segt.get(cur["seg_id"])
        if (not prev["edited"] and not cur["edited"] and prev["speaker"] == cur["speaker"] and a and b and a["rec_id"] == b["rec_id"]
                and b["start"] < a["end"] - 0.5 and (b["end"] - b["start"]) <= (a["end"] - a["start"]) + 0.5
                and min(a["end"], b["end"]) - max(a["start"], b["start"]) >= 0.6 * min(a["end"] - a["start"], b["end"] - b["start"])
                and len(_words(prev["text"])) >= len(_words(cur["text"]))):
            cur["include"] = 0
            changes.append(("dup", cur["id"], cur["text"]))
            continue
        # a sound on its own line (*ding ding ding*) belongs to the same speaker's line before it
        if (not prev["edited"] and not cur["edited"] and prev["include"] and prev["speaker"] == cur["speaker"]
                and not re.search(r"[\w㐀-鿿]", speech_only(cur["text"])) and not _is_placeholder(cur["text"]) and _contiguous(prev, cur)):
            prev["text"] = prev["text"].rstrip() + " " + cur["text"].strip()
            cur["include"] = 0
            changes.append(("merged", cur["id"], cur["text"]))
            continue
        # a fragment with no end punctuation ("Goodbye") + the rest of her breath ("机器人!"), Chinese or English
        if (not prev["edited"] and not cur["edited"] and prev["include"] and prev["speaker"] == cur["speaker"]
                and (CJK.search(prev["text"] + cur["text"])) and not prev["text"].rstrip().endswith(TERMINAL)
                and _contiguous(prev, cur) and len(_words(prev["text"])) <= 6 and len(prev["text"]) <= 40
                and not (is_mostly_chinese(prev["text"]) and is_mostly_chinese(cur["text"]))):
            prev["text"] = prev["text"].rstrip() + " " + cur["text"].strip()
            cur["include"] = 0
            changes.append(("merged", cur["id"], cur["text"]))
            continue
        if (not prev["edited"] and not cur["edited"] and prev["include"] and prev["speaker"] == cur["speaker"]
                and not CJK.search(prev["text"] + cur["text"]) and not prev["text"].rstrip().endswith(TERMINAL)
                and (cur["text"].lstrip()[:1].islower() or _contiguous(prev, cur))):
            nxt = cur["text"].strip()
            if nxt[:1].isupper() and not nxt.startswith("I ") and not nxt.startswith("I'"):  # mid-sentence: "Happy" + "Birthday" -> "Happy birthday"
                nxt = nxt[0].lower() + nxt[1:]
            prev["text"] = prev["text"].rstrip() + " " + nxt
            cur["include"] = 0
            changes.append(("merged", cur["id"], cur["text"]))
    for l in lines:  # 3. punctuation / artefacts
        if not l["edited"] and l["include"]:
            t = clean_text(l["text"])
            if t != l["text"] and orig[l["id"]][0] == l["text"]:
                changes.append(("tidied", l["id"], l["text"]))
            l["text"] = t
    with db.tx() as c:
        for l in lines:
            ot, oi = orig[l["id"]]
            if (l["text"], l["include"]) == (ot, oi):
                continue
            tr = translation_of(l["rendered"]) if is_mostly_chinese(l["text"]) else ""
            rendered = render_line(emoji(l["speaker"]), l["text"], tr) if l["text"] != ot else l["rendered"]
            log(c, "autoclean", json.dumps({"line": l["id"], "text": ot, "include": oi, "rendered": l["rendered"]}, ensure_ascii=False),
                json.dumps({"text": l["text"], "include": l["include"]}, ensure_ascii=False), l["seg_id"], moment_id, "autoclean")
            c.execute("UPDATE moment_lines SET text=?, include=?, rendered=? WHERE id=?", (l["text"], l["include"], rendered, l["id"]))
        if changes:
            c.execute("UPDATE moments SET autoclean=1 WHERE id=?", (moment_id,))
    return changes


def undo_cleanup(moment_id):
    from .edits import log
    rows = db.rows("SELECT id, before FROM edits WHERE moment_id=? AND kind='autoclean' ORDER BY id DESC", moment_id)
    with db.tx() as c:
        for r in rows:  # newest first, so the oldest 'before' wins
            b = json.loads(r["before"])
            c.execute("UPDATE moment_lines SET text=?, include=?, rendered=? WHERE id=?", (b["text"], b["include"], b["rendered"], b["line"]))
        c.execute("UPDATE edits SET kind='autoclean-undone' WHERE moment_id=? AND kind='autoclean'", (moment_id,))
        c.execute("UPDATE moments SET autoclean=-1 WHERE id=?", (moment_id,))
        log(c, "autoclean-undo", str(len(rows)), "", None, moment_id, "polish")
    return len(rows)


# ---------------------------------------------------------------- Phase 2: "needs ears" flags (no edits)
def ears(moment_id):
    """What only listening can fix: the picker's quoted punchline missing from the draft, second-listen disputes,
    unsure speakers. found = nearby transcript lines that contain the missing quote (one click to widen)."""
    m = db.one("SELECT * FROM moments WHERE id=?", moment_id)
    lines = db.rows("SELECT l.text, l.include, l.seg_id, s.spk, s.spk_conf FROM moment_lines l LEFT JOIN segments s ON s.id=l.seg_id "
                    "WHERE l.moment_id=? ORDER BY l.ord", moment_id)
    missing, found = _find_missing(m, [l for l in lines if l["include"]])
    rl = json.loads(m["relisten"]) if m["relisten"] else {}
    disputed = sum(1 for v in (rl.get("lines") or {}).values() if v.get("verdict") == "disputed")
    unsure = sum(1 for l in lines if l["include"] and l["seg_id"] and not l["spk"] and (l["spk_conf"] or 0) < 0.6)
    widen = None
    if found:
        idx = [s["idx"] for s in found]
        widen = {"before": max(0, m["seg_from"] - min(idx)), "after": max(0, max(idx) - m["seg_to"])}
    return {"missing": missing, "found": [s["t"] for s in found], "widen": widen, "disputed": disputed, "unsure": unsure}
