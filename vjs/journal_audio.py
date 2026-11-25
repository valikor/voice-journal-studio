"""Journal audio: every published quote gets a clip of the real recording on the family journal.

Decided by the owner 2026-09-29 (bus T-voice-journal-studio-20260929-1030-journal-audio-launch):
  A. family tier only   B. on by default (Polish can switch it off)   C. every voice in a kept moment is OK
  D. backfill the quotes already live.

The clip runs from the first included line to the last (continuous — what was actually said), loudness-
normalised so clips don't jump in volume, with short fades. It lands in the journal repo's public/audio/.

Where the clip is listed (changed 2026-09-29 after reading the site): NOT inside the entry markdown — the same entry
file renders on family, friends and public pages, so an in-block marker would put the clip URL into non-family HTML
and the site's scene-card script would also split on it. Instead the clips are listed in a sidecar manifest,
family-journal/src/data/journal-audio.json, keyed by moment id; only family pages import it and place a player in
the card that starts at the matching <!-- vjs:m<ID> --> marker. `lines` = start of each rendered line (tap-a-line later).
Nothing is deployed from here."""
import hashlib
import json
import re
import subprocess
from pathlib import Path

from . import db
from .config import CFG

VERSION = 1  # bump when the encoding changes → new filenames (cache-busting)
MARK = re.compile(r"[ \t]*<!-- vjs:audio [^>]*-->\n?")


def _span(m):
    all_ = db.rows("SELECT l.include, l.text, l.rendered, s.start, s.end FROM moment_lines l JOIN segments s ON s.id=l.seg_id "
                   "WHERE l.moment_id=? ORDER BY l.ord", m["id"])
    shown = [r for r in all_ if r["include"] and (r["rendered"] or "").strip()]
    # a hidden line whose words were merged into a shown line ("…marble" + "run.") is still part of the audio
    merged = [r for r in all_ if not r["include"] and (r["text"] or "").strip()
              and any((r["text"] or "").strip().rstrip(".") in (x["rendered"] or "") for x in shown)]
    if shown:
        a0, b0 = min(r["start"] for r in shown + merged), max(r["end"] for r in shown + merged)
        a, b = max(0.0, a0 - 0.4), b0 + 0.6
        return a, b, [round(max(0.0, r["start"] - a), 1) for r in shown]
    rows = []
    if not rows:
        rows = db.rows("SELECT start, end FROM segments WHERE rec_id=? AND idx BETWEEN ? AND ? "
                       "AND coalesce(flags,'') IN ('','media') ORDER BY idx", m["rec_id"], m["seg_from"], m["seg_to"])
    if not rows:
        return None
    a = max(0.0, min(r["start"] for r in rows) - 0.4)
    b = max(r["end"] for r in rows) + 0.6
    return a, b, [round(max(0.0, r["start"] - a), 1) for r in rows]


def _audience(entry: Path):
    head = entry.read_text(encoding="utf-8").split("---", 2)[1] if entry.exists() else ""
    m = re.search(r"^audience:\s*\[([^\]]*)\]", head, re.M)
    return {x.strip() for x in m.group(1).split(",")} if m else set()


def _encode(src, a, b, out: Path):
    dur = b - a
    af = (f"loudnorm=I=-18:TP=-1.5:LRA=11,aresample=44100,"
          f"afade=t=in:st=0:d=0.12,afade=t=out:st={max(0.0, dur - 0.12):.2f}:d=0.12")
    tmp = out.with_suffix(".tmp.m4a")
    r = subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-ss", f"{a:.2f}", "-t", f"{dur:.2f}", "-i", str(src),
                        "-ac", "1", "-af", af, "-c:a", "aac", "-b:a", "48k", "-movflags", "+faststart", str(tmp)],
                       capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180)
    if r.returncode != 0 or not tmp.exists():
        raise RuntimeError(f"ffmpeg: {r.stderr[-300:]}")
    tmp.replace(out)


def _set_marker(entry: Path, mid, marker):
    """Put (or replace/remove) the audio marker right after the moment's start marker; nothing else in the file changes."""
    text = entry.read_text(encoding="utf-8")
    start = f"<!-- vjs:m{mid} -->\n"
    i = text.find(start)
    if i < 0:
        return False
    j = i + len(start)
    rest = MARK.sub("", text[j:], count=1) if MARK.match(text[j:]) else text[j:]
    new = text[:j] + (marker + "\n" if marker else "") + rest
    if new != text:
        entry.write_text(new, encoding="utf-8")
    return True


