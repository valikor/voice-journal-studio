"""Regression set for note-vs-card consistency (the owner's 2026-10-25 examples). Run: ./.venv/bin/python tests/test_notes.py"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from vjs import notes

L = lambda *rows: [{"speaker": s, "text": t} for s, t in rows]
G = notes.generic_vocab()
cases = [
    # (name, note, card lines, should_pass)
    ("coffee: quote attributed to her was Dad's",
     "She says 'Black and brown coffee!' followed by 'Don't press that button.' — a sudden shift into a dramatic, faux-urgent tone, likely mimicking a video game or cartoon.",
     L(("kid", "Black and brown coffee!"), ("dad", "Don't press that button.")), False),
    ("floating thing: Mandarin claimed over English + 'confidently'",
     "She confidently declares 'Now I'm on the floating thing' in Mandarin, then switches to English to assert ownership of her phone.",
     L(("kid", "Now I'm on the floating thing."), ("dad", "Yeah, it works."), ("kid", "I take my phone with me.")), False),
    ("smiling: quoted Chinese and 'small face' are not on the card",
     "Asks to play together politely ('我可以和你一起玩嗎?') then immediately switches to singing/describing a 'small face' in Chinese.",
     L(("kid", "Let's do the smiling.")), False),
    ("control: extractive note passes", "Her words: “Let's do the smiling.” (English).", L(("kid", "Let's do the smiling.")), True),
    ("control: faithful note passes", "She asks Dad to play and says 'Let's do the smiling.'", L(("kid", "Let's do the smiling."), ("dad", "Okay.")), True),
    ("control: real Chinese claim passes", "She says '我吃饱了' in Chinese after eating.", L(("kid", "我吃饱了"), ("dad", "Good.")), True),
]
bad = 0
for name, note, lines, want in cases:
    ok, why = notes.verify(note, lines, G)
    status = "PASS" if ok == want else "FAIL"
    bad += ok != want
    print(f"{status}  {name}  -> verifier says {'ok' if ok else 'reject'} {why if why else ''}")
print("ALL OK" if not bad else f"{bad} FAILED")
sys.exit(1 if bad else 0)
