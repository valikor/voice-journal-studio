"""Local review app (http://localhost:8765). Binds to 127.0.0.1 only."""
import json
import time
import mimetypes
import queue
import re
import subprocess
import sys
import threading
import traceback
import webbrowser
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import db
from .config import CFG, ROOT
from .textutil import lang_of, render_line, tokens, zh_punct

WEB = ROOT / "web"
JOBS = queue.Queue()
JOB_STATE = {}  # moment_id -> 'queued'|'working'|'done'|'error'


# ---------------------------------------------------------------- background prep
def _worker():
    from .polish import prepare
    while True:
        mid = JOBS.get()
        JOB_STATE[mid] = "working"
        try:
            prepare(mid)
            JOB_STATE[mid] = "done"
        except Exception:
            traceback.print_exc()
            JOB_STATE[mid] = "error"


RELISTEN = {"busy": False, "done": 0, "last_error": None}


def _relisten_worker():
    """Keeps second listens ready ahead of the reviewer (kept moments first, then the queue)."""
    import time
    from .relisten import queue_order_sql, relisten
    while True:
        try:
            nxt = db.rows(queue_order_sql(), 1)
            if not nxt:
                time.sleep(30)
                continue
            RELISTEN["busy"] = True
            if relisten(nxt[0]["id"]) is None:
                time.sleep(1)  # never spin: an unlistenable card must not pin a CPU core
            RELISTEN["done"] += 1
        except Exception as e:
            RELISTEN["last_error"] = str(e)[:200]
            time.sleep(10)
        finally:
            RELISTEN["busy"] = False


def enqueue(mid):
    if JOB_STATE.get(mid) in ("queued", "working"):
        return
    JOB_STATE[mid] = "queued"
    JOBS.put(mid)


# ---------------------------------------------------------------- helpers
def _rec(rec_id):
    return db.one("SELECT id, filename, recorded_at, duration, title, path, category FROM recordings WHERE id=?", rec_id)


def _segs(rec_id, a, b):
    return db.rows(
        "SELECT id, idx, start, end, coalesce(text_final, text) AS text, text AS raw, lang, spk, spk_guess, spk_conf, lang_hint, "
        "avg_logprob, q, text_final IS NOT NULL AS fixed, src FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? "
        "AND (flags IS NULL OR flags='' OR flags='media') ORDER BY idx", rec_id, a, b)  # junk/dup/repeat lines never shown


def _suspect_dups(segs):
    """Lines that are the same stretch of audio heard twice — same words, or the same thing in the other language
    (whisper translating). Marks the later one s["dup_of"] = the line it repeats, for a one-tap hide. Lines at
    different moments are real repeats (she echoes people) and are never flagged."""
    import difflib
    from .textutil import lang_of, speech_only
    shown = [s for s in segs if s.get("in")]
    for i, b in enumerate(shown):
        for a in shown[:i]:
            ov = min(a["end"], b["end"]) - max(a["start"], b["start"])
            short = min(a["end"] - a["start"], b["end"] - b["start"]) or 0.01
            if ov < 0.5 * short:
                continue
            ta, tb = speech_only(a["text"]).lower(), speech_only(b["text"]).lower()
            la, lb = lang_of(ta), lang_of(tb)
            same = difflib.SequenceMatcher(None, ta, tb).ratio() >= 0.6
            translated = {la, lb} == {"zh", "en"}
            if same or translated:
                b["dup_of"] = a["id"]; b["dup_kind"] = "translation" if translated and not same else "same"
                break


def moment_payload(m, context=2):
    rec = _rec(m["rec_id"])
    segs = _segs(m["rec_id"], m["seg_from"] - context, m["seg_to"] + context)
    for s in segs:
        s["in"] = m["seg_from"] <= s["idx"] <= m["seg_to"]
        s["speaker"] = s["spk"] or s["spk_guess"] or "other"
    lines = db.rows("SELECT * FROM moment_lines WHERE moment_id=? ORDER BY ord", m["id"])
    for l in lines:
        l["alts"] = json.loads(l["alts"] or "{}")
        seg = db.one("SELECT start, end FROM segments WHERE id=?", l["seg_id"]) if l["seg_id"] else None
        l["start"], l["end"] = (seg["start"], seg["end"]) if seg else (None, None)
    inside = [s for s in segs if s["in"]]
    out = dict(m)
    try:  # an AI note must agree with the card as it is NOW (edits, speakers, hidden lines); otherwise it's swapped for a neutral one
        from .notes import ensure as _note_ensure
        out["why"] = _note_ensure(dict(m))
    except Exception as e:
        print("note check failed", e)
    if m.get("kid_sim") is not None and m["kid_sim"] < 0.60 and m["status"] in ("pending", "later"):
        out["media_hint"] = {"sim": round(m["kid_sim"], 2)}   # proposal only: the voice on this card doesn't sound like hers
    out["tags"] = json.loads(m.get("tags") or "[]")
    out["relisten"] = json.loads(m["relisten"]) if m.get("relisten") else None
    out["rec"] = {k: rec[k] for k in ("id", "filename", "recorded_at", "duration", "title", "category")}
    from .labels import get as _lab, place_of as _place
    out["rec"]["label"] = _lab(rec["id"]); out["rec"]["place"] = _place(rec["title"])
    _suspect_dups(segs)
    dup = {x["id"]: (x["dup_of"], x["dup_kind"]) for x in segs if x.get("dup_of")}
    for l in lines:
        if l.get("seg_id") in dup and l.get("include"):
            l["dup_of"], l["dup_kind"] = dup[l["seg_id"]]
    try:
        from .window import suggest as _win
        out["window"] = _win(m) if not lines else None   # only before Polish: a card with a draft keeps its lines
    except Exception:
        out["window"] = None
    try:
        from .twins import seen as _seen
        out["seen"] = _seen(m)
    except Exception:
        out["seen"] = None
    out["segments"] = segs
    out["lines"] = lines
    out["clip"] = {"start": max(0, inside[0]["start"] - 0.5), "end": inside[-1]["end"] + 0.6} if inside else None
    out["prep"] = JOB_STATE.get(m["id"], "done" if lines and all("zh" in l["alts"] for l in lines) else ("none" if not lines else "partial"))
    from .publish import moment_markdown
    out["preview"] = moment_markdown(m, lines) if lines else ""
    if lines and m.get("status") == "journal":
        try:
            from .autopolish import ears
            out["ears"] = ears(m["id"])
        except Exception as e:
            out["ears"] = {"error": str(e)[:120]}
        out["autocleaned"] = [json.loads(r["before"]) | {"after": json.loads(r["after"])} for r in db.rows(
            "SELECT before, after FROM edits WHERE moment_id=? AND kind='autoclean' ORDER BY id", m["id"])]
    return out


def _confirm_speakers(m, speakers):
    """Saving speakers on keep = training labels for the speaker model."""
    with db.tx() as c:
        for sid, spk in (speakers or {}).items():
            c.execute("UPDATE segments SET spk=? WHERE id=? AND rec_id=?", (spk, int(sid), m["rec_id"]))


# ---------------------------------------------------------------- tonight session
CHECKS = {  # mode -> (where, question)
    "foreign": ("s.flags LIKE '%foreign%'", "We hid this as another language / junk. What is it really?"),
    "kid": ("coalesce(s.flags,'') IN ('','media') AND s.spk IS NULL AND s.spk_guess='kid' AND coalesce(s.adult_like,0)=0 "
            "AND length(s.text) >= 6", "We're counting this as her in the language log. Is it?"),
    "kept": ("coalesce(s.flags,'')='' AND length(s.text) >= 8", "Is this real speech, with roughly these words?"),
}


def _triage_top(n):
    from .rank import order_sql
    order = order_sql() if db.ranked() else "coalesce(m.priority,1), m.score DESC, r.recorded_at DESC, m.seg_from"
    return [r["id"] for r in db.rows(f"SELECT m.id FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.status='pending' ORDER BY {order} LIMIT ?", n)]