def _manifest_path():
    return Path(CFG["journal_repo"]) / "src" / "data" / "journal-audio.json"


def _manifest():
    p = _manifest_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"about": "", "clips": {}}


def _save_manifest(man):
    man["about"] = ("Written by voice-journal-studio. Family tier ONLY (the owner, decision A, 2026-09-29): import this on "
                    "/family/ pages only. Key = moment id from <!-- vjs:m<ID> -->. Do not hand-edit.")
    man["clips"] = dict(sorted(man["clips"].items(), key=lambda kv: int(kv[0])))
    p = _manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(man, ensure_ascii=False, indent=1) + "\n"
    if not p.exists() or p.read_text(encoding="utf-8") != text:
        p.write_text(text, encoding="utf-8")


def export(mid, verbose=True, man=None):
    """Clip + marker for one published moment. Returns the site path, or None (and removes any old marker)."""
    m = db.one("SELECT m.*, r.path src FROM moments m JOIN recordings r ON r.id=m.rec_id WHERE m.id=?", mid)
    if not m or m["status"] != "published" or not m["publish_path"]:
        return None
    entry = Path(m["publish_path"])
    public = Path(CFG["journal_repo"]) / "public"
    own = man is None
    man = man if man is not None else _manifest()
    _set_marker(entry, mid, None)  # retire the old in-block marker (see module doc)
    if m["audio_off"] or "family" not in _audience(entry):
        man["clips"].pop(str(mid), None)
        if own:
            _save_manifest(man)
        _drop(mid, public, keep=None)
        with db.tx() as c:
            c.execute("UPDATE moments SET audio=NULL WHERE id=?", (mid,))
        return None
    sp = _span(m)
    if not sp:
        return None
    a, b, lines = sp
    date = entry.stem
    h = hashlib.sha1(f"{m['rec_id']}|{a:.2f}|{b:.2f}|v{VERSION}".encode()).hexdigest()[:6]
    rel = f"audio/{date[:4]}/{date}-m{mid}-{h}.m4a"
    out = public / rel
    out.parent.mkdir(parents=True, exist_ok=True)
    if not out.exists():
        _encode(m["src"], a, b, out)
    _drop(mid, public, keep=out)
    if f"<!-- vjs:m{mid} -->" not in entry.read_text(encoding="utf-8"):
        # the quote was removed from the journal by hand (the owner's call) → its clip goes too, and the moment is marked removed
        man["clips"].pop(str(mid), None)
        if own:
            _save_manifest(man)
        _drop(mid, public, keep=None)
        with db.tx() as c:
            c.execute("UPDATE moments SET status='removed', audio=NULL WHERE id=?", (mid,))
        if verbose:
            print(f"    m{mid}: quote no longer in {entry.name} → clip removed, moment marked 'removed'")
        return None
    man["clips"][str(mid)] = {"entry": entry.stem, "src": "/" + rel, "dur": round(b - a, 1), "lines": lines}
    if own:
        _save_manifest(man)
    with db.tx() as c:
        c.execute("UPDATE moments SET audio=? WHERE id=?", ("/" + rel, mid))
    return "/" + rel


def _drop(mid, public, keep):
    """Old clips of this moment (a re-cut changes the hash) — ours to remove."""
    for f in (public / "audio").glob(f"*/*-m{mid}-*.m4a"):
        if keep is None or f != keep:
            f.unlink()


def export_all(verbose=True):
    done = skipped = 0
    man = _manifest()
    live = {str(m["id"]) for m in db.rows("SELECT id FROM moments WHERE status='published'")}
    for k in [k for k in man["clips"] if k not in live]:  # un-published since → drop from the list
        man["clips"].pop(k)
    for m in db.rows("SELECT id FROM moments WHERE status='published' ORDER BY id"):
        try:
            if export(m["id"], verbose, man=man):
                done += 1
            else:
                skipped += 1
        except Exception as e:
            skipped += 1
            print(f"    m{m['id']}: {e}")
    _save_manifest(man)
    if verbose:
        print(f"  journal audio: {done} clip(s) ready, {skipped} without audio → {_manifest_path()}")
    return done
