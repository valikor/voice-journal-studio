"""Idle-GPU transcript audit (A1, 2026-11-19) — read-only on the journal, resumable, report only.

Moments the owner kept (published / ready / journal) already carry a second listen (vjs/relisten.py). Where that second listen
disagreed with the stored line, this module takes a THIRD, independent decode of just that line's audio with the current
settings (household vocabulary prompt, language forced to the line's own language and 'auto'), and flags the line only when
the new decode agrees with the second listen and not with the stored text. Two independent listens against one is the
signal; one against one is noise (whisper's confidently-wrong cases).

Lines the owner edited himself (moment_lines.edited=1, or segment text_final) are never flagged: he has already listened to them.
Nothing in moments / moment_lines / segments is changed; results go to data/audit_state.json (checkpoint) and a markdown report
in exports/.   Run:  ./.venv/bin/python -m vjs.audit [--limit N]      (idle GPU only; stop any time, it resumes)"""
import json
import sys
import time
from datetime import date

from . import db
from .config import DATA, ROOT

STATE = DATA / "audit_state.json"
STATUSES = ("published", "ready", "journal")
ORDER = {"published": 0, "ready": 1, "journal": 2}


def candidates():
    from .relisten import similarity
    out = []
    for m in db.rows("SELECT m.id, m.status, m.rec_id, m.relisten, r.recorded_at, r.path FROM moments m JOIN recordings r ON r.id=m.rec_id "
                     "WHERE m.status IN ('published','ready','journal') AND m.relisten IS NOT NULL"):
        rl = json.loads(m["relisten"])
        if rl.get("inconclusive"):
            continue
        edited = {r["seg_id"]: dict(r) for r in db.rows("SELECT seg_id, text, edited, include FROM moment_lines WHERE moment_id=?", m["id"])}
        for lid, v in (rl.get("lines") or {}).items():
            if v.get("verdict") != "disputed" or not v.get("alt") or v.get("sim") is None or v["sim"] >= 0.7:
                continue
            seg = db.one("SELECT id, start, end, text, text_final FROM segments WHERE id=?", int(lid))
            if not seg or seg["text_final"]:
                continue
            ml = edited.get(seg["id"])
            if ml and (ml["edited"] or not ml["include"]):
                continue
            out.append({"moment": m["id"], "status": m["status"], "when": m["recorded_at"], "path": m["path"], "seg": seg["id"],
                        "start": seg["start"], "end": seg["end"], "current": (ml["text"] if ml else None) or seg["text"], "second": v["alt"]})
    out.sort(key=lambda c: (ORDER[c["status"]], c["when"], c["seg"]))
    return out


def _state():
    try:
        return json.loads(STATE.read_text(encoding="utf8"))
    except Exception:
        return {}


def run(limit=None, verbose=True):
    from .asr import asr_prompt, decode_clip
    from .relisten import similarity
    from .textutil import lang_of
    st = _state()
    todo = [c for c in candidates() if str(c["seg"]) not in st]
    if limit:
        todo = todo[:limit]
    if verbose:
        print(f"  audit: {len(todo)} disputed lines to re-decode ({len(st)} already done)", flush=True)
    prompt = asr_prompt()
    t0 = time.time()
    for i, c in enumerate(todo, 1):
        lang = {"zh": "zh", "en": "en"}.get(lang_of(c["current"]), "auto")
        try:
            third = decode_clip(c["path"], c["start"], c["end"], lang, prompt)
            auto = decode_clip(c["path"], c["start"], c["end"], "auto", prompt)
        except Exception as e:
            third = auto = ""
            print(f"    decode failed seg {c['seg']}: {e}", flush=True)
        best = max((third, auto), key=lambda t: similarity(t, c["second"]))
        s_cur, s_sec = similarity(best, c["current"]), similarity(best, c["second"])
        verdict = "likely" if s_sec >= 0.8 and s_cur < 0.7 else "maybe" if s_sec >= 0.6 and s_cur < 0.8 else "noise"
        st[str(c["seg"])] = {"moment": c["moment"], "status": c["status"], "when": c["when"], "current": c["current"], "second": c["second"],
                             "third": third, "auto": auto, "verdict": verdict, "sim_cur": round(s_cur, 3), "sim_second": round(s_sec, 3)}
        if i % 20 == 0:
            STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf8")
            if verbose:
                print(f"    {i}/{len(todo)} · {int(time.time() - t0)}s", flush=True)
    STATE.write_text(json.dumps(st, ensure_ascii=False), encoding="utf8")
    return report(st)


def _rescore(st):
    """Re-judge saved results with Traditional→Simplified and digit/numeral differences ignored (whisper answers 從3數到4 for 从三数到四)."""
    import re
    from .relisten import similarity
    from .textutil import to_simplified
    num = str.maketrans("零一二三四五六七八九", "0123456789")

    def n(t):
        return re.sub(r"\s+", "", to_simplified(t or "")).translate(num)
    for v in st.values():
        best = max((v["third"], v["auto"]), key=lambda t: similarity(n(t), n(v["second"])))
        s_cur, s_sec = similarity(n(best), n(v["current"])), similarity(n(best), n(v["second"]))
        v["verdict"] = "likely" if s_sec >= 0.8 and s_cur < 0.7 else "maybe" if s_sec >= 0.6 and s_cur < 0.8 else "noise"
        v["sim_cur"], v["sim_second"] = round(s_cur, 3), round(s_sec, 3)
    return st


def report(st=None):
    st = _rescore(st or _state())
    rows = sorted(st.items(), key=lambda kv: (0 if kv[1]["verdict"] == "likely" else 1, ORDER[kv[1]["status"]], kv[1]["when"]))
    path = ROOT / "exports" / f"transcript-audit-{date.today().isoformat()}.md"
    n = {k: sum(1 for _, v in st.items() if v["verdict"] == k) for k in ("likely", "maybe", "noise")}
    lines = [f"# Transcript audit · {date.today().isoformat()}", "",
             f"Lines the owner kept where a second listen disagreed with the stored text, re-decoded a third time. "
             f"**{n['likely']} likely wrong**, {n['maybe']} maybe, {n['noise']} noise (not shown). Nothing was changed; open the card (Moment id) to fix.", ""]
    for key, title in (("likely", "Likely wrong (two listens agree against the stored line)"), ("maybe", "Maybe")):
        sel = [(s, v) for s, v in rows if v["verdict"] == key]
        if not sel:
            continue
        lines += [f"## {title}", ""]
        for s, v in sel:
            lines += [f"- **m{v['moment']}** · {v['status']} · {v['when'][:10]} · seg {s}",
                      f"  - stored: {v['current']}", f"  - heard again: {v['third'] or v['auto']}", f"  - second listen: {v['second']}"]
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf8")
    return {"report": str(path), **n}


if __name__ == "__main__":
    lim = int(sys.argv[sys.argv.index("--limit") + 1]) if "--limit" in sys.argv else None
    print(run(limit=lim))
