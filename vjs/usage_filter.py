"""Language-log noise filter (2026-10-14). The LLM flags many "errors" that are really speech-recognition artifacts or not
errors at all. These deterministic rules recognise them; matching flags are set to status 'dismissed' with `auto` = the rule
name (so a human 'dismissed' is distinguishable, and `./vjs.sh usagefilter --restore` puts them all back). Flags you
confirmed or dismissed yourself are never touched. New flags are screened the same way before they are stored.

Rules (each was checked against the flags the owner confirmed — none of those match):
  punctuation/spacing/case only · "no error" · stylistic/colloquial/ASR hedging in the explanation · long run-on (not one utterance)
  · cut-off sentence completed · English word inside Chinese (code-switch / measure word before an English noun) · number-only changes
Mislabelled kinds (a Chinese-only kind on English text) are re-labelled, not dismissed."""
import re

from . import db

LATIN = re.compile(r"[A-Za-z]{2,}")
CJK = re.compile(r"[㐀-鿿]")
TOK = re.compile(r"[a-z0-9']+|[㐀-鿿]")
HEDGE = re.compile(r"garbl|misheard|mishear|speech recogni|run-on|run on|punctuat|capitali[sz]|apostrophe|spacing|missing comma|"
                   r"jumbled|awkward|stylistic|casual speech|more natural|fits better|better fits|sounds better|no error|口语|口頭|不规范|不規範", re.I)
ZH_ONLY = {"measure-word", "aspect-particle"}


def _toks(t):
    return TOK.findall((t or "").lower().replace("’", "'"))


def reason(kind, lang, span, correct, explanation):
    """Name of the rule that says this flag is noise, or None."""
    span, correct, expl = (span or "").strip(), (correct or "").strip(), explanation or ""
    ts, tc = _toks(span), _toks(correct)
    if ts == tc or "".join(ts) == "".join(tc):
        return "punctuation-or-spacing-only"
    if re.search(r"\bno error\b|\(no error\)", correct + " " + expl, re.I):
        return "not-an-error"
    if HEDGE.search(expl):
        return "asr-or-style-hedge"
    if len(ts) > 11 or len(span) > 70:
        return "run-on-not-one-utterance"
    if len(tc) >= len(ts) + 2 and tc[:len(ts)] == ts:
        return "cut-off-sentence-completed"
    if LATIN.search(span) and CJK.search(span) and set(CJK.findall(span)) <= set(CJK.findall(correct)):
        return "code-switch-not-an-error"          # 哪个game → 哪个游戏, 太easy了 → 太容易了
    if lang == "zh" and kind == "measure-word" and LATIN.search(span):
        return "measure-word-before-english-noun"
    strip = lambda ts_: [re.sub(r"\d+", "#", x) for x in ts_]
    if strip(ts) == strip(tc):
        return "number-only-change"
    return None


def relabel(kind, lang, span):
    """A Chinese-only kind on English text is a mislabel — call it 'other-grammar' instead of dropping a possibly real flag."""
    return "other-grammar" if kind in ZH_ONLY and not CJK.search(span or "") else kind


def apply(dry=False):
    rows = db.rows("SELECT id, kind, lang, span, correct, explanation FROM usage_flags WHERE status='suggested'")
    hit, rel = {}, 0
    with db.tx() as c:
        for r in rows:
            why = reason(r["kind"], r["lang"], r["span"], r["correct"], r["explanation"])
            if why:
                hit[why] = hit.get(why, 0) + 1
                if not dry:
                    c.execute("UPDATE usage_flags SET status='dismissed', auto=? WHERE id=?", (why, r["id"]))
            else:
                k2 = relabel(r["kind"], r["lang"], r["span"])
                if k2 != r["kind"]:
                    rel += 1
                    if not dry:
                        c.execute("UPDATE OR IGNORE usage_flags SET kind=? WHERE id=?", (k2, r["id"]))
    left = db.one("SELECT count(*) n FROM usage_flags WHERE status='suggested'")["n"]
    return {"checked": len(rows), "auto_dismissed": sum(hit.values()), "by_rule": hit, "relabelled": rel, "still_suggested": left if not dry else len(rows) - sum(hit.values())}


def restore():
    with db.tx() as c:
        n = c.execute("UPDATE usage_flags SET status='suggested', auto=NULL WHERE auto IS NOT NULL").rowcount
    return {"restored": n}