def merge_counts(groups):
    return {"groups": len(groups), "cards_absorbed": sum(len(g["cards"]) - 1 for g in groups)}


def quick_cards(limit=30, exclude=()):
    """Quick-judge rows: her single best line (from the punchline pass, else her longest line) + the line before it as setup."""
    ex = {int(x) for x in exclude}
    out = []
    for mid in _triage_top(min(2500, limit + len(ex) + 20)):
        if mid in ex:
            continue
        m = db.one("SELECT * FROM moments WHERE id=?", mid)
        segs = _segs(m["rec_id"], m["seg_from"] - 1, m["seg_to"])  # one line of lead-in, for setup
        inside = [x for x in segs if m["seg_from"] <= x["idx"] <= m["seg_to"]]
        spk = lambda x: x["spk"] or x["spk_guess"] or "other"
        her = [x for x in inside if spk(x) == "kid"]
        punch = next((x for x in her if x["id"] == m["punch_seg"]), None)
        scored = punch is not None
        if not punch:
            punch = max(her, key=lambda x: len(x["text"] or ""), default=None)
        if not punch:
            continue
        k = segs.index(punch)
        setup = segs[k - 1] if k > 0 else None
        rec = _rec(m["rec_id"])
        a = (setup["start"] if setup else punch["start"]) - 0.25
        out.append({"id": mid, "rec": m["rec_id"], "date": rec["recorded_at"], "tag": m["punch_tag"] if scored else None, "scored": scored,
                    "punch": {"spk": spk(punch), "t": punch["text"], "id": punch["id"]},
                    "setup": {"spk": spk(setup), "t": setup["text"]} if setup else None,
                    "clip": {"start": max(0, a), "end": punch["end"] + 0.5}})
        if len(out) >= limit:
            break
    return out


def daily_session(n_cards=20, n_checks=3):
    """Built once a day (at the 2 AM intake, or on first open) so opening it is instant; cards you've since decided drop out."""
    from . import lightning
    day = time.strftime("%Y-%m-%d")
    d = db.kv_get("daily", {})
    if d.get("date") != day:
        checks = []
        per = {"kid": 1, "kept": 1, "foreign": 1}
        for mode, k in per.items():
            where, question = CHECKS[mode]
            for sg in db.rows("SELECT s.id, s.rec_id, s.start, s.end, s.text, coalesce(s.spk, s.spk_guess) spk, r.recorded_at FROM segments s "
                              f"JOIN recordings r ON r.id=s.rec_id WHERE {where} AND s.id NOT IN (SELECT seg_id FROM spot_checks) "
                              "ORDER BY random() LIMIT ?", k):
                checks.append({"mode": mode, "question": question, "seg": sg})
        d = {"date": day, "cards": lightning.make_round(n_cards), "checks": checks[:n_checks]}
        db.kv_set("daily", d)
    if "sample_cards" not in d:  # safety net for auto-skip: up to 5 random skipped cards, slipped in unlabelled (vjs/autoskip.py)
        samp = db.rows("SELECT m.* FROM moments m WHERE m.status='auto_skipped' ORDER BY random() LIMIT 5")
        d["sample_cards"] = [dict(lightning._card(m), rand=True) for m in samp]
        d["sample_ids"] = [m["id"] for m in samp]
        db.kv_set("daily", d)
    live = {r["id"] for r in db.rows("SELECT id FROM moments WHERE status='pending'")}
    skipped = {r["id"] for r in db.rows("SELECT id FROM moments WHERE status='auto_skipped'")}
    done = db.kv_get("daily_log", {}).get(day)
    cards = [c for c in d["cards"] if c["id"] in live]
    import random
    for c in d.get("sample_cards", []):
        if c["id"] in skipped:
            cards.insert(random.randint(1, max(1, len(cards))), c)   # unlabelled: judged like any other card
    return {"date": day, "cards": cards, "checks": d["checks"], "done_today": done,
            "streak": _streak(db.kv_get("daily_log", {}))}


def _streak(log):
    import datetime as dt
    n, day = 0, dt.date.today()
    if day.isoformat() not in log:
        day -= dt.timedelta(days=1)
    while day.isoformat() in log:
        n += 1; day -= dt.timedelta(days=1)
    return n


def forecast():
    """Replaces the Quality-check question 'how much of a big backlog will be usable?' with numbers from your real decisions."""
    dec = db.rows("SELECT substr(r.recorded_at,1,7) month, m.status FROM moments m JOIN recordings r ON r.id=m.rec_id "
                  f"WHERE m.status IN ('journal','ready','published','log','rejected') AND m.id NOT IN ({db.SWEPT_SQL})")
    months = defaultdict(lambda: {"n": 0, "kept": 0, "log": 0, "rejected": 0})
    for r in dec:
        x = months[r["month"]]; x["n"] += 1
        x["kept" if r["status"] in ("journal", "ready", "published") else r["status"]] += 1
    kept = sum(x["kept"] for x in months.values()); n = sum(x["n"] for x in months.values())
    mined = db.one("SELECT round(sum(coalesce(r.duration,0))/3600.0,1) h, (SELECT count(*) FROM moments WHERE status NOT IN ('merged','empty','removed')) mom "
                   "FROM recordings r WHERE r.status='mined'")
    waiting = db.one("SELECT count(*) n, round(sum(coalesce(duration,0))/3600.0,1) h FROM recordings WHERE status IN ('new','error')")
    pend = db.one("SELECT count(*) n FROM moments WHERE status='pending'")["n"]
    rate = kept / n if n else 0
    per_h = (mined["mom"] / mined["h"]) if mined["h"] else 0
    sc = {r["mode"] + ":" + r["verdict"]: r["n"] for r in db.rows("SELECT mode, verdict, count(*) n FROM spot_checks GROUP BY 1,2")}
    ok, tot = sc.get("kept:ok", 0), sum(v for k, v in sc.items() if k.startswith("kept:"))
    return {"decided": n, "kept": kept, "keep_rate": round(rate, 3), "pending": pend, "pending_expected_keepers": round(pend * rate),
            "waiting_recordings": waiting["n"], "waiting_hours": waiting["h"] or 0,
            "moments_per_hour": round(per_h, 1), "expected_keepers_per_10h": round(per_h * 10 * rate),
            "expected_keepers_waiting": round((waiting["h"] or 0) * per_h * rate),
            "transcripts_roughly_right": {"ok": ok, "of": tot}, "months": [dict(month=k, **v) for k, v in sorted(months.items())]}


