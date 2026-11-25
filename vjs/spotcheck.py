"""Listen-and-judge spot checks in the terminal. Single keypresses, no Enter.

  ./vjs.sh spotcheck foreign   lines hidden as another language / junk — did we hide real family speech?
  ./vjs.sh spotcheck halluc    lines hidden as "thanks for watching"-type filler — same question
  ./vjs.sh spotcheck repeat    lines hidden as replayed media (audiobook, show, book)
  ./vjs.sh spotcheck kept      random lines that passed every filter — did junk slip through?
  ./vjs.sh spotcheck kid       random lines counted as hers in the language log — is it really her?

Stop any time with q or Ctrl-C; what you've judged so far is saved and summarised.
Verdicts go to data/spotcheck.tsv. Nothing in the database changes.
"""
import csv
import signal
import subprocess
import sys
import termios
import tty
from datetime import datetime

from . import db
from .audio import clip
from .config import DATA

# For hidden lines the only question that matters: did we hide real people talking?
HIDDEN_KEYS = {
    "n": ("noise", "nothing / noise / gibberish — hiding it is right"),
    "m": ("media", "song, TV, video, audiobook — hiding it is right"),
    "h": ("HER", "her, really talking — should NOT have been hidden"),
    "a": ("ADULT", "a parent / real person really talking — should NOT have been hidden"),
}
MODES = {
    "foreign": ("s.flags LIKE '%foreign%'", HIDDEN_KEYS, "What is this really?"),
    "halluc": ("s.flags LIKE '%halluc%'", HIDDEN_KEYS, "What is this really?"),
    "repeat": ("s.flags LIKE '%repeat%'", HIDDEN_KEYS, "What is this really?"),
    "kept": ("coalesce(s.flags,'')=''", {
        "y": ("ok", "real speech, text roughly right"),
        "t": ("bad-text", "real speech, but the text is badly wrong"),
        "j": ("JUNK", "noise / media / invented text — should have been hidden"),
    }, "Is this real speech with roughly the right words?"),
    "kid": ("coalesce(s.flags,'') IN ('','media') AND coalesce(s.spk, s.spk_guess)='kid' AND coalesce(s.adult_like,0)=0", {
        "y": ("her", "her, words roughly right"),
        "t": ("her-bad-text", "her, but the text is badly wrong"),
        "a": ("NOT-HER", "an adult / someone else"),
        "j": ("JUNK", "media / noise"),
    }, "Is this her talking?"),
}
BAD = {"HER", "ADULT", "JUNK", "NOT-HER"}
LOG = DATA / "spotcheck.tsv"


def _getch():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        return sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _play(path, start, end):
    return subprocess.Popen(["afplay", str(clip(path, max(0, start), end))],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(mode, n=15):
    if mode not in MODES:
        print("modes:", ", ".join(MODES))
        return
    where, keys, question = MODES[mode]
    rows = db.rows(
        "SELECT s.id, s.start, s.end, s.text, s.flags, coalesce(s.spk, s.spk_guess) spk, r.path, r.filename, r.recorded_at "
        f"FROM segments s JOIN recordings r ON r.id=s.rec_id WHERE {where} ORDER BY random() LIMIT ?", n)
    total = db.one(f"SELECT count(*) n FROM segments s WHERE {where}")["n"]
    menu = "   ".join(f"[{k}] {desc}" for k, (_, desc) in keys.items())
    print(f"\n{mode}: {total} lines in this bucket — {len(rows)} random ones.  {question}\n"
          f"  {menu}\n  [r] replay   [c] replay with 5 s around it   [s] skip   [q] stop & save\n")
    results, p = [], None
    stop = {"now": False}
    signal.signal(signal.SIGINT, lambda *_: stop.update(now=True))
    new = not LOG.exists()
    with open(LOG, "a", newline="") as fh:
        w = csv.writer(fh, delimiter="\t")
        if new:
            w.writerow(["when", "mode", "seg_id", "verdict", "file", "start", "text"])
        for i, s in enumerate(rows, 1):
            if stop["now"]:
                break
            print(f"[{i}/{len(rows)}] {s['recorded_at'][:10]} @ {int(s['start'] // 60)}:{int(s['start'] % 60):02d}  "
                  f"“{s['text'][:90]}”")
            p = _play(s["path"], s["start"] - 0.3, s["end"] + 0.4)
            while not stop["now"]:
                try:
                    k = _getch().lower()
                except Exception:
                    k = "q"
                if k in ("r", "c"):
                    p.terminate()
                    pad = 5 if k == "c" else 0.3
                    p = _play(s["path"], s["start"] - pad, s["end"] + pad + 0.1)
                    continue
                if k in keys or k in ("s", "q"):
                    break
            p.terminate()
            if stop["now"] or k == "q":
                break
            if k == "s":
                print("      skipped")
                continue
            verdict = keys[k][0]
            print(f"      → {verdict}")
            results.append(verdict)
            w.writerow([datetime.now().isoformat(timespec="seconds"), mode, s["id"], verdict, s["filename"], s["start"], s["text"]])
            fh.flush()
    if p:
        p.terminate()
    if results:
        bad = sum(v in BAD for v in results)
        counts = ", ".join(f"{v} {results.count(v)}" for v in dict.fromkeys(results))
        print(f"\n{mode}: judged {len(results)} — {counts}")
        print(f"  filter mistakes: {bad}/{len(results)} ({100 * bad // len(results)}%)   log: {LOG}")
    else:
        print("\nnothing judged")
