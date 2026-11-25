"""Turn a triaged moment into editable journal lines: per-line alternatives
(forced-Mandarin / forced-English re-decodes of the same audio) plus
house-style rendering with pinyin and suggested translations."""
import json
import re

from . import db
from .asr import decode_clip
from .config import CFG
from .llm import chat_json
from .textutil import cjk_runs, is_mostly_chinese, lang_of, render_line

TR_SYSTEM = """You translate a family's Mandarin/English conversation for the parents' private journal about their preschool daughter.
Speakers: KID = the daughter (about 5), MOM, DAD. Only lines marked >> need a translation; the others are context.
For each >> line that is mostly Chinese: write the natural English a parent would actually say in that moment — not word-for-word.
Use the conversation to resolve who "you"/"I"/"it" is, omitted subjects and objects, and what the words mean here
(给你 while feeding = "feed you", 最后 = "at the end"). Keep the speaker's tone and length; keep English words as-is.
If an implied word is essential, put it in [square brackets], sparingly. Never add explanations.
For a >> line that is mostly English with some Chinese words, give the meaning of each listed Chinese word in this context
(抱抱 while being carried = "carry", not "hug").
First write "situation": one sentence on what is happening and who is doing what to whom. Then translate with that in mind."""

TR_SCHEMA = {
    "type": "object",
    "properties": {"situation": {"type": "string"}, "lines": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "translation": {"type": "string"},
            "glosses": {"type": "array", "items": {"type": "object", "properties": {
                "chinese": {"type": "string"}, "meaning": {"type": "string"}},
                "required": ["chinese", "meaning"]}},
        },
        "required": ["id", "translation", "glosses"],
    }}},
    "required": ["situation", "lines"],
}


def emoji(spk):
    return CFG["speakers"].get(spk or "other", CFG["speakers"]["other"])["emoji"]


def build_lines(moment_id, force=False):
    m = db.one("SELECT * FROM moments WHERE id=?", moment_id)
    if not m:
        return
    if db.one("SELECT id FROM moment_lines WHERE moment_id=?", moment_id) and not force:
        return
    segs = db.rows("SELECT * FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? "
                   "AND (flags IS NULL OR flags='' OR flags='media') ORDER BY idx", m["rec_id"], m["seg_from"], m["seg_to"])
    # lines you edited survive a rebuild (keyed by the transcript line they came from)
    keep = {l["seg_id"]: l for l in db.rows("SELECT * FROM moment_lines WHERE moment_id=? AND edited=1", moment_id)}
    with db.tx() as c:
        c.execute("DELETE FROM moment_lines WHERE moment_id=?", (moment_id,))
        for k, s in enumerate(segs):
            old = keep.get(s["id"])
            if old:
                c.execute("INSERT INTO moment_lines(moment_id, ord, seg_id, speaker, text, rendered, include, alts, edited) "
                          "VALUES(?,?,?,?,?,?,?,?,1)", (moment_id, k, s["id"], old["speaker"], old["text"], old["rendered"],
                                                         old["include"], old["alts"]))
                continue
            spk = s["spk"] or s["spk_guess"] or "other"
            text = s["text_final"] or s["text"]
            c.execute(
                "INSERT INTO moment_lines(moment_id, ord, seg_id, speaker, text, rendered, include, alts) VALUES(?,?,?,?,?,?,?,?)",
                (moment_id, k, s["id"], spk, text, render_line(emoji(spk), text), 1, json.dumps({"auto": s["text"]})),
            )
    _after_build(moment_id)


def _after_build(moment_id):
    try:  # safe clean-up on entering Polish (the owner, decision E); undone cards stay as they are
        from .autopolish import cleanup
        cleanup(moment_id)
    except Exception as e:
        print(f"    clean-up skipped for m{moment_id}: {e}")


def add_alternatives(moment_id):
    """Re-decode each line's audio with language forced to zh and to en."""
    m = db.one("SELECT m.*, r.path FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.id=?", moment_id)
    from .asr import asr_prompt; prompt = asr_prompt()
    for ln in db.rows("SELECT l.*, s.start, s.end, s.text AS auto FROM moment_lines l JOIN segments s ON s.id=l.seg_id WHERE moment_id=?", moment_id):
        alts = json.loads(ln["alts"] or "{}")
        if "zh" in alts and "en" in alts:
            continue
        alts["auto"] = ln["auto"]
        try:  # which languages are plausible for this audio at all?
            _, probs = decode_clip(m["path"], ln["start"], ln["end"], "auto", prompt, with_probs=True)
            alts["p"] = {k: round(float(probs.get(k, 0)), 3) for k in ("zh", "en")}
        except Exception:
            pass
        for lang in ("zh", "en"):
            try:
                alts[lang] = decode_clip(m["path"], ln["start"], ln["end"], lang, prompt)
            except Exception as e:
                alts[lang] = ""
                print(f"    alt decode failed: {e}")
        with db.tx() as c:
            c.execute("UPDATE moment_lines SET alts=? WHERE id=?", (json.dumps(alts, ensure_ascii=False), ln["id"]))