def library():
    """Everything at a glance: the whole archive by month, what's been done to it, and how good it is."""
    from . import quality
    from .edits import stats as edit_stats
    months = db.rows("""
        SELECT substr(recorded_at,1,7) month, count(*) n, round(sum(coalesce(duration,0))/3600.0,2) hours,
          round(sum(CASE WHEN status='skipped' THEN coalesce(duration,0) ELSE 0 END)/3600.0,2) h_skipped,
          round(sum(CASE WHEN status IN ('new','error') THEN coalesce(duration,0) ELSE 0 END)/3600.0,2) h_waiting,
          round(sum(CASE WHEN status IN ('transcribed','analyzed','mined') AND coalesce(vad_v,1)<2 THEN coalesce(duration,0) ELSE 0 END)/3600.0,2) h_first,
          round(sum(CASE WHEN status IN ('transcribed','analyzed','mined') AND vad_v=2 THEN coalesce(duration,0) ELSE 0 END)/3600.0,2) h_full,
          sum(CASE WHEN category='work' THEN 1 ELSE 0 END) work
        FROM recordings GROUP BY 1 ORDER BY 1""")
    kid = {r["month"]: r for r in db.rows("""
        SELECT substr(r.recorded_at,1,7) month, count(*) lines, round(avg(s.q),3) q,
          round(sum(s.end-s.start)/60.0,1) minutes
        FROM segments s JOIN recordings r ON r.id=s.rec_id
        WHERE coalesce(s.spk,s.spk_guess)='kid' AND (s.flags IS NULL OR s.flags='' OR s.flags='media') GROUP BY 1""")}
    mom = {r["month"]: r for r in db.rows("""
        SELECT substr(r.recorded_at,1,7) month, count(*) moments,
          sum(CASE WHEN m.status IN ('journal','ready','published','log','rejected') THEN 1 ELSE 0 END) reviewed,
          sum(CASE WHEN m.status IN ('journal','ready','published') THEN 1 ELSE 0 END) kept,
          sum(CASE WHEN m.status='published' THEN 1 ELSE 0 END) published,
          round(avg(json_extract(m.relisten,'$.agree')),3) agree
        FROM moments m JOIN recordings r ON r.id=m.rec_id GROUP BY 1""")}
    for mo in months:
        mo.update({k: kid.get(mo["month"], {}).get(k) for k in ("lines", "q", "minutes")})
        mo.update({k: mom.get(mo["month"], {}).get(k) for k in ("moments", "reviewed", "kept", "published", "agree")})
    tot = db.one("""SELECT count(*) n, round(sum(coalesce(duration,0))/3600.0,1) h,
        sum(status!='skipped') in_scope, round(sum(CASE WHEN status!='skipped' THEN coalesce(duration,0) ELSE 0 END)/3600.0,1) h_scope,
        sum(status IN ('transcribed','analyzed','mined')) done, round(sum(CASE WHEN status IN ('transcribed','analyzed','mined') THEN coalesce(duration,0) ELSE 0 END)/3600.0,1) h_done,
        sum(vad_v=2) full_pass FROM recordings""")
    seg = db.one("""SELECT count(*) lines, sum(coalesce(spk,spk_guess)='kid' AND (flags IS NULL OR flags='' OR flags='media')) kid,
        sum(flags LIKE '%dup%') dup, sum(flags LIKE '%halluc%') halluc, sum(flags LIKE '%foreign%') foreign_, sum(flags LIKE '%repeat%') rep,
        sum(flags LIKE '%loop%') loop_, sum(src='gap' AND (flags IS NULL OR flags='' OR flags='media')) recovered,
        sum(text_final IS NOT NULL) fixed FROM segments""")
    mst = {r["status"]: r["n"] for r in db.rows("SELECT status, count(*) n FROM moments GROUP BY 1")}
    rl = db.rows("SELECT json_extract(relisten,'$.agree') a, status FROM moments WHERE relisten IS NOT NULL "
                 "AND json_extract(relisten,'$.agree') IS NOT NULL")
    agree = {"n": len(rl), "mean": round(sum(r["a"] for r in rl) / len(rl), 3) if rl else None,
             "by_status": {st: round(sum(r["a"] for r in rl if r["status"] == st) / max(1, sum(1 for r in rl if r["status"] == st)), 3)
                           for st in {r["status"] for r in rl}}}
    return {"months": months, "totals": tot, "segments": seg, "moments": mst, "quality": quality.report(),
            "agreement": agree, "edits": edit_stats(), "relisten_worker": RELISTEN}


# ---------------------------------------------------------------- stats
def stats(tier="all"):
    from .usage import log_segments_sql
    base = log_segments_sql() if tier == "all" else "SELECT s.* FROM segments s WHERE s.spk='kid' AND (s.flags IS NULL OR s.flags='')"
    segs = db.rows(
        f"SELECT x.id, coalesce(x.text_final, x.text) t, x.spk, substr(r.recorded_at,1,7) month, r.recorded_at "
        f"FROM ({base}) x JOIN recordings r ON r.id=x.rec_id ORDER BY r.recorded_at, x.idx")
    checked = {r["seg_id"] for r in db.rows("SELECT seg_id FROM usage_checked")}
    flag_rows = db.rows("SELECT * FROM usage_flags WHERE status!='dismissed'")
    flags_by_seg = defaultdict(list)
    for f in flag_rows:
        flags_by_seg[f["seg_id"]].append(f)
    watch = [r["word"] for r in db.rows("SELECT word FROM watch_words ORDER BY added_at")]

    months = defaultdict(lambda: Counter())
    first_seen = {}
    freq = {"zh": Counter(), "en": Counter()}
    watch_m = defaultdict(Counter)
    kinds = Counter()
    for s in segs:
        m = s["month"]
        toks = tokens(s["t"])
        if not toks:
            continue
        L = lang_of(s["t"])
        mm = months[m]
        mm["utts"] += 1
        mm["tokens"] += len(toks)
        mm[L] += 1
        mm["verified"] += 1 if s["spk"] == "kid" else 0
        if s["id"] in checked:
            mm["checked"] += 1
            fl = flags_by_seg.get(s["id"], [])
            if fl:
                mm["flagged"] += 1
                for f in fl:
                    kinds[(f["lang"], f["kind"])] += 1
        for w, lg in toks:
            freq[lg][w] += 1
            mm["tok_" + lg] += 1
            if w not in first_seen:
                first_seen[w] = (m, lg, s["t"])
        low = s["t"].lower()
        for w in watch:
            wl = w.lower()
            n = sum(1 for t, _ in toks if t == wl) or (low.count(wl) if not re.match(r"^[a-z']+$", wl) else 0)
            if n:
                watch_m[w][m] += n
    new_by_month = defaultdict(list)
    for w, (m, lg, ex) in first_seen.items():
        new_by_month[m].append({"w": w, "lang": lg, "ex": ex[:120], "n": freq[lg][w]})
    month_rows = []
    for m in sorted(months):
        c = months[m]
        month_rows.append({
            "month": m, "utts": c["utts"], "tokens": c["tokens"],
            "mlu": round(c["tokens"] / c["utts"], 2) if c["utts"] else 0,
            "zh": c["zh"], "en": c["en"], "mixed": c["mixed"],
            "tok_zh": c["tok_zh"], "tok_en": c["tok_en"],
            "checked": c["checked"], "flagged": c["flagged"],
            "correct_pct": round(100 * (1 - c["flagged"] / c["checked"]), 1) if c["checked"] else None,
            "new_words": len(new_by_month.get(m, [])),
            "verified": c["verified"],
        })
    recent = sorted(new_by_month)[-3:]
    return {
        "tier": tier,
        "months": month_rows,
        "top": {lg: [{"w": w, "n": n} for w, n in freq[lg].most_common(60)] for lg in ("zh", "en")},
        "new_words": {m: sorted(new_by_month[m], key=lambda x: -x["n"])[:80] for m in recent},
        "watch": {w: [{"month": m, "n": watch_m[w].get(m, 0)} for m in sorted(months)] for w in watch},
        "kinds": [{"lang": k[0], "kind": k[1], "n": n} for k, n in kinds.most_common()],
        "totals": {"utts": len(segs), "verified": sum(1 for s in segs if s["spk"] == "kid"),
                   "checked": sum(1 for s in segs if s["id"] in checked)},
    }


