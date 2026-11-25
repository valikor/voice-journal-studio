"""Resumable batch pipeline: ingest → ASR → speaker features → mine moments →
usage checks. Newest recordings first, so fresh material reaches the review
queue while the backlog is still processing. Safe to stop and re-run anytime."""
import os
import time
import traceback
from datetime import datetime

from . import db, ingest
from .asr import asr_prompt, transcribe_wav
from .audio import drop_wav, wav16k
from .config import CFG, DATA
from .textutil import lang_of

LOCK = DATA / "pipeline.lock"


def _status(**kw):
    st = db.kv_get("pipeline", {})
    st.update(kw, updated=datetime.now().isoformat(timespec="seconds"))
    db.kv_set("pipeline", st)


def running():
    if not LOCK.exists():
        return False
    try:
        os.kill(int(LOCK.read_text()), 0)
        return True
    except Exception:
        return False


def asr_one(rec):
    t0 = time.time()
    wav = wav16k(rec["path"], rec["id"])
    segs = transcribe_wav(wav, asr_prompt(),
                          progress=lambda i, n: _status(detail=f"ASR chunk {i}/{n}"))
    # lines you corrected or labelled are kept; new lines covering the same audio are skipped
    kept = db.rows("SELECT start, end FROM segments WHERE rec_id=? AND (text_final IS NOT NULL OR spk IS NOT NULL)", rec["id"])
    segs = [x for x in segs if not any(min(x["end"], k["end"]) - max(x["start"], k["start"]) > 0.3 * (x["end"] - x["start"]) for k in kept)]
    base = (db.one("SELECT max(idx) m FROM segments WHERE rec_id=?", rec["id"])["m"] or 0) + 1000
    with db.tx() as c:
        c.execute("DELETE FROM segments WHERE rec_id=? AND text_final IS NULL AND spk IS NULL", (rec["id"],))
        c.executemany(
            "INSERT INTO segments(rec_id, idx, start, end, text, lang, avg_logprob, no_speech, flags, asr_lang) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(rec["id"], base + i, s["start"], s["end"], s["text"], lang_of(s["text"]), s["avg_logprob"], s["no_speech"], s["flags"], s.get("asr_lang"))
             for i, s in enumerate(segs)],
        )
        c.execute("UPDATE recordings SET status='transcribed', asr_seconds=?, error=NULL, vad_v=2 WHERE id=?",
                  (round(time.time() - t0, 1), rec["id"]))
    from .repair import _renumber
    _renumber(rec["id"])
    import soundfile as sf
    from .repair import rescue_languages
    rescue_languages(rec["id"], sf.read(str(wav), dtype="float32")[0], asr_prompt())
    return wav


def remine(verbose=True):
    """Re-flag noise on everything transcribed, then re-mine recordings — keeps any moment you've already triaged."""
    from . import media
    from .mine import mine_recording
    from .llm import ensure_ready
    ensure_ready()
    print(f"  text flags: {media.flag_text()} · repeat flags: {media.flag_repeats()}")
    with db.tx() as c:
        c.execute("DELETE FROM moment_lines WHERE moment_id IN (SELECT id FROM moments WHERE status='pending')")
        n = c.execute("DELETE FROM moments WHERE status='pending'").rowcount
    print(f"  cleared {n} untriaged moment(s)")
    recs = db.rows("SELECT id, filename FROM recordings WHERE status='mined' ORDER BY recorded_at DESC")
    for i, r in enumerate(recs, 1):
        k = db.one("SELECT count(*) n FROM segments WHERE rec_id=? AND spk_guess='kid' AND coalesce(flags,'') IN ('','media')", r["id"])["n"]
        if verbose:
            print(f"[{i}/{len(recs)}] {r['filename']} ({k} clean kid lines)")
        if k >= CFG["mine_min_kid_segments"]:
            mine_recording(r["id"], verbose=verbose)


