import argparse
import sys

from . import db


def main(argv=None):
    p = argparse.ArgumentParser(prog="vjs", description="Voice Journal Studio")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="ingest + transcribe + mine (newest first, resumable)")
    r.add_argument("--limit", type=int)
    r.add_argument("--file", help="only recordings whose filename contains this")
    r.add_argument("--no-llm", action="store_true", help="transcribe only")
    r.add_argument("--no-usage", action="store_true")
    s = sub.add_parser("serve", help="open the review app")
    s.add_argument("--port", type=int)
    s.add_argument("--no-open", action="store_true")
    qc = sub.add_parser("queuecut", help="no-loss queue cuts: file near-copies of already-handled cards, park small (<=2 line) cards")
    qc.add_argument("--apply", action="store_true"); qc.add_argument("--restore", action="store_true")
    sub.add_parser("mediacard", help="score waiting cards for 'voice is not hers' (TV / another child) — proposals only")
    nt = sub.add_parser("notes", help="AI notes vs card text: audit the consistency rate, replace notes that contradict their card, regenerate with the model")
    nt.add_argument("--audit", action="store_true"); nt.add_argument("--apply", action="store_true")
    nt.add_argument("--restore", action="store_true", help="put the model's original notes back"); nt.add_argument("--regen", action="store_true", help="GPU: rewrite replaced notes from the current card text")
    nt.add_argument("--limit", type=int, default=300)
    sub.add_parser("status")
    sub.add_parser("publish", help="write 'ready' moments into the journal repo")
    sub.add_parser("retry", help="reset recordings that errored")
    sc = sub.add_parser("spotcheck", help="listen to flagged/sampled lines and judge them")
    sc.add_argument("mode", nargs="?", default="foreign", help="foreign|halluc|repeat|media|kept|kid")
    sc.add_argument("-n", type=int, default=15)
    sub.add_parser("gapfill", help="recover speech the old VAD dropped, then re-mine those recordings")
    sub.add_parser("overnight", help="gapfill + full backlog, in that order")
    og = sub.add_parser("organize", help="file processed recordings from the Inbox / May–Sep paste into by-month/ (dry run unless --apply)")
    og.add_argument("--apply", action="store_true")
    sub.add_parser("rules", help="apply the owner's who-can-be-in-it rules (no child before 2023, 2024 allow-list, work recordings)")
    sub.add_parser("learn", help="train the keep-predictor on the owner's decisions; goes live only if it beats the current order")
    sub.add_parser("storage", help="audit: every recording accounted for once, nothing missing, nothing iCloud-only")
    sub.add_parser("compare", help="head-to-head: qwen3.5-9b vs qwen3-30b-a3b on the owner's translations and polish edits")
    sub.add_parser("audio", help="cut a clip of the real recording for every published quote (family journal)")
    so = sub.add_parser("sort", help="recordings not about her: --summarize (LLM), --status, --restore [<id>|--all] (undo 'delete candidate')")
    so.add_argument("--summarize", action="store_true")
    so.add_argument("--limit", type=int)
    so.add_argument("--redo", action="store_true")
    so.add_argument("--status", action="store_true")
    so.add_argument("--restore", nargs="?", const="--all", help="move delete candidates back: a recording id or --all")
    uf = sub.add_parser("usagefilter", help="auto-dismiss language-log flags that are transcription artifacts / code-switching / style (reversible)")
    uf.add_argument("--dry-run", action="store_true")
    uf.add_argument("--restore", action="store_true", help="put every auto-dismissed flag back")
    sub.add_parser("orange", help="apply Finder ORANGE labels: those memos are work, not for the journal (marked, never mined)")
    cl = sub.add_parser("clean", help="disk hygiene: old playback cache, big log, old database backups (moved to _to_delete/, never deleted); dry run unless --apply")
    cl.add_argument("--apply", action="store_true")
    sk = sub.add_parser("autoskip", help="file the lowest N%% of waiting cards as 'auto_skipped' (reversible); --restore brings them back")
    sk.add_argument("--pct", type=float, default=25)
    sk.add_argument("--by", choices=["order", "model"], default="order", help="order = bottom of the Triage order (default); model = lowest keep-probability")
    sk.add_argument("--dry-run", action="store_true")
    sk.add_argument("--restore", action="store_true")
    sk.add_argument("--month", help="with --restore: only this month (YYYY-MM)")
    sk.add_argument("--top", type=float, help="with --restore: only the best-ranked N%% of the auto-skipped pile")
    kp = sub.add_parser("keepers", help="backtest whether the punchline scores predict your keeps; --apply to put them in charge of Triage order if they win")
    kp.add_argument("--apply", action="store_true")
    pl = sub.add_parser("punchlines", help="overnight: the LLM names her single best line + a short reason on every waiting card (resumable)")
    pl.add_argument("--limit", type=int)
    pl.add_argument("--redo", action="store_true")
    pl.add_argument("--batch", type=int, default=10)
    pl.add_argument("--scope", choices=["pending", "decided", "both"], default="pending", help="decided = your past decisions, scored without your corrections (for the backtest)")
    sub.add_parser("rank", help="backtest a better Triage order on your past decisions; apply it only if it wins")
    ls = sub.add_parser("langscan", help="find English lines that were really Chinese speech (top of the Triage pile)")
    ls.add_argument("--cards", type=int, default=600)
    sub.add_parser("voice", help="fingerprint voices and re-guess speakers from your labels")
    sub.add_parser("redecode", help="re-decode recovered lines made with the old settings")
    sub.add_parser("cleanup", help="hide gap-fill duplicates, strip whisper's own translations")
    sub.add_parser("remine", help="re-flag background media/junk and re-score all untriaged moments")
    sub.add_parser("reguess", help="re-apply the speaker model trained on your corrections")
    u = sub.add_parser("usage", help="run usage checks on log utterances")
    u.add_argument("--limit", type=int)
    a = p.parse_args(argv)

    if a.cmd == "run":
        from .pipeline import run
        run(limit=a.limit, only=a.file, skip_llm=a.no_llm, usage=not a.no_usage)
    elif a.cmd == "serve":
        from .server import serve
        serve(port=a.port, open_browser=not a.no_open)
    elif a.cmd == "queuecut":
        from . import queuecut as _q
        if a.restore:
            print(_q.restore())
        elif a.apply:
            print(_q.apply())
        else:
            print("preview:", len(_q.find_dups()), "near-copies would be filed; add --apply")
    elif a.cmd == "mediacard":
        from . import mediacard as _mc
        print(_mc.score_all())
    elif a.cmd == "notes":
        from . import notes as _n
        if a.restore:
            _n.restore(); print("original notes restored")
        elif a.regen:
            print(_n.regen(limit=a.limit))
        elif a.apply:
            print("notes replaced:", _n.apply_all())
        else:
            r = _n.audit(); print({k: v for k, v in r.items() if k != "ids"})
    elif a.cmd == "status":
        for r in db.rows("SELECT status, count(*) n, round(sum(duration)/3600.0,1) hours FROM recordings GROUP BY status"):
            print(f"  recordings {r['status']:<12} {r['n']:>4}  {r['hours'] or 0:>6} h")
        for r in db.rows("SELECT status, count(*) n FROM moments GROUP BY status"):
            print(f"  moments    {r['status']:<12} {r['n']:>4}")
        from . import edits as _ed
        _ed.backfill_touched()
        started = db.one("SELECT count(*) n FROM moments WHERE status='pending' AND touched_at IS NOT NULL")["n"]
        bad = _ed.edited_but_new()
        print(f"  started    edited, not decided {started:>4}   |  INVARIANT edited-but-New: {len(bad)} {'OK' if not bad else bad[:10]}")
        print("  pipeline  ", db.kv_get("pipeline", {}))
        from .autoskip import sample_stats
        ss = sample_stats()
        if ss["pile"] or ss["sampled"]:
            rate = "no samples yet" if ss["rate"] is None else f"kept {ss['kept']} of the last {ss['sampled']} sampled ({ss['rate']:.0%})"
            print(f"  auto-skip  {ss['pile']} in the pile · {rate}" + ("  ⚠ may be too aggressive — " + ss["suggest"] if ss["warn"] else ""))
    elif a.cmd == "publish":
        from .publish import publish_ready
        for w in publish_ready():
            print(f"  {w['date']}  moment {w['id']}  → {w['path']}")
    elif a.cmd == "retry":
        with db.tx() as c:
            n = c.execute("UPDATE recordings SET status='new', error=NULL WHERE status='error'").rowcount
        print(f"  reset {n}")
    elif a.cmd == "spotcheck":
        from .spotcheck import run as spot
        spot(a.mode, a.n)
    elif a.cmd == "gapfill":
        from .llm import ensure_ready
        from .repair import gapfill_all
        ensure_ready()
        gapfill_all()
    elif a.cmd == "organize":
        from .storage import backfill_fp, organize
        print(f"  fingerprinted {backfill_fp()} recording(s)")
        organize(apply=a.apply)
    elif a.cmd == "rules":
        from .rules import enforce
        enforce()
    elif a.cmd == "learn":
        from .learn import train
        train()
    elif a.cmd == "storage":
        from .storage import check
        check()
    elif a.cmd == "compare":
        from .compare import run as cmp
        cmp(["qwen/qwen3.5-9b", "qwen/qwen3-30b-a3b-2507"])
    elif a.cmd == "audio":
        from .journal_audio import export_all
        export_all()
    elif a.cmd == "sort":
        from . import sortrec
        if a.restore:
            print("  ", sortrec.restore(None if a.restore == "--all" else a.restore))
        elif a.summarize:
            print("  ", sortrec.summarize(a.limit, a.redo))
        else:
            print("  ", sortrec.status())
    elif a.cmd == "usagefilter":
        from . import usage_filter
        r = usage_filter.restore() if a.restore else usage_filter.apply(a.dry_run)
        print("  " + ", ".join(f"{k} {v}" for k, v in r.items()))
    elif a.cmd == "orange":
        from .finder_tags import refresh
        print("  marked:", refresh(verbose=False))
    elif a.cmd == "clean":
        from . import clean
        import json
        print(json.dumps(clean.run(a.apply), indent=2))
    elif a.cmd == "autoskip":
        from . import autoskip
        r = autoskip.restore(a.month, a.top) if a.restore else autoskip.run(a.pct, a.dry_run, a.by)
        print("  " + ", ".join(f"{k} {v}" for k, v in r.items()))
    elif a.cmd == "keepers":
        from . import keepers
        r = keepers.backtest()
        if not r.get("ready"):
            print("  " + r["reason"])
        elif a.apply:
            keepers.apply()
    elif a.cmd == "punchlines":
        from .punchlines import run as punch_run
        print(punch_run(limit=a.limit, redo=a.redo, batch=a.batch, scope=a.scope))
    elif a.cmd == "rank":
        from .llm import ensure_ready
        from .rank import run as rank_run
        ensure_ready()
        rank_run()
    elif a.cmd == "langscan":
        from .langscan import scan
        scan(a.cards)
    elif a.cmd == "overnight":
        from .llm import ensure_ready
        from .pipeline import run
        from .repair import gapfill_all
        ensure_ready()
        gapfill_all()
        run()
    elif a.cmd == "voice":
        from .voice import run_all
        run_all()
    elif a.cmd == "redecode":
        from .repair import redecode_all
        redecode_all()
    elif a.cmd == "cleanup":
        from .repair import cleanup_all
        cleanup_all()
    elif a.cmd == "remine":
        from .pipeline import remine
        remine()
    elif a.cmd == "reguess":
        from .speaker import reguess_all
        print(f"  re-guessed {reguess_all()} segment(s)")
    elif a.cmd == "usage":
        from .llm import ensure_ready
        from .usage import check_pending
        ensure_ready()
        check_pending(limit=a.limit)


if __name__ == "__main__":
    sys.exit(main())
