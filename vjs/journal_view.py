"""📖 Journal view (Library): everything that is on your-journal-site.example, read straight from the journal repo's entry files
(so it is complete and always in step with what the site is built from), plus the private context the site never shows:
for blocks that came through this app (<!-- vjs:m<ID> -->) the AI note, your private note, {curly} notes, lines you left out,
and the recording they came from. Entries imported from the original diary doc have no private context stored.
Also lists what is NOT on the site: private-only entry files and cards waiting to be published. Read-only."""
import re
from pathlib import Path

from . import db
from .config import CFG

BLOCK = re.compile(r"<!-- vjs:m(\d+) -->\n?(.*?)<!-- /vjs -->", re.S)
AUDIO = re.compile(r"[ \t]*<!-- vjs:audio [^>]*-->\n?")
CURLY = re.compile(r"\{([^{}]*)\}")


def _front(text):
    m = re.match(r"---\n(.*?)\n---\n?(.*)", text, re.S)
    if not m:
        return {}, text
    fm = {}
    for line in m.group(1).splitlines():
        k, _, v = line.partition(":")
        if _:
            fm[k.strip()] = v.split("#")[0].strip().strip('"')
    return fm, m.group(2)


def _moment(mid):
    m = db.one("SELECT * FROM moments WHERE id=?", mid)
    if not m:
        return None
    rec = db.one("SELECT filename, recorded_at, title FROM recordings WHERE id=?", m["rec_id"])
    lines = db.rows("SELECT l.speaker, l.text, l.include, s.start, s.end FROM moment_lines l LEFT JOIN segments s ON s.id=l.seg_id "
                    "WHERE l.moment_id=? ORDER BY l.ord", mid)
    inc = [l for l in lines if l["include"] and l["start"] is not None]
    curly = [c.strip() for l in lines for c in CURLY.findall(l["text"] or "") if c.strip()]
    hidden = [{"speaker": l["speaker"], "text": l["text"]} for l in lines if not l["include"] and (l["text"] or "").strip() and len((l["text"] or "").strip()) > 1]
    return {"id": mid, "status": m["status"], "star": m["star"], "published_at": m["published_at"], "entry_date": m["entry_date"],
            "ai_note": m["why"], "private_note": m["note_private"], "public_note": m["note_public"], "curly": curly, "hidden_lines": hidden,
            "rec": {"id": m["rec_id"], "filename": rec["filename"], "recorded_at": rec["recorded_at"], "title": rec["title"]},
            "clip": {"start": max(0, min(l["start"] for l in inc) - 0.4), "end": max(l["end"] for l in inc) + 0.6} if inc else None,
            "voice_on_site": bool(m["audio"]) and not m["audio_off"], "tags": m["tags"]}


def journal():
    repo = Path(CFG["journal_repo"]).expanduser()
    entries, n_moments, n_private_ctx = [], 0, 0
    for f in sorted((repo / "src" / "content" / "entries").glob("*/*.md")):
        fm, body = _front(f.read_text(encoding="utf-8"))
        aud = fm.get("audience", "")
        blocks, pos = [], 0
        for m in BLOCK.finditer(body):
            pre = body[pos:m.start()].strip()
            if pre:
                blocks.append({"kind": "imported", "text": AUDIO.sub("", pre)})
            blocks.append({"kind": "moment", "text": AUDIO.sub("", m.group(2)).strip(), "moment": _moment(int(m.group(1)))})
            pos = m.end()
        tail = body[pos:].strip()
        if tail:
            blocks.append({"kind": "imported", "text": AUDIO.sub("", tail)})
        n_moments += sum(b["kind"] == "moment" for b in blocks)
        on_site = "family" in aud or "friends" in aud or "public" in aud
        entries.append({"file": f.name, "year": f.parent.name, "date": fm.get("date"), "slug": fm.get("slug"), "heading": fm.get("heading"),
                        "undated": fm.get("undated") == "true", "audience": aud, "on_site": on_site, "blocks": blocks})
    private = []
    for f in sorted((repo / "private").glob("*.md")) if (repo / "private").exists() else []:
        txt = f.read_text(encoding="utf-8")
        private.append({"file": f.name, "text": AUDIO.sub("", re.sub(r"<!--.*?-->", "", txt, flags=re.S)).strip()})
    waiting = db.rows("SELECT id, status, entry_date FROM moments WHERE status IN ('ready','journal') ORDER BY entry_date")
    return {"entries": entries, "private_files": private,
            "totals": {"entries": len(entries), "on_site": sum(e["on_site"] for e in entries), "moments_from_app": n_moments,
                       "imported_blocks": sum(b["kind"] == "imported" for e in entries for b in e["blocks"]),
                       "private_only": sum(not e["on_site"] for e in entries) + len(private),
                       "ready_to_publish": sum(w["status"] == "ready" for w in waiting), "in_polish": sum(w["status"] == "journal" for w in waiting)}}
