"""Write approved moments into the family-journal Astro repo as house-style
markdown. Idempotent: each moment lives between <!-- vjs:m<ID> --> markers, so
re-publishing an edited moment replaces its block instead of duplicating it.
Nothing is deployed from here — build/deploy stays your existing step."""
import re
from datetime import datetime
from pathlib import Path

from . import db
from .config import CFG, DATA

EXPORTS = DATA / "exports"
EXPORTS.mkdir(exist_ok=True)


def moment_markdown(m, lines):
    body = [l["rendered"].strip() for l in lines if l["include"] and (l["rendered"] or "").strip()]
    if not body:
        return ""
    note = (m.get("note_public") or "").strip()
    if note:
        note = note.strip("[]* ")
        if not note.endswith((".", "!", "?", "。")):
            note += "."
        body[-1] += f" *[{note}]*"
    if m.get("star"):
        body[0] = "⭐ " + body[0]
    return "\n\n".join(body)  # the site renders one paragraph per blank-line-separated line


def _entry_path(date):
    repo = Path(CFG["journal_repo"])
    return repo / "src" / "content" / "entries" / date[:4] / f"{date}.md"


def _upsert_block(path: Path, date, mid, block):
    start, end = f"<!-- vjs:m{mid} -->", "<!-- /vjs -->"
    wrapped = f"{start}\n{block}\n{end}"
    if path.exists():
        text = path.read_text(encoding="utf-8")
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = f'---\ndate: {date}\naudience: [family]\nslug: "{date}"\n---\n'
    pat = re.compile(re.escape(start) + r".*?" + re.escape(end), re.S)
    if pat.search(text):
        text = pat.sub(lambda _: wrapped, text)
    else:
        text = text.rstrip("\n") + "\n\n" + wrapped + "\n"
    path.write_text(text, encoding="utf-8")


def preview(moment_id):
    m = db.one("SELECT * FROM moments WHERE id=?", moment_id)
    lines = db.rows("SELECT * FROM moment_lines WHERE moment_id=? ORDER BY ord", moment_id)
    return moment_markdown(m, lines)


NOTE_TO_SELF = re.compile(r"\{[^{}]*[}\]]|\{[^{}]*$")  # "{inaudible, delete it}", "{fix formatting]"


def unresolved(moment_id):
    """{…} is a note to yourself / to Claude — it must never reach the journal."""
    m = db.one("SELECT note_public FROM moments WHERE id=?", moment_id) or {}
    texts = [l["rendered"] or "" for l in db.rows("SELECT rendered FROM moment_lines WHERE moment_id=? AND include=1", moment_id)]
    return [t for t in texts + [m.get("note_public") or ""] if "{" in t]


def publish_ready():
    ready = db.rows(
        "SELECT m.*, r.recorded_at FROM moments m JOIN recordings r ON r.id=m.rec_id "
        "WHERE m.status='ready' ORDER BY coalesce(m.entry_date, substr(r.recorded_at,1,10)), m.seg_from")
    written, export = [], []
    blocked = []
    for m in ready:
        if unresolved(m["id"]):
            blocked.append(m["id"])
            continue
        date = m["entry_date"] or m["recorded_at"][:10]
        lines = db.rows("SELECT * FROM moment_lines WHERE moment_id=? ORDER BY ord", m["id"])
        block = moment_markdown(m, lines)
        if not block:
            continue
        path = _entry_path(date)
        _upsert_block(path, date, m["id"], block)
        with db.tx() as c:
            c.execute("UPDATE moments SET status='published', published_at=datetime('now','localtime'), publish_path=? WHERE id=?",
                      (str(path), m["id"]))
        try:  # her voice goes with the quote (family tier, on by default — the owner 2026-09-29)
            from .journal_audio import export as export_clip  # (named so it can't shadow the `export` list above)
            export_clip(m["id"], verbose=False)
        except Exception as e:
            print(f"    audio for m{m['id']} skipped: {e}")
        written.append({"id": m["id"], "date": date, "path": str(path)})
        export.append(f"**[{int(date[5:7])}/{int(date[8:10])}/{date[:4]}]**\n{block}\n")
    if export:
        out = EXPORTS / f"journal_additions_{datetime.now():%Y%m%d_%H%M%S}.md"
        out.write_text("\n".join(export), encoding="utf-8")
    if blocked:
        with db.tx() as c:  # send them back to Polish rather than publishing a note-to-self
            c.executemany("UPDATE moments SET status='journal' WHERE id=?", [(i,) for i in blocked])
    return written
