#!/usr/bin/env python3
"""Synthetic demo recordings for the open-source release: fictional child "Pip" and a fictional parent, rendered with macOS `say`
(voices Junior = child-like, Fred = adult). No real people, no real audio. Writes demo/inbox/*.m4a with a gap between turns.
  python3 scripts/make_demo_audio.py <out_dir>"""
import subprocess, sys, tempfile
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "demo/inbox")
SCENES = {
    "2026-03-02 bedtime-dinosaurs": [
        ("Junior", "Daddy, I am a dinosaur and I am very hungry!"),
        ("Fred", "Oh no. What do dinosaurs eat for dinner?"),
        ("Junior", "Pancakes. Dinosaurs eat a thousand pancakes."),
        ("Fred", "A thousand? That is a lot of pancakes."),
        ("Junior", "And then I roar, and then I go to sleep."),
    ],
    "2026-03-09 the-moon-follows-us": [
        ("Junior", "Look, the moon is following our car!"),
        ("Fred", "It does look like it is following us."),
        ("Junior", "It is because the moon likes me. I am its best friend."),
        ("Fred", "I think that is the nicest reason I have heard."),
    ],
    "2026-03-15 sock-puppet-trial": [
        ("Fred", "Who ate the last banana?"),
        ("Junior", "Not me. It was the sock puppet."),
        ("Fred", "The sock puppet has no mouth."),
        ("Junior", "Yes it does. It is a very sneaky mouth."),
        ("Junior", "You cannot arrest him, he is only three socks tall."),
    ],
}
OUT.mkdir(parents=True, exist_ok=True)
for name, turns in SCENES.items():
    with tempfile.TemporaryDirectory() as td:
        parts = []
        for i, (voice, text) in enumerate(turns):
            a = Path(td) / f"{i}.aiff"
            subprocess.run(["say", "-v", voice, "-o", str(a), text], check=True)
            w = Path(td) / f"{i}.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-i", str(a), "-ar", "16000", "-ac", "1", str(w)], check=True)
            gap = Path(td) / f"g{i}.wav"
            subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.8", str(gap)], check=True)
            parts += [w, gap]
        lst = Path(td) / "l.txt"
        lst.write_text("".join(f"file '{p}'\n" for p in parts))
        subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c:a", "aac", "-b:a", "32k", str(OUT / f"{name}.m4a")], check=True)
        print("wrote", OUT / f"{name}.m4a")
