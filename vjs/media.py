"""Background-media and junk detection (TV, news, YouTube, the nightly Chinese
story audiobook, whisper hallucinations). Three independent signals, all
recorded as segment flags so they drop out of mining and the language log:

  repeat  — the same sentence shows up in recordings on different days. Stories
            and shows get replayed; a preschooler doesn't repeat long sentences
            verbatim night after night.
  foreign — whisper heard a language other than Mandarin/English (Welsh, Japanese,
            Russian… on noise), or the text is in kana/hangul/cyrillic.
  halluc  — known whisper outros ("thanks for watching", "欢迎订阅我的频道", …).
  media   — the mining model judged the line to be broadcast/narration/read-aloud
            (set in mine.py).
"""
import re
from collections import defaultdict

from . import db
from .asr import HALLUCINATIONS

FOREIGN_SCRIPT = re.compile(r"[぀-ヿ가-힯Ѐ-ӿ]")  # kana, hangul, cyrillic
OK_LANGS = {"zh", "en", "chinese", "english", "yue", "cantonese", None, ""}
NORM = re.compile(r"[\W_]+", re.U)
# function words of languages whisper hallucinates into on noise (fr/es/de/cy/pt) — none are English
OTHER_LATIN = set("le la les des est dans je tout rien sur une avec pour pas vous nous el los las del por que muy y ist und nicht ich das der mae yn ac wedi ei eu diolch iawn fawr não você é".split())


def _norm(t):
    return NORM.sub("", (t or "").lower())


def add_flag(c, seg_id, flags, flag):
    have = [f for f in (flags or "").split(",") if f]
    if flag not in have:
        c.execute("UPDATE segments SET flags=? WHERE id=?", (",".join(have + [flag]), seg_id))


HANZI = re.compile(r"[\u4e00-\u9fff]")
ENGLISH = set("i you he she it we they me my your a an the and or but is am are was were be do did not no yes yeah okay ok "
              "what this that there here to of in on for with can go want daddy dad mommy mom look oh wow".split())


def is_foreign(text, asr_lang):
    """Trust the text over whisper's language guess: whisper often labels a chunk 'korean' or
    'hawaiian' while writing perfectly good Mandarin, and hiding that would lose real family speech."""
    text = text or ""
    if FOREIGN_SCRIPT.search(text):
        return True                                   # kana / hangul / cyrillic on the page
    if HANZI.search(text):
        return False                                  # Chinese characters = Mandarin, whatever whisper guessed
    words = re.findall(r"[a-zà-ÿ']+", text.lower())
    if len(words) >= 4 and sum(w in OTHER_LATIN for w in words) / len(words) >= 0.3:
        return True                                   # French / Welsh / Spanish function words
    guessed_other = (asr_lang or "").lower() not in OK_LANGS
    return guessed_other and len(words) >= 2 and not any(w in ENGLISH for w in words)


# characters that exist only in Traditional Chinese; the family writes Simplified, so a line in Traditional is
# almost always whisper inventing text on noise (quality check 3: "我愛你", "我可以跟妳一起看電視嗎?")
TRAD_ONLY = set("愛們這個說會來時麼電視妳為裡還沒媽們對話歡樂點過讓給東車門開關見覺學習書寫聽頭長間題號後現經與應當從")
SIMP_ONLY = set("爱们这个说会来时么电视为里还没妈对话欢乐点过让给东车门开关见觉学习书写听头长间题号后现经与应当从")


def traditional(text):
    t = set(text or "")
    return len(t & TRAD_ONLY) >= 1 and not (t & (SIMP_ONLY - TRAD_ONLY))


def flag_text(rec_id=None):
    """foreign / halluc from the text (re-derived from scratch each time, so rule fixes apply retroactively)."""
    # lines you typed or corrected are never treated as whisper junk (e.g. a "[giggles]" you added)
    q = ("SELECT id, text, asr_lang, flags FROM segments WHERE text_final IS NULL AND coalesce(src,'')!='manual'"
         + (" AND rec_id=?" if rec_id else ""))
    n = 0
    with db.tx() as c:
        for s in db.rows(q, *([rec_id] if rec_id else [])):
            have = [f for f in (s["flags"] or "").split(",") if f and f not in ("foreign", "halluc", "doubt")]
            if is_foreign(s["text"], s["asr_lang"]):
                have.append("foreign"); n += 1
            elif HALLUCINATIONS.search(s["text"]) or traditional(s["text"]):
                have.append("halluc"); n += 1
            elif "*" in (s["asr_lang"] or "") and HANZI.search(s["text"] or ""):
                # re-decoded from a wrong-language guess INTO CHINESE: across 3 blind quality checks these were
                # almost never real ("我愛你", "我會想到你了"); English re-decodes were mostly real, so they stay
                have.append("doubt"); n += 1
            new = ",".join(have)
            if new != (s["flags"] or ""):
                c.execute("UPDATE segments SET flags=? WHERE id=?", (new, s["id"]))
    return n


def flag_repeats(min_chars=24):  # short everyday phrases ("Open your mouth.") legitimately repeat
    """Sentences heard on 2+ different days → background media."""
    seen = defaultdict(set)
    segs = db.rows("SELECT s.id, s.text, s.flags, substr(r.recorded_at,1,10) day FROM segments s "
                   "JOIN recordings r ON r.id=s.rec_id")
    for s in segs:
        k = _norm(s["text"])
        if len(k) >= min_chars:
            seen[k].add(s["day"])
    n = 0
    with db.tx() as c:
        for s in segs:
            k = _norm(s["text"])
            if len(k) >= min_chars and len(seen[k]) >= 2 and "repeat" not in (s["flags"] or ""):
                add_flag(c, s["id"], s["flags"], "repeat"); n += 1
    return n