# ---------------------------------------------------------------- handler
class H(BaseHTTPRequestHandler):
    server_version = "vjs"

    def log_message(self, *a):
        pass

    # -- plumbing
    def _json(self, obj, code=200):
        b = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _file(self, path: Path, ctype=None):
        if not path.exists():
            return self._json({"error": "not found"}, 404)
        size = path.stat().st_size
        ctype = ctype or mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = size - int(m.group(2))
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", "no-cache" if path.suffix in (".html", ".js", ".css") else "max-age=3600")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(end - start + 1))

    def do_GET(self):
        try:
            self._route("GET")
        except BrokenPipeError:
            pass
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    def do_POST(self):
        try:
            self._route("POST")
        except Exception as e:
            traceback.print_exc()
            self._json({"error": str(e)}, 500)

    # -- routes
    def _route(self, method):
        u = urlparse(self.path)
        p, q = u.path, {k: v[0] for k, v in parse_qs(u.query).items()}

        if method == "GET" and (p == "/" or p.startswith("/web/")):
            f = WEB / ("index.html" if p == "/" else p[5:])
            if WEB.resolve() not in f.resolve().parents and f.resolve() != (WEB / "index.html").resolve():
                return self._json({"error": "bad path"}, 400)
            return self._file(f)

        if p == "/api/status":
            from .pipeline import running
            return self._json({
                "recordings": db.rows("SELECT status, count(*) n, round(sum(duration)/3600.0,1) hours FROM recordings GROUP BY status"),
                "moments": {**{r["status"]: r["n"] for r in db.rows("SELECT status, count(*) n FROM moments GROUP BY status")},
                            "started": db.one("SELECT count(*) n FROM moments WHERE status='pending' AND touched_at IS NOT NULL")["n"],
                            "parked": db.one("SELECT count(*) n FROM moments WHERE status='pending' AND touched_at IS NULL AND parked_at IS NOT NULL")["n"],
                            "new": db.one("SELECT count(*) n FROM moments WHERE status='pending' AND touched_at IS NULL AND parked_at IS NULL")["n"]},
                "pipeline": db.kv_get("pipeline", {}), "running": running(),
                "flags": db.one("SELECT count(*) n FROM usage_flags WHERE status='suggested'")["n"],
                "labels": {r["spk"]: r["n"] for r in db.rows("SELECT spk, count(*) n FROM segments WHERE spk IS NOT NULL GROUP BY spk")},
                "names": {k: v["label"] for k, v in CFG["speakers"].items()},
                "emoji": {k: v["emoji"] for k, v in CFG["speakers"].items()},
                "ui": int(max(f.stat().st_mtime for f in (ROOT / "web").iterdir())),  # page reloads itself when this changes
            })

        if p == "/api/daily" and method == "GET":  # Daily 5: ~20 cards (sound + context) and 3 quick checks, built once a day
            return self._json(daily_session())
        if p == "/api/daily/done" and method == "POST":
            b = self._body(); day = time.strftime("%Y-%m-%d")
            log = db.kv_get("daily_log", {}); log[day] = {"cards": int(b.get("cards", 0)), "kept": int(b.get("kept", 0)), "checks": int(b.get("checks", 0))}
            db.kv_set("daily_log", log)
            return self._json({"ok": True, "days": len(log)})
        if p == "/api/autoskip" and method == "GET":
            from .autoskip import sample_stats
            return self._json(sample_stats())
        if p == "/api/quick" and method == "POST":  # {limit, exclude:[ids already shown]}
            b = self._body()
            return self._json({"items": quick_cards(int(b.get("limit", 30)), b.get("exclude", [])),
                               "scored": db.one("SELECT count(*) n FROM moments WHERE status='pending' AND punch_at IS NOT NULL")["n"],
                               "pending": db.one("SELECT count(*) n FROM moments WHERE status='pending'")["n"]})
        if p == "/api/inbox" and method == "GET":
            from . import inbox
            return self._json({"items": inbox.queue(), "running": bool(db.kv_get("pipeline", {}).get("state") == "running"), "current": db.kv_get("pipeline", {}).get("current", "")})
        if p == "/api/inbox/measure" and method == "POST":
            from . import inbox
            b = self._body()
            return self._json(inbox.measure(b["id"]))
        if p == "/api/inbox/set" and method == "POST":     # {ids, mode: skip|delete|restore}
            from . import inbox
            b = self._body()
            return self._json(inbox.set_aside(b["ids"], b["mode"]))
        if p == "/api/inbox/describe" and method == "POST":
            from . import inbox
            return self._json(inbox.describe(self._body()["id"]))
        if p == "/api/places" and method == "GET":
            from . import labels
            return self._json({"places": labels.places(q.get("all") == "1"), "tags": labels.all_tags()})
        if p == "/api/label" and method == "POST":   # {rec_ids:[...] | rec + place_all:true, note, tags:[...], mode:set|add}
            from . import labels
            b = self._body()
            ids = b.get("rec_ids") or [b["rec"]]
            if b.get("place_all") and b.get("rec"):
                pl = labels.place_of(_rec(b["rec"])["title"])
                ids = [r["id"] for r in db.rows("SELECT id, title FROM recordings") if labels.place_of(r["title"]) == pl]
            res = labels.set_label(ids, b.get("note"), b.get("tags"), b.get("mode", "set"))
            res["tags"] = labels.all_tags()
            return self._json(res)
        if p == "/api/kidok/info" and method == "GET":   # what "she's in these" would cover
            from . import rules
            cands = rules.candidates(q.get("year"), q.get("rec"))
            return self._json({"year": q.get("year"), "recordings": len(cands),
                               "cards": db.one("SELECT count(*) n FROM moments WHERE status='no_kid' AND rec_id IN (%s)" % ",".join("'%s'" % c["id"] for c in cands) if cands else "SELECT 0 n")["n"],
                               "unprocessed": sum(1 for c in cands if c["status"] == "skipped")})
        if p == "/api/kidok" and method == "POST":      # {rec} | {year} | {rec_ids}, allow: true|false
            from . import rules
            b = self._body()
            ids = b.get("rec_ids") or [c["id"] for c in rules.candidates(b.get("year"), b.get("rec"))] if b.get("allow", True) else (b.get("rec_ids") or [b["rec"]])
            if b.get("rec") and not b.get("year") and not b.get("rec_ids"):
                ids = [b["rec"]]
            return self._json(rules.allow_kid(ids) if b.get("allow", True) else rules.deny_kid(ids))
        if p == "/api/sort/list" and method == "GET":
            from . import sortrec
            return self._json({"items": sortrec.listing(q.get("kind") or None, q.get("unsorted") == "1"), "status": sortrec.status()})
        if p == "/api/sort/choose" and method == "POST":   # {ids:[...], choice: family archive|work meeting|personal note|delete candidate}
            from . import sortrec
            b = self._body()
            return self._json(sortrec.choose(b["ids"], b["choice"]))
        if p == "/api/sort/restore" and method == "POST":  # {id} or {all:true}
            from . import sortrec
            b = self._body()
            return self._json(sortrec.restore(None if b.get("all") else b["id"]))
        if p == "/api/reader/list" and method == "GET":
            from . import reader
            return self._json({"items": reader.recordings(q.get("month"), q.get("year"), q.get("kind"), q.get("q"))})
        if (m := re.fullmatch(r"/api/reader/([0-9a-f]+)", p)) and method == "GET":
            from . import reader
            r = reader.recording(m.group(1))
            return self._json(r if r else {"error": "not found"}, 200 if r else 404)
        if p == "/api/reader/span" and method == "POST":   # {rec, a, b, action: keep|log|skip}
            from . import reader
            b = self._body()
            return self._json(reader.span_action(b["rec"], b["a"], b["b"], b["action"]))
        if p == "/api/reader/span/undo" and method == "POST":
            from . import reader
            return self._json(reader.span_undo(self._body()))
        if p == "/api/reader/card" and method == "POST":   # {rec, a, b} -> a card id covering that span, for in-place edits
            from . import reader
            b = self._body()
            return self._json({"id": reader.card_for_edit(b["rec"], b["a"], b["b"])})
        if p == "/api/journal" and method == "GET":
            from . import journal_view
            return self._json(journal_view.journal())
        if p == "/api/corpus" and method == "GET":
            from . import corpus
            return self._json(corpus.corpus())
        if p == "/api/forecast" and method == "GET":
            return self._json(forecast())
        if p == "/api/merge" and method == "GET":
            from . import merge
            top = _triage_top(300)
            return self._json({"batches": merge.batches(), "pilot": merge_counts(merge.plan(set(top))), "all": merge_counts(merge.plan())})
        if p == "/api/merge/apply" and method == "POST":  # {scope: "pilot"|"all"}
            from . import merge
            scope = self._body().get("scope", "pilot")
            return self._json(merge.apply(merge.plan(set(_triage_top(300)) if scope == "pilot" else None)))
        if p == "/api/merge/undo" and method == "POST":
            from . import merge
            return self._json(merge.undo(self._body()["batch"]))
        if p == "/api/lightning/sweep" and method == "GET":
            from . import lightning
            return self._json(lightning.sweep_preview())
        if p == "/api/lightning/sweep" and method == "POST":  # {ids:[...]} -> file as log-only (undoable)
            from . import lightning
            return self._json({"filed": lightning.apply_sweep([int(i) for i in self._body().get("ids", [])])})
        if p == "/api/lightning/undo" and method == "POST":
            from . import lightning
            return self._json({"restored": lightning.undo_sweep()})

        if p == "/api/queue":
            stage = q.get("stage", "triage")
            if stage in ("mixed", "hints"):  # lists built by vjs/idlejobs3.py (mixed-language lines to correct; waiting cards the tuned model disputes)
                key = "mixed_queue" if stage == "mixed" else "tuned_hints"
                ids, seen = [], set()
                for x in (db.kv_get(key, []) or []):
                    mid = x["moment"] if isinstance(x, dict) else x
                    if mid not in seen:
                        seen.add(mid); ids.append(mid)
                ms = [m for m in (db.one("SELECT * FROM moments WHERE id=?", i) for i in ids) if m and m["status"] not in ("rejected", "removed", "merged")]
                ms = [m for m in ms if m["touched_at"] is None][: int(q.get("limit", 40))] if stage == "hints" else ms[: int(q.get("limit", 40))]
                return self._json({"total": len(ms), "items": [moment_payload(m) for m in ms]})
            status = {"triage": "('pending')", "started": "('pending')", "parked": "('pending')", "later": "('later')", "polish": "('journal')",
                      "publish": "('ready')", "published": "('published')", "rejected": "('rejected')",
                      "log": "('log')", "auto_skipped": "('auto_skipped')", "thin": "('thin')", "no_kid": "('no_kid')",
                      "merged": "('merged')"}.get(stage, "('pending')")
            from .rank import order_sql
            default = (order_sql() if db.ranked()  # overnight tournament, if it beat the old order
                       else "coalesce(m.priority,1), m.score DESC, r.recorded_at DESC, m.seg_from")  # family first
            order = {"date": "r.recorded_at DESC, m.seg_from",
                     "score": "m.score DESC, r.recorded_at DESC, m.seg_from"}.get(q.get("sort"), default)
            if stage in ("log", "later", "rejected", "thin", "no_kid", "auto_skipped", "merged"):
                # a pile you're browsing: cards you touched in the last day sink to the end, so the one you just
                # edited and re-filed (e.g. "Log only" again) doesn't come straight back as the top card
                order = "(CASE WHEN m.triaged_at >= datetime('now','-1 day','localtime') THEN 1 ELSE 0 END), " + order
            where = f"m.status IN {status} AND m.score >= ?"
            args = [int(q.get("min", 0))]
            if stage == "triage":
                where += " AND m.touched_at IS NULL AND m.parked_at IS NULL"  # New = never edited, not parked; edited-but-undecided cards live in Started
            elif stage == "parked":
                where += " AND m.touched_at IS NULL AND m.parked_at IS NOT NULL"
            elif stage == "started":
                where += " AND m.touched_at IS NOT NULL"
                order = "m.touched_at DESC"
            if q.get("tag"):
                where += " AND m.tags LIKE ?"
                args.append(f'%"{q["tag"]}"%')
            if q.get("family") == "1":
                where += " AND coalesce(m.priority,1) < 2"
            if q.get("month"):  # corpus-grid click: only cards from recordings of that month (YYYY-MM)
                where += " AND substr(r.recorded_at,1,7)=?"
                args.append(q["month"])
            if q.get("rec"):
                where += " AND m.rec_id=?"
                args.append(q["rec"])
            ms = db.rows(f"SELECT m.* FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE {where} ORDER BY {order} LIMIT ?",
                         *args, int(q.get("limit", 40)))
            total = db.one(f"SELECT count(*) n FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE {where}", *args)["n"]
            return self._json({"total": total, "items": [moment_payload(m) for m in ms]})

        if (m := re.fullmatch(r"/api/moment/(\d+)", p)) and method == "GET":
            mo = db.one("SELECT * FROM moments WHERE id=?", int(m.group(1)))
            return self._json(moment_payload(mo) if mo else {"error": "not found"}, 200 if mo else 404)

        if (m := re.fullmatch(r"/api/moment/(\d+)/(\w+)", p)) and method == "POST":
            return self._moment_action(int(m.group(1)), m.group(2), self._body())

        if p == "/api/moment/new" and method == "POST":
            b = self._body()
            seg = db.one("SELECT rec_id, idx FROM segments WHERE id=?", int(b["seg_id"]))
            a, z = seg["idx"] - int(b.get("before", 1)), seg["idx"] + int(b.get("after", 1))
            with db.tx() as c:
                c.execute("INSERT OR IGNORE INTO moments(rec_id, seg_from, seg_to, score, tags, why, status) VALUES(?,?,?,?,?,?,?)",
                          (seg["rec_id"], max(0, a), z, 10, "[]", "added by you from search", "pending"))
            mo = db.one("SELECT * FROM moments WHERE rec_id=? AND seg_from=? AND seg_to=?", seg["rec_id"], max(0, a), z)
            return self._json(moment_payload(mo))

        if p == "/api/clip":
            from .audio import clip
            rec = _rec(q["rec"])
            f = clip(rec["path"], float(q["start"]), float(q["end"]))
            return self._file(f, "audio/mp4")

        if p == "/api/notes":  # every {note} the owner has left, as a to-do list
            items = db.rows(
                "SELECT 'transcript line' AS \"where\", coalesce(s.text_final, s.text) AS text, s.rec_id, s.idx, r.recorded_at "
                "FROM segments s JOIN recordings r ON r.id=s.rec_id WHERE coalesce(s.text_final, s.text) LIKE '%{%' "
                "UNION ALL SELECT 'journal draft', l.rendered, m.rec_id, s.idx, r.recorded_at FROM moment_lines l "
                "JOIN moments m ON m.id=l.moment_id JOIN recordings r ON r.id=m.rec_id LEFT JOIN segments s ON s.id=l.seg_id "
                "WHERE l.rendered LIKE '%{%' AND l.include=1 AND m.status NOT IN ('rejected','published') "
                "UNION ALL SELECT 'context note', m.note_public, m.rec_id, m.seg_from, r.recorded_at FROM moments m "
                "JOIN recordings r ON r.id=m.rec_id WHERE m.note_public LIKE '%{%' ORDER BY 5 DESC LIMIT 200")
            return self._json({"items": items})

        if p == "/api/library":
            return self._json(library())

        if p == "/api/check" and method == "POST":
            b = self._body()
            with db.tx() as c:
                if b.get("undo"):
                    c.execute("DELETE FROM spot_checks WHERE id=(SELECT max(id) FROM spot_checks WHERE seg_id=?)", (int(b["seg_id"]),))
                else:
                    c.execute("INSERT INTO spot_checks(seg_id, mode, verdict) VALUES(?,?,?)", (int(b["seg_id"]), b["mode"], b["verdict"]))
            return self._json({"ok": True})

        if p == "/api/search":
            term = q.get("q", "").strip()
            if not term:
                return self._json({"items": []})
            kid = " AND coalesce(s.spk, s.spk_guess)='kid'" if q.get("kid") == "1" else ""
            items = db.rows(
                "SELECT s.id, s.idx, s.rec_id, s.start, s.end, coalesce(s.text_final, s.text) text, coalesce(s.spk, s.spk_guess) speaker, "
                f"r.recorded_at, r.title FROM segments s JOIN recordings r ON r.id=s.rec_id WHERE coalesce(s.text_final, s.text) LIKE ?{kid} "
                "ORDER BY r.recorded_at DESC LIMIT 100", f"%{term}%")
            recs = []   # recording-level hits: summaries, topics, speakers, titles (kept work meetings are findable here)
            if db.one("SELECT 1 FROM sqlite_master WHERE name='recording_summary'"):
                recs = db.rows("SELECT r.id, r.recorded_at, r.title, r.duration, coalesce(s.user_kind, s.kind) kind, s.summary, s.topics, l.note, l.tags FROM recordings r "
                               "LEFT JOIN recording_summary s ON s.rec_id=r.id LEFT JOIN recording_label l ON l.rec_id=r.id WHERE r.status!='delete_candidate' AND "
                               "(s.summary LIKE ? OR s.topics LIKE ? OR s.speakers LIKE ? OR r.title LIKE ? OR l.note LIKE ? OR l.tags LIKE ?) "
                               "ORDER BY r.recorded_at DESC LIMIT 30", *([f"%{term}%"] * 6))
            return self._json({"items": items, "recordings": recs})

        if p == "/api/stats":
            return self._json(stats(q.get("tier", "all")))

        if p == "/api/usage":
            st = q.get("status", "suggested")
            items = db.rows(
                "SELECT f.*, coalesce(s.text_final, s.text) text, s.rec_id, s.start, s.end, r.recorded_at FROM usage_flags f "
                "JOIN segments s ON s.id=f.seg_id JOIN recordings r ON r.id=s.rec_id WHERE f.status=? "
                "ORDER BY r.recorded_at DESC LIMIT 60", st)
            return self._json({"items": items})

        if (m := re.fullmatch(r"/api/usage/(\d+)", p)) and method == "POST":
            b = self._body()
            with db.tx() as c:
                c.execute("UPDATE usage_flags SET status=? WHERE id=?", (b["status"], int(m.group(1))))
            return self._json({"ok": True})

        if p == "/api/watch":
            if method == "POST":
                b = self._body()
                with db.tx() as c:
                    if b.get("remove"):
                        c.execute("DELETE FROM watch_words WHERE word=?", (b["word"],))
                    elif b.get("word", "").strip():
                        c.execute("INSERT OR IGNORE INTO watch_words(word) VALUES(?)", (b["word"].strip(),))
            return self._json({"words": [r["word"] for r in db.rows("SELECT word FROM watch_words ORDER BY added_at")]})

        if p == "/api/recordings":
            return self._json({"items": db.rows(
                "SELECT r.id, r.filename, r.recorded_at, r.duration, r.title, r.status, r.error, r.kid_segments, r.asr_seconds, r.category, r.prior, "
                "(SELECT count(*) FROM moments m WHERE m.rec_id=r.id) moments, "
                "(SELECT count(*) FROM moments m WHERE m.rec_id=r.id AND m.status='pending') pending "
                "FROM recordings r WHERE r.status!='skipped' ORDER BY r.recorded_at DESC")})

        if p == "/api/transcript":
            rec = _rec(q["rec"])
            segs = _segs(q["rec"], int(q.get("from", 0)), int(q.get("to", 10 ** 6)))
            for s in segs:
                s["speaker"] = s["spk"] or s["spk_guess"] or "other"
            return self._json({"rec": {k: rec[k] for k in ("id", "filename", "recorded_at", "title")}, "segments": segs})

        if p == "/api/pipeline/start" and method == "POST":
            from .pipeline import running
            if not running():
                subprocess.Popen([sys.executable, "-m", "vjs.cli", "run"], cwd=str(ROOT),
                                 stdout=open(ROOT / "data" / "pipeline.log", "ab"), stderr=subprocess.STDOUT,
                                 start_new_session=True)
            return self._json({"ok": True})

        if p == "/api/publish" and method == "POST":
            from .publish import publish_ready
            return self._json({"written": publish_ready()})

        return self._json({"error": f"no route {method} {p}"}, 404)

    def _moment_action(self, mid, action, b):
        m = db.one("SELECT * FROM moments WHERE id=?", mid)
        if not m:
            return self._json({"error": "not found"}, 404)

        if action == "triage":
            decision = {"triage": "pending", "polish": "journal"}.get(b["decision"], b["decision"])  # stage names slipped in once (m1075, m2806)
            if decision not in ("journal", "log", "rejected", "later", "pending", "ready", "published", "thin", "no_kid", "auto_skipped", "superseded", "reader_skip", "adhoc", "removed"):
                return self._json({"error": f"unknown decision {decision!r}"}, 400)
            if m["status"] == "auto_skipped" and decision != "auto_skipped":  # safety-net bookkeeping (vjs/autoskip.py)
                from .autoskip import record_sample
                sampled = db.kv_get("daily", {}).get("sample_ids", [])
                record_sample(mid, decision, "sample" if mid in sampled else "pile")
            if decision in ("journal", "log"):
                _confirm_speakers(m, b.get("speakers"))
            with db.tx() as c:
                c.execute("UPDATE moments SET status=?, star=coalesce(?, star), triaged_at=datetime('now','localtime') WHERE id=?",
                          (decision, b.get("star"), mid))
                if decision == "pending":  # undo: drop labels given by this moment
                    pass
            if decision == "journal":
                enqueue(mid)
            return self._json({"ok": True})

        if action == "range":  # extend/shrink: {before:+1/-1, after:+1/-1}
            a = max(0, m["seg_from"] - int(b.get("before", 0)))
            z = max(a, m["seg_to"] + int(b.get("after", 0)))
            with db.tx() as c:
                c.execute("UPDATE moments SET seg_from=?, seg_to=? WHERE id=?", (a, z, mid))
            if db.one("SELECT 1 FROM moment_lines WHERE moment_id=?", mid):
                from .polish import build_lines
                build_lines(mid, force=True)
                enqueue(mid)
            return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid)))

        if action == "segment":  # fix one transcript line (Triage/Tonight): {seg_id, text?, speaker?}
            from .edits import set_segment
            set_segment(int(b["seg_id"]), text=b.get("text"), speaker=b.get("speaker"), via=b.get("via", "card"), moment_id=mid)
            return self._json({"ok": True})

        if action == "rehear":  # whisper translated instead of transcribing? decode this line forced zh and forced en: {seg_id}
            from .asr import decode_clip
            seg = db.one("SELECT s.start, s.end, r.path FROM segments s JOIN recordings r ON r.id=s.rec_id WHERE s.id=?", int(b["seg_id"]))
            out, probs = {}, {}
            for lang in ("zh", "en"):
                # the one-sentence Mandarin prompt keeps whisper in Simplified characters (without it: 那個/這個)
                text, pr = decode_clip(seg["path"], seg["start"], seg["end"], lang, prompt="以下是普通话的句子。" if lang == "zh" else "", with_probs=True)
                text = zh_punct(text) if lang == "zh" else text
                out[lang] = text; probs = probs or pr
            return self._json({"zh": out["zh"], "en": out["en"],
                               "p_zh": round(float(probs.get("zh", 0)), 2), "p_en": round(float(probs.get("en", 0)), 2)})

        if action == "hide":  # one-tap: this line is a duplicate / junk → hidden (flag 'dup'), logged, reversible
            from .edits import log
            from .media import add_flag
            seg = db.one("SELECT id, flags, text FROM segments WHERE id=?", int(b["seg_id"]))
            with db.tx() as c:
                add_flag(c, seg["id"], seg["flags"], "dup")
                c.execute("UPDATE moment_lines SET include=0 WHERE moment_id=? AND seg_id=?", (mid, seg["id"]))
                log(c, "hide", seg["text"], "", seg["id"], mid, b.get("via", "card"))
            return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid), context=1))

        if action == "unhide":  # undo of a hide: take the 'dup' flag off again, put the line back in the draft (logged)
            from .edits import log
            seg = db.one("SELECT id, flags, text FROM segments WHERE id=?", int(b["seg_id"]))
            have = [f for f in (seg["flags"] or "").split(",") if f and f != "dup"]
            with db.tx() as c:
                c.execute("UPDATE segments SET flags=? WHERE id=?", (",".join(have), seg["id"]))
                c.execute("UPDATE moment_lines SET include=1 WHERE moment_id=? AND seg_id=?", (mid, seg["id"]))
                log(c, "unhide", "", seg["text"], seg["id"], mid, b.get("via", "card"))
            return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid), context=1))

        if action == "translate_alts":  # {line_id, text} → two more ways to say it, in context
            from .polish import translate_lines
            lid = int(b["line_id"])
            alts = []
            for temp in (0.7, 1.0):
                try:
                    r = translate_lines(mid, only={lid}, texts={lid: b.get("text")} if b.get("text") else None, temperature=temp).get(lid, {})
                    t = (r.get("translation") or "").strip()
                    if t and t not in alts:
                        alts.append(t)
                except Exception as e:
                    print("translate_alts", e)
            return self._json({"alts": alts})

        if action == "split":  # one whisper line that is really several turns: {seg_id, parts:[{text, speaker}]}
            from .edits import set_segment, log
            from .polish import emoji
            from .repair import _renumber
            parts = [p for p in b.get("parts", []) if (p.get("text") or "").strip()]
            seg = db.one("SELECT * FROM segments WHERE id=?", int(b["seg_id"]))
            if not seg or len(parts) < 2:
                return self._json({"error": "need a line and at least two parts"}, 400)
            # idempotent: the same split sent twice (double Enter, autosave retry) must change nothing
            cur_text = (seg["text_final"] or seg["text"] or "").strip()
            after_rows = db.rows("SELECT text_final, text, spk, spk_guess FROM segments WHERE rec_id=? AND src='split' AND start>=? AND id<>? ORDER BY start, idx",
                                 seg["rec_id"], seg["start"] - 0.001, seg["id"])
            want = [p["text"].strip() for p in parts[1:]]
            got = [(r["text_final"] or r["text"] or "").strip() for r in after_rows[:len(want)]]
            if cur_text == parts[0]["text"].strip() and got == want:
                return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid), context=1))
            # share the line's audio out by text length (approximate — good enough to replay a turn)
            w = [max(1, len(p["text"].strip())) for p in parts]
            dur, t, spans = seg["end"] - seg["start"], seg["start"], []
            for x in w:
                spans.append((t, t + dur * x / sum(w))); t = spans[-1][1]
            set_segment(seg["id"], text=parts[0]["text"], speaker=parts[0].get("speaker"), via=b.get("via", "split"), moment_id=mid)
            base = (db.one("SELECT max(idx) mx FROM segments WHERE rec_id=?", seg["rec_id"])["mx"] or 0) + 5000
            new_ids = []
            with db.tx() as c:
                c.execute("UPDATE segments SET end=?, src='split' WHERE id=?", (spans[0][1], seg["id"]))
                for i, (p, (a, e)) in enumerate(zip(parts[1:], spans[1:]), 1):
                    txt = p["text"].strip()
                    cur = c.execute("INSERT INTO segments(rec_id, idx, start, end, text, text_final, lang, spk, src, q) VALUES(?,?,?,?,?,?,?,?,'split',1.0)",
                                    (seg["rec_id"], base + i, a, e, txt, txt, lang_of(txt), p.get("speaker") or seg["spk"] or seg["spk_guess"]))
                    new_ids.append(cur.lastrowid)
                log(c, "split", seg["text_final"] or seg["text"], " / ".join(f'{p.get("speaker")}: {p["text"].strip()}' for p in parts),
                    seg["id"], mid, b.get("via", "card"))
            was_last = seg["idx"] == m["seg_to"]
            _renumber(seg["rec_id"])
            with db.tx() as c:
                if was_last:  # keep the new pieces inside the moment
                    c.execute("UPDATE moments SET seg_to=(SELECT idx FROM segments WHERE id=?) WHERE id=?", (new_ids[-1], mid))
                lines = db.rows("SELECT id, ord, seg_id FROM moment_lines WHERE moment_id=? ORDER BY ord", mid)
                host = next((l for l in lines if l["seg_id"] == seg["id"]), None)
                if host:  # a Polish draft exists: put each piece on its own draft line
                    c.execute("UPDATE moment_lines SET ord=ord+? WHERE moment_id=? AND ord>?", (len(new_ids), mid, host["ord"]))
                    for k, (nid, p) in enumerate(zip(new_ids, parts[1:]), 1):
                        spk = p.get("speaker") or "kid"
                        c.execute("INSERT INTO moment_lines(moment_id, ord, seg_id, speaker, text, rendered, include, alts, edited) VALUES(?,?,?,?,?,?,1,?,1)",
                                  (mid, host["ord"] + k, nid, spk, p["text"].strip(), render_line(emoji(spk), p["text"].strip()), json.dumps({"auto": ""})))
            return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid), context=1))

        if action == "save":  # Polish autosave: {lines:[{id,speaker,text,rendered,include}], note_public, note_private, entry_date, star, status?}
            from .edits import log, set_segment
            for l in b.get("lines", []):
                cur = db.one("SELECT * FROM moment_lines WHERE id=? AND moment_id=?", int(l["id"]), mid)
                if not cur:
                    continue
                if cur["seg_id"] and (l["text"] != cur["text"] or l["speaker"] != cur["speaker"]):
                    set_segment(cur["seg_id"], text=l["text"] if l["text"] != cur["text"] else None,
                                speaker=l["speaker"] if l["speaker"] != cur["speaker"] else None, via="polish", moment_id=mid)
                with db.tx() as c:
                    changed = (l["rendered"] != cur["rendered"] or bool(l["include"]) != bool(cur["include"])
                               or l["text"] != cur["text"] or l["speaker"] != cur["speaker"])
                    if l["rendered"] != cur["rendered"]:
                        log(c, "rendered", cur["rendered"], l["rendered"], cur["seg_id"], mid, "polish")
                    if bool(l["include"]) != bool(cur["include"]):
                        log(c, "include", str(cur["include"]), str(int(bool(l["include"]))), cur["seg_id"], mid, "polish")
                    c.execute("UPDATE moment_lines SET speaker=?, text=?, rendered=?, include=?, edited=CASE WHEN ? THEN 1 ELSE edited END "
                              "WHERE id=?", (l["speaker"], l["text"], l["rendered"], 1 if l["include"] else 0, changed, cur["id"]))
            with db.tx() as c:
                for k in ("note_public", "note_private"):
                    if k in b and (b[k] or "") != (m[k] or ""):
                        log(c, k, m[k], b[k], None, mid, "polish")
                if "audio_off" in b:
                    c.execute("UPDATE moments SET audio_off=? WHERE id=?", (int(b["audio_off"]) or None, mid))
                c.execute("UPDATE moments SET note_public=?, note_private=?, entry_date=?, star=?, status=coalesce(?, status), "
                          "polished_at=CASE WHEN ?='ready' THEN datetime('now','localtime') ELSE polished_at END WHERE id=?",
                          (b.get("note_public", m["note_public"]), b.get("note_private", m["note_private"]),
                           b.get("entry_date") or m["entry_date"], int(b.get("star", m["star"]) or 0),
                           b.get("status"), b.get("status"), mid))
            if b.get("quiet"):
                return self._json({"ok": True})
            return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid)))

        if action == "render":  # {line_id, text, speaker, translation?, retranslate?} → {rendered, translation}
            # your translation is used as typed; the model (with the whole conversation as context) only fills
            # an empty one, word glosses for mixed lines, or when you ask for a fresh one (↻)
            from .polish import emoji, translate_lines
            from .textutil import is_mostly_chinese, speech_only
            text, spk = b["text"], b["speaker"]
            tr, gl = (b.get("translation") or "").strip(), {}
            zh = lang_of(speech_only(text)) in ("zh", "mixed")
            mostly = is_mostly_chinese(speech_only(text))
            if zh and (b.get("retranslate") or not mostly or not tr):
                lid = b.get("line_id")
                try:
                    if lid:
                        r = translate_lines(mid, only={int(lid)}, texts={int(lid): text}).get(int(lid), {})
                    else:
                        r = {}
                    if mostly and (b.get("retranslate") or not tr):
                        tr = r.get("translation", "") or tr
                    gl = {g["chinese"]: g["meaning"] for g in r.get("glosses", [])}
                except Exception as e:
                    print("render failed", e)
            if not mostly:
                tr = ""
            return self._json({"rendered": render_line(emoji(spk), text, tr, gl), "translation": tr})

        if action == "addline":  # a line whisper missed: {after_seg_id|null, speaker, text}
            from .polish import emoji
            from .repair import _renumber
            text, spk = b["text"].strip(), b.get("speaker", "kid")
            if not text:
                return self._json({"error": "empty"}, 400)
            twin = db.one("SELECT s.id FROM segments s WHERE s.rec_id=? AND s.src='manual' AND s.spk=? AND coalesce(s.text_final,s.text)=? "
                          "AND s.idx BETWEEN ? AND ? AND s.start>=(SELECT start FROM segments WHERE id=?)-0.01 AND s.start<=(SELECT start FROM segments WHERE id=?)+0.01 "
                          "AND (s.flags IS NULL OR s.flags='')", m["rec_id"], spk, text, 0, 10**9, b.get("after_seg_id") or 0, b.get("after_seg_id") or 0) if b.get("after_seg_id") else None
            if twin:  # same line added twice (double press) — already there
                return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid), context=1))
            segs = db.rows("SELECT id, idx, start, end FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? "
                           "AND (flags IS NULL OR flags='' OR flags='media') ORDER BY idx", m["rec_id"], m["seg_from"], m["seg_to"])
            after = next((x for x in segs if x["id"] == b.get("after_seg_id")), None)
            # timestamp just after the chosen line's start, so it always sorts directly below it
            # (lines can overlap in time; "after the end" could land below the next line instead)
            if after:
                t = after["start"] + 0.001
            else:  # at the start of the moment
                t = max(0.0, segs[0]["start"] - 0.001) if segs else 0.0
            base = (db.one("SELECT max(idx) mx FROM segments WHERE rec_id=?", m["rec_id"])["mx"] or 0) + 5000
            with db.tx() as c:
                cur = c.execute("INSERT INTO segments(rec_id, idx, start, end, text, text_final, lang, spk, src) VALUES(?,?,?,?,?,?,?,?,'manual')",
                                (m["rec_id"], base, t, (after["end"] if after else t + 0.5), text, text, lang_of(text), spk))
                new_id = cur.lastrowid
                from .edits import log
                log(c, "addline", None, f"{spk}: {text}", new_id, mid, b.get("via", "card"))
            first_id = segs[0]["id"] if segs else None
            last_id = segs[-1]["id"] if segs else None
            _renumber(m["rec_id"])
            new_idx = db.one("SELECT idx FROM segments WHERE id=?", new_id)["idx"]
            if m.get("relisten"):  # an added "heard but missing" line is no longer missing
                rl = json.loads(m["relisten"])
                rl["extra"] = [e for e in rl.get("extra", []) if e.get("text") != text]
                with db.tx() as c:
                    c.execute("UPDATE moments SET relisten=? WHERE id=?", (json.dumps(rl, ensure_ascii=False), mid))
            with db.tx() as c:  # keep the new line inside the moment when it's added at either end
                if after and after["id"] == last_id:
                    c.execute("UPDATE moments SET seg_to=? WHERE id=?", (new_idx, mid))
                if not after:
                    c.execute("UPDATE moments SET seg_from=? WHERE id=?", (new_idx, mid))
                # moments with a Polish draft get the line too, without touching edited lines
                lines = db.rows("SELECT id, ord, seg_id FROM moment_lines WHERE moment_id=? ORDER BY ord", mid)
                if lines:
                    pos = next((l["ord"] + 1 for l in lines if after and l["seg_id"] == after["id"]), 0)
                    c.execute("UPDATE moment_lines SET ord=ord+1 WHERE moment_id=? AND ord>=?", (mid, pos))
                    c.execute("INSERT INTO moment_lines(moment_id, ord, seg_id, speaker, text, rendered, include, alts, edited) VALUES(?,?,?,?,?,?,1,?,1)",
                              (mid, pos, new_id, spk, text, render_line(emoji(spk), text), json.dumps({"auto": ""})))
            return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid), context=1))

        if action == "undoclean":  # put the card back exactly as it was before the safe clean-up
            from .autopolish import undo_cleanup
            undo_cleanup(mid)
            return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid), context=1))

        if action == "relisten":
            from .relisten import relisten
            relisten(mid)
            return self._json(moment_payload(db.one("SELECT * FROM moments WHERE id=?", mid), context=1))

        if action == "prepare":
            from .polish import build_lines
            build_lines(mid)
            enqueue(mid)
            return self._json({"ok": True})

        return self._json({"error": f"unknown action {action}"}, 400)


