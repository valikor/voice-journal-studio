"""./vjs.sh clean [--apply] — disk hygiene (2026-10-17). Dry run unless --apply. Never touches recordings or studio.db.
  - data/clips: playback cache; files untouched for CACHE_DAYS are removed (they are re-cut on demand).
  - data/whisper-server.log: append-only log; trimmed to its last LOG_KEEP_MB.
  - data/studio_backup_*.db: the newest KEEP_BACKUPS stay; older ones are MOVED to _to_delete/_db_backups/ (you empty that folder)."""
import time
from pathlib import Path

from .config import DATA
from .sortrec import TRASH

CACHE_DAYS, LOG_KEEP_MB, KEEP_BACKUPS = 14, 5, 4


def run(apply=False):
    out = {}
    cutoff = time.time() - CACHE_DAYS * 86400
    old = [f for f in (DATA / "clips").glob("*") if f.is_file() and f.stat().st_atime < cutoff and f.stat().st_mtime < cutoff]
    out["clips"] = {"files": len(old), "mb": round(sum(f.stat().st_size for f in old) / 1e6)}
    log = DATA / "whisper-server.log"
    busy = (DATA / "pipeline.lock").exists()   # the transcriber is writing to that log right now: leave it alone
    big = log.exists() and log.stat().st_size > LOG_KEEP_MB * 1e6 * 2 and not busy
    out["whisper_log"] = {"mb": round(log.stat().st_size / 1e6) if log.exists() else 0, "will_trim_to_mb": LOG_KEEP_MB if big else None}
    bks = sorted(DATA.glob("studio_backup_*.db"), key=lambda f: f.stat().st_mtime, reverse=True)
    move = bks[KEEP_BACKUPS:]
    out["backups"] = {"keep": [f.name for f in bks[:KEEP_BACKUPS]], "move_to_trash_folder": [f.name for f in move], "mb": round(sum(f.stat().st_size for f in move) / 1e6)}
    if apply:
        for f in old:
            f.unlink(missing_ok=True)
        if big:
            keep = LOG_KEEP_MB * 1_000_000
            with open(log, "rb") as fh:
                fh.seek(-keep, 2); tail = fh.read()
            log.write_bytes(tail[tail.find(b"\n") + 1:])
        if move:
            dest = TRASH / "_db_backups"; dest.mkdir(parents=True, exist_ok=True)
            for f in move:
                f.rename(dest / f.name)   # same volume; nothing is deleted
        out["applied"] = True
    return out