def run(limit=None, only=None, skip_llm=False, usage=True):
    if running():
        print("pipeline already running")
        return
    LOCK.write_text(str(os.getpid()))
    try:
        _status(state="running", started=datetime.now().isoformat(timespec="seconds"), detail="ingest")
        ingest.scan()
        try:   # memos you labelled orange while they were queued
            from .finder_tags import refresh as _orange
            _orange()
        except Exception as e:
            print(f"  orange-label refresh skipped: {e}")
        llm_ok = False
        if not skip_llm:
            try:
                from .llm import ensure_ready
                ensure_ready()
                llm_ok = True
            except Exception as e:
                print(f"  ! LLM unavailable ({e}); transcribing only")
        q = "SELECT * FROM recordings WHERE status IN ('new','transcribed','analyzed')"
        args = []
        if only:
            q += " AND filename LIKE ?"
            args.append(f"%{only}%")
        # family recordings first (hint from old summaries / filename), then everything else; newest first within
        since = CFG.get("prioritize_since") or "9999"
        # newest memos first (config prioritize_since), then family recordings, then everything else
        todo = db.rows(q + " ORDER BY CASE WHEN recorded_at >= ? THEN 0 ELSE 1 END, "
                       "CASE WHEN recorded_at >= ? THEN recorded_at END DESC, "
                       "CASE WHEN filename LIKE '%work%' THEN 2 WHEN prior='family' THEN 0 "
                       "WHEN prior='work' THEN 2 ELSE 1 END, recorded_at DESC", *args, since, since)
        if limit:
            todo = todo[: int(limit)]
        for n, rec in enumerate(todo, 1):
            now = db.one("SELECT status FROM recordings WHERE id=?", rec["id"])   # skipped / set aside in the Inbox preview since the run began?
            if not now or now["status"] in ("skipped", "delete_candidate"):
                print(f"[{n}/{len(todo)}] {rec['filename']} - skipped (set aside in the Inbox preview)")
                continue
            label = f"[{n}/{len(todo)}] {rec['filename']} ({(rec['duration'] or 0) / 60:.0f} min)"
            print(label)
            _status(current=rec["filename"], done=n - 1, total=len(todo), detail="ASR")
            try:
                wav = None
                if rec["status"] == "new":
                    wav = asr_one(rec)
                    _status(detail="recovering speech the VAD missed")
                    from .repair import gapfill
                    gapfill(rec, verbose=True)  # also runs speaker analysis + junk flags
                    with db.tx() as c:
                        c.execute("UPDATE recordings SET status='analyzed' WHERE id=?", (rec["id"],))
                    rec["status"] = "analyzed"
                if rec["status"] == "transcribed":
                    from .speaker import analyze
                    _status(detail="speaker features")
                    wav = wav or wav16k(rec["path"], rec["id"])
                    kid = analyze(rec["id"], wav)
                    with db.tx() as c:
                        c.execute("UPDATE recordings SET status='analyzed' WHERE id=?", (rec["id"],))
                    rec["status"] = "analyzed"
                    print(f"    {kid} kid-pitch segment(s)")
                drop_wav(rec["id"])
                if rec["status"] == "analyzed":
                    try:  # voice fingerprints → better her-vs-Dad guesses before moments are mined
                        from . import voice
                        voice.embed_recording(rec["id"])
                        voice.apply(rec_id=rec["id"])
                    except Exception as e:
                        print(f"    voice step skipped: {e}")
                if rec["status"] == "analyzed" and rec.get("archive"):   # kept as archive/work/personal: readable + searchable, never mined
                    with db.tx() as c:
                        c.execute("UPDATE recordings SET status='mined' WHERE id=?", (rec["id"],))
                    rec["status"] = "mined"
                if rec["status"] == "analyzed" and llm_ok:
                    from . import media
                    media.flag_text(rec["id"])
                    media.flag_repeats()
                    r = db.one("SELECT count(*) n FROM segments WHERE rec_id=? AND spk_guess='kid' AND coalesce(flags,'') IN ('','media')", rec["id"])
                    if (r["n"] or 0) >= CFG["mine_min_kid_segments"]:
                        from .mine import mine_recording
                        _status(detail="mining moments")
                        mine_recording(rec["id"])
                    else:
                        with db.tx() as c:
                            c.execute("UPDATE recordings SET category=coalesce(category, CASE WHEN prior='work' THEN 'work' ELSE 'quiet' END) WHERE id=?", (rec["id"],))
                    from .quality import score_all
                    score_all(rec["id"])
                    with db.tx() as c:
                        c.execute("UPDATE recordings SET status='mined' WHERE id=?", (rec["id"],))
            except Exception as e:
                traceback.print_exc()
                with db.tx() as c:
                    c.execute("UPDATE recordings SET status='error', error=? WHERE id=?", (str(e)[:500], rec["id"]))
                drop_wav(rec["id"])
        try:  # the owner's ground truth: no child before 2023, 2024 allow-list, no Child/Mom inside work talk
            from .rules import enforce
            enforce()
        except Exception as e:
            print(f"  rules skipped: {e}")
        try:  # cards where she barely speaks never reach Triage (0 of 58 kept, 2026-10-07)
            from .learn import filter_thin
            filter_thin()
        except Exception as e:
            print(f"  thin filter skipped: {e}")
        try:  # processed recordings leave the Inbox / May–Sep paste for by-month/<YYYY-MM>/ (db path updated with each move)
            from .config import CFG as _C
            if _C.get("organize_recordings"):
                from .storage import organize
                organize(apply=True)
        except Exception as e:
            print(f"  filing skipped: {e}")
        if usage and llm_ok:
            from .usage import check_pending
            _status(detail="usage checks", current="")
            check_pending()
        _status(state="idle", detail="done", current="")
    finally:
        LOCK.unlink(missing_ok=True)