def _nightly_intake(hour=3):
    """Automatic intake: at ~3 AM, if the Inbox holds recordings the database doesn't know, run the pipeline
    (ingest → transcribe → moments → thin filter → filing → rank). Runs inside the review app because that process
    was started from a Terminal tab and can read ~/Documents (a launchd job can't)."""
    import datetime as dt
    import subprocess
    import time
    from pathlib import Path
    from .pipeline import running
    last = None
    while True:
        time.sleep(300)
        now = dt.datetime.now()
        if now.hour < hour or last == now.date() or running():  # first check after 3 AM — catches up if the Mac slept through it
            continue
        last = now.date()
        try:
            daily_session()  # have today's Daily 5 ready before you open it
        except Exception as e:
            print(f"daily prebuild skipped: {e}")
        inbox = Path.home() / "Desktop" / "Voice Memos Inbox"
        known = {Path(r["path"]).name for r in db.rows("SELECT path FROM recordings")}
        new = [f for f in inbox.glob("*") if f.suffix.lower() in (".m4a", ".qta", ".wav", ".mp3") and f.name not in known]
        if not new:
            continue
        root = Path(__file__).resolve().parent.parent
        log = open(root / "data" / f"nightly_{now:%Y%m%d}.log", "w")
        subprocess.Popen(["/bin/zsh", "-c", "./vjs.sh run && ./vjs.sh rank"], cwd=root, stdout=log, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True)


def serve(port=None, open_browser=True):
    port = port or CFG["review_port"]
    for _ in range(2):
        threading.Thread(target=_worker, daemon=True).start()
    threading.Thread(target=_relisten_worker, daemon=True).start()
    threading.Thread(target=_nightly_intake, daemon=True).start()
    # anything kept for the journal but not yet prepared (e.g. server restarted)
    for r in db.rows("SELECT id FROM moments WHERE status='journal'"):
        if not db.one("SELECT 1 FROM moment_lines WHERE moment_id=? AND alts LIKE '%\"zh\"%'", r["id"]):
            enqueue(r["id"])
    httpd = ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://localhost:{port}/"
    print(f"Voice Journal Studio → {url}")
    if open_browser:
        webbrowser.open(url)
    httpd.serve_forever()
