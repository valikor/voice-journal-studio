#!/usr/bin/env python3
"""PII gate for a release tree. Exits 1 on any hit. Generic patterns are built in; names come from a PRIVATE denylist that is never committed:
  python3 scripts/pii_gate.py <dir> [--deny path/to/denylist.json]     (JSON list of case-insensitive strings; or env VJS_PII_DENY="a,b,c")
Checks text files AND filenames: home-directory paths, email addresses, phone numbers, IP addresses (non-loopback), private-key / API-key patterns,
real-looking domains from the denylist, plus every denylisted word."""
import json, os, re, sys
from pathlib import Path

PATTERNS = {
    "home path": re.compile(r"/Users/[A-Za-z0-9._-]+|/home/[a-z][a-z0-9_-]+|C:\\Users\\"),
    "email": re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"),
    "phone": re.compile(r"(?<!\d)(?:\+?1[ -.])?\(?\d{3}\)?[ -.]\d{3}[ -.]\d{4}(?!\d)"),
    "ip": re.compile(r"\b(?!127\.|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}\b"),
    "secret": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----|AKIA[0-9A-Z]{16}|ghp_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{10,}"),
}
ALLOW_EMAIL = re.compile(r"@(example\.(com|org)|users\.noreply\.github\.com)$")
ALLOW_FILES = {"LICENSE", "AUTHORS.md"}
ALLOW_PHRASES = ["Copyright (c) 2026 David Pritts", "MIT © 2026 David Pritts"]
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv"}
BIN = {".m4a", ".wav", ".mp3", ".png", ".jpg", ".jpeg", ".gif", ".woff", ".woff2", ".pdf", ".db", ".pyc", ".ico"}


def load_deny(arg):
    words = []
    if arg and Path(arg).exists():
        words += json.loads(Path(arg).read_text(encoding="utf-8"))
    if os.environ.get("VJS_PII_DENY"):
        words += [w.strip() for w in os.environ["VJS_PII_DENY"].split(",") if w.strip()]
    return [w.lower() for w in words if w]


def main():
    root = Path(sys.argv[1])
    deny = load_deny(sys.argv[sys.argv.index("--deny") + 1] if "--deny" in sys.argv else None)
    if not deny:
        print("WARNING: no name denylist given — only generic patterns are checked")
    hits = []
    for p in sorted(root.rglob("*")):
        if any(part in SKIP_DIRS for part in p.parts) or p.is_dir():
            continue
        rel = p.relative_to(root)
        if str(rel) in ALLOW_FILES:   # the author's own name belongs in the licence and the README byline
            continue
        low = str(rel).lower()
        for w in deny:
            if w in low:
                hits.append((str(rel), 0, f"denylisted word in filename: {w}"))
        if p.suffix.lower() in BIN:
            continue
        try:
            text = p.read_text(encoding="utf-8")
        except Exception:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for ph in ALLOW_PHRASES:   # the author byline (as in the licence) is intentional
                line = line.replace(ph, "")
            ll = line.lower()
            for w in deny:
                if w in ll:
                    hits.append((str(rel), n, f"denylisted word: {w}"))
            for name, rx in PATTERNS.items():
                for m in rx.finditer(line):
                    if name == "email" and ALLOW_EMAIL.search(m.group(0)):
                        continue
                    if name == "ip" and all(int(x) <= 255 for x in m.group(0).split(".")) is False:
                        continue
                    hits.append((str(rel), n, f"{name}: {m.group(0)[:40]}"))
    for h in hits[:200]:
        print(f"{h[0]}:{h[1]}  {h[2]}")
    print(f"PII gate: {'FAILED' if hits else 'passed'} ({len(hits)} hit(s))")
    sys.exit(1 if hits else 0)


if __name__ == "__main__":
    main()