TRANSLATION = re.compile(r'"\s*\*\((.+?)\)\*')  # the *(…)* right after a Chinese quote


def translation_of(rendered):
    m = TRANSLATION.search(rendered or "")
    return m.group(1).strip() if m else ""


def _translation_examples(n=12, exclude_moment=None):
    """This parent's own wording: translations they rewrote, then the published journal."""
    ex = []
    for l in db.rows("SELECT l.text, l.rendered, l.speaker FROM moment_lines l JOIN moments m ON m.id=l.moment_id "
                     "WHERE l.edited=1 AND m.status IN ('journal','ready','published') AND l.rendered LIKE '%*(%' AND m.id != ? "
                     "ORDER BY l.id DESC LIMIT 40", exclude_moment or -1):
        tr = translation_of(l["rendered"])
        if tr and is_mostly_chinese(l["text"]) and "{" not in tr:
            ex.append(f"{l['speaker'].upper()}: {l['text']} → {tr}")
    try:
        from pathlib import Path
        root = Path(CFG["journal_repo"]) / "src" / "content" / "entries"
        for f in sorted(root.glob("*/*.md"), reverse=True):
            body = f.read_text(encoding="utf-8")
            if re.search(r"^audience:.*private", body, re.M):
                continue  # entries pulled from the site aren't style examples
            for line in body.splitlines():
                m = re.search(r'"([^"]*[\u3400-\u9fff][^"]*)"\s*\*\(([^)]+)\)\*', line)
                if m and is_mostly_chinese(m.group(1)) and len(ex) < n + 20:
                    ex.append(f"{m.group(1)} → {m.group(2)}")
    except Exception:
        pass
    seen, out = set(), []
    for e in ex:
        if e not in seen:
            seen.add(e); out.append(e)
    return out[:n]


def translate_lines(moment_id, only=None, texts=None, temperature=0.2):
    """Translate a moment's Chinese lines *in context*: the whole conversation with speakers, the moment's note,
    and this parent's own past translations as the style guide. only = line ids to translate (default: all
    Chinese lines); texts = {line_id: unsaved text}. Returns {line_id: {"translation", "glosses"}}."""
    from .textutil import speech_only
    texts = texts or {}
    lines = db.rows("SELECT id, speaker, text, include FROM moment_lines WHERE moment_id=? ORDER BY ord", moment_id)
    m = db.one("SELECT why, note_public FROM moments WHERE id=?", moment_id) or {}
    need = set()
    convo = []
    for l in lines:
        t = speech_only(texts.get(l["id"], l["text"]))
        mark = "  "
        if lang_of(t) in ("zh", "mixed") and (only is None or l["id"] in only):
            need.add(l["id"]); mark = ">>"
            if not is_mostly_chinese(t):
                t += f"   (Chinese words: {', '.join(cjk_runs(t))})"
        convo.append(f"{mark} [{l['id']}] {(l['speaker'] or 'other').upper()}: {t}")
    if not need:
        return {}
    ctx = m.get("note_public") or ""
    ex = _translation_examples(exclude_moment=moment_id)
    user = ((f"Situation: {ctx}\n" if ctx else "")
            + ("How this parent translates (match this style):\n" + "\n".join(f"- {e}" for e in ex) + "\n\n" if ex else "")
            + "Conversation:\n" + "\n".join(convo)
            + f"\n\nReturn exactly {len(need)} results, one per >> id: {sorted(need)}")
    res = chat_json(TR_SYSTEM, user, TR_SCHEMA, max_tokens=2000, temperature=temperature)
    return {r["id"]: r for r in res.get("lines", []) if r.get("id") in need}


def suggest_rendering(moment_id):
    """Fill house-style renderings (pinyin is deterministic; English meaning is a suggestion, made in context)."""
    lines = db.rows("SELECT * FROM moment_lines WHERE moment_id=? ORDER BY ord", moment_id)
    tr = {}
    try:
        tr = translate_lines(moment_id, only={l["id"] for l in lines if not l.get("edited")})
    except Exception as e:
        print(f"    translate failed: {e}")
    with db.tx() as c:
        for l in lines:
            if l.get("edited"):
                continue  # never overwrite your wording
            r = tr.get(l["id"], {})
            gl = {g["chinese"]: g["meaning"] for g in r.get("glosses", [])}
            c.execute("UPDATE moment_lines SET rendered=? WHERE id=?",
                      (render_line(emoji(l["speaker"]), l["text"], r.get("translation", ""), gl), l["id"]))


def prepare(moment_id, alternatives=True):
    build_lines(moment_id)
    if alternatives:
        add_alternatives(moment_id)
    suggest_rendering(moment_id)
