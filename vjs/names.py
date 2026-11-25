"""One person, many spellings. Groups come from config.local.json "name_groups" (never from code):
[["Latin/pinyin", "简体", "nickname"], ...]. Used to (a) bias ASR with every form, (b) make quote/note checks treat the forms as equal."""
import re

from .config import CFG

GROUPS = [[f for f in g if f] for g in (CFG.get("name_groups") or []) if g]


def prompt_terms():
    return ", ".join(f for g in GROUPS for f in g)


def canon_map():
    m = {}
    for i, g in enumerate(GROUPS):
        for f in g:
            m[f.lower()] = f"namegroup{i}"
    return m


def canonicalize(text):
    """Replace any listed form with its group's canonical token so equality checks see one entity."""
    t = text or ""
    for form, tok in sorted(canon_map().items(), key=lambda kv: -len(kv[0])):
        t = re.sub(re.escape(form), f" {tok} ", t, flags=re.I)
    return t
