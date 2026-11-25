"""`./vjs.sh storage` — is every recording accounted for, exactly once, and safe?

Checks (read-only): content duplicates across the source folders; database rows whose file is gone (moved/renamed);
audio on disk the database doesn't know; recordings ingested twice; iCloud-only placeholders ("Optimize Mac Storage"
can offload files — the pipeline still reads them, but Backblaze skips them); and the size of the derived data here.
See STORAGE.md for the layout."""
import collections
import hashlib
import subprocess
from pathlib import Path

from . import db
from .config import CFG, DATA

AUDIO = (".m4a", ".qta", ".wav", ".mp3")


def _fp(p):
    h, s = hashlib.sha1(), p.stat().st_size
    h.update(str(s).encode())
    with open(p, "rb") as f:
        h.update(f.read(262144)); f.seek(max(0, s - 262144)); h.update(f.read(262144))
    return h.hexdigest()


def _placeholders(d):
    try:
        out = subprocess.run(["ls", "-lO", str(d)], capture_output=True, text=True, timeout=60).stdout
        return sum(1 for l in out.splitlines() if "dataless" in l and l.lower().endswith(AUDIO))
    except Exception:
        return None


def check(verbose=True):
    srcs = [Path(s) for s in CFG["sources"] if Path(s).exists()]
    if ARCHIVE.exists():
        srcs += sorted(d for d in ARCHIVE.iterdir() if d.is_dir())
    files = [f for d in srcs for f in d.iterdir() if f.is_file() and f.suffix.lower() in AUDIO and f.stat().st_size >= 20_000]
    by = collections.defaultdict(list)
    for f in files:
        by[_fp(f)].append(f)
    dups = [v for v in by.values() if len(v) > 1]
    rows = db.rows("SELECT id, path, filename, status FROM recordings")
    missing = [r["path"] for r in rows if not Path(r["path"]).exists()]
    known = {Path(r["path"]).resolve() for r in rows}
    unknown = [f for f in files if f.resolve() not in known]
    twice = db.rows("SELECT group_concat(filename, ' | ') f FROM recordings GROUP BY recorded_at, round(duration) HAVING count(*) > 1")
    per_src = collections.Counter(next((str(s) for s in srcs if Path(r["path"]).parent == s), "elsewhere") for r in rows)
    ph = {str(d): _placeholders(d) for d in srcs}
    ok = not (dups or missing or unknown or twice)
    if verbose:
        print(f"  audio in sources: {len(files)} · distinct by content: {len(by)} · content duplicates: {len(dups)}")
        for v in dups[:10]:
            print("     dup:", " = ".join(str(p) for p in v))
        print(f"  database: {len(rows)} recordings · file missing: {len(missing)} · on disk but not in db: {len(unknown)} · ingested twice: {len(twice)}")
        for m in missing[:10]:
            print("     missing:", m)
        for u in unknown[:10]:
            print("     not in db (next `run` ingests it):", u)
        dc = [r for r in rows if r["status"] == "delete_candidate"]
        if dc:
            mb = sum(Path(r["path"]).stat().st_size for r in dc if Path(r["path"]).exists()) / 1e6
            print(f"  delete candidates: {len(dc)} file(s), {mb:.0f} MB waiting in _to_delete/ (nothing deleted — you empty it; `./vjs.sh sort --restore --all` puts them back)")
        for src, n in per_src.items():
            print(f"     {n:4d} from {src}")
        for d, n in ph.items():
            print(f"  iCloud-only placeholders in {Path(d).name}: {n}" + ("  ← Finder: right-click the folder → Keep Downloaded" if n else ""))
        print("  derived data:", ", ".join(f"{p.name} {sum(f.stat().st_size for f in p.rglob('*') if f.is_file()) / 1e9:.1f} GB"
                                         for p in [DATA / "clips", DATA / "wav", DATA / "emb"] if p.exists()),
              f"· studio.db {(DATA / 'studio.db').stat().st_size / 1e6:.0f} MB")
        g = gaps()
        print("  weeks with no recordings (last 10):", ", ".join(g) if g else "none")
        print("  " + backup_status())
        print("  ✓ every recording accounted for, once" if ok else "  ⚠ see above")
    return ok


