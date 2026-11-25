"""Applying the same edit twice must change nothing (the 2026-10-24 duplicate-lines bug: one split saved twice).
Runs against a COPY of the DB on port 8791.  ./.venv/bin/python tests/test_idempotent_edits.py"""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
tmp = tempfile.mkdtemp(); db = os.path.join(tmp, "t.db"); shutil.copy(os.path.join(ROOT, "data", "studio.db"), db)
srv = subprocess.Popen([os.path.join(ROOT, "vjs.sh"), "serve", "--no-open", "--port", "8791"], env={**os.environ, "VJS_DB": db}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
time.sleep(4)
post = lambda p, b: json.load(urllib.request.urlopen(urllib.request.Request("http://localhost:8791" + p, data=json.dumps(b).encode(), headers={"Content-Type": "application/json"}), timeout=30))
count = lambda rec: sqlite3.connect(db).execute("select count(*) from segments where rec_id=?", (rec,)).fetchone()[0]
bad = 0
try:
    c = sqlite3.connect(db)
    m = c.execute("select id, rec_id, seg_from from moments where status='pending' and seg_to-seg_from>=3 limit 1").fetchone()
    mid, rec = m[0], m[1]; seg = c.execute("select id from segments where rec_id=? and idx=?", (rec, m[2])).fetchone()[0]
    parts = [{"text": "alpha one", "speaker": "dad"}, {"text": "beta two", "speaker": "kid"}, {"text": "gamma three", "speaker": "dad"}]
    n0 = count(rec); post(f"/api/moment/{mid}/split", {"seg_id": seg, "parts": parts}); n1 = count(rec)
    post(f"/api/moment/{mid}/split", {"seg_id": seg, "parts": parts}); n2 = count(rec)
    ok = (n1 == n0 + 2 and n2 == n1); bad += not ok; print("split twice   ", "PASS" if ok else "FAIL", n0, n1, n2)
    post(f"/api/moment/{mid}/addline", {"after_seg_id": seg, "speaker": "kid", "text": "zeta missed line"}); n3 = count(rec)
    post(f"/api/moment/{mid}/addline", {"after_seg_id": seg, "speaker": "kid", "text": "zeta missed line"}); n4 = count(rec)
    ok = (n3 == n2 + 1 and n4 == n3); bad += not ok; print("addline twice ", "PASS" if ok else "FAIL", n2, n3, n4)
    post(f"/api/moment/{mid}/hide", {"seg_id": seg}); post(f"/api/moment/{mid}/hide", {"seg_id": seg})
    fl = sqlite3.connect(db).execute("select flags from segments where id=?", (seg,)).fetchone()[0] or ""
    ok = fl.count("dup") <= 1; bad += not ok; print("hide twice    ", "PASS" if ok else "FAIL", repr(fl))
finally:
    srv.terminate(); shutil.rmtree(tmp, ignore_errors=True)
print("ALL OK" if not bad else f"{bad} FAILED"); sys.exit(1 if bad else 0)