# ---------------------------------------------------------------- feature #3 (2026-10-07): fingerprints, filing, gaps, backup
ARCHIVE = Path.home() / "Documents" / "Recordings" / "by-month"   # <YYYY-MM>/<original filename>


def backfill_fp():
    n = 0
    with db.tx() as c:
        for r in db.rows("SELECT id, path FROM recordings WHERE fp IS NULL"):
            p = Path(r["path"])
            if p.exists():
                c.execute("UPDATE recordings SET fp=? WHERE id=?", (_fp(p), r["id"])); n += 1
    return n


def known_copy(path: Path):
    """Same audio already ingested under another name (re-export / rename)? Returns that filename or None."""
    row = db.one("SELECT filename FROM recordings WHERE fp=?", _fp(path))
    return row["filename"] if row else None


def organize(apply=False, verbose=True):
    """Move processed recordings out of the Inbox and the May–Sep paste into by-month/<YYYY-MM>/, updating the database
    path in the same step. Only files the pipeline has finished with (status mined/skipped) move; the original top-level
    archive stays where it is (its legacy sidecar files sit next to it). Dry run unless apply=True."""
    roots = [Path.home() / "Desktop" / "Voice Memos Inbox",
             Path.home() / "Documents" / "Recordings" / "incoming" / "VoiceMemosMay13toSep24"]
    plan = []
    for r in db.rows("SELECT id, path, filename, recorded_at, status FROM recordings WHERE status IN ('mined','skipped')"):
        p = Path(r["path"])
        if p.parent not in roots or not p.exists():
            continue
        dest = ARCHIVE / r["recorded_at"][:7] / p.name
        plan.append((r["id"], p, dest))
    if apply:  # a re-copied memo that's already filed: if byte-identical, the Inbox copy goes to the Trash (recoverable)
        import filecmp
        inbox = roots[0]
        known = {r["filename"]: Path(r["path"]) for r in db.rows("SELECT filename, path FROM recordings")}
        for f in inbox.glob("*"):
            filed = known.get(f.name)
            if filed and filed != f and filed.exists() and filecmp.cmp(f, filed, shallow=False):
                f.rename(Path.home() / ".Trash" / f.name)
                if verbose:
                    print(f"  duplicate copy of {f.name} (already filed) → Trash")
    moved = 0
    for rid, src, dest in plan:
        if not apply:
            continue
        if dest.exists():
            if _fp(dest) != _fp(src):
                print(f"  ! {dest} exists with different content — left {src} alone")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)  # same volume: atomic
        with db.tx() as c:
            c.execute("UPDATE recordings SET path=? WHERE id=?", (str(dest), rid))
        moved += 1
    if verbose:
        months = collections.Counter(d.parent.name for _, _, d in plan)
        print(f"  {'moved' if apply else 'would move'} {moved if apply else len(plan)} recording(s) into {ARCHIVE}: "
              + ", ".join(f"{m} ({n})" for m, n in sorted(months.items())))
    return plan


def gaps(weeks=10):
    """Weeks with no recordings, recent first — 'did I forget to export?'"""
    import datetime as dt
    have = {r["w"] for r in db.rows("SELECT strftime('%Y-%W', recorded_at) w FROM recordings")}
    today = dt.date.today()
    out = []
    for i in range(weeks):
        d = today - dt.timedelta(weeks=i)
        if d.strftime("%Y-%W") not in have:
            out.append(f"week of {(d - dt.timedelta(days=d.weekday())).isoformat()}")
    return out


def backup_status():
    import re as _re
    try:
        t = Path("/Library/Backblaze.bzpkg/bzdata/bzreports/bzstat_lastbackupcompleted.xml").read_text()
        r = Path("/Library/Backblaze.bzpkg/bzdata/bzreports/bzstat_remainingbackup.xml").read_text()
        when = _re.search(r'localdatetime="(\d{8})(\d{4})', t)
        left = _re.search(r'remainingnumfilesforbackup="(\d+)"', r)
        return f"Backblaze last completed {when.group(1)[:4]}-{when.group(1)[4:6]}-{when.group(1)[6:]} {when.group(2)[:2]}:{when.group(2)[2:]}, {left.group(1)} file(s) waiting"
    except Exception:
        return "Backblaze status unreadable"
