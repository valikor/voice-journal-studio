"""Speaker guess per segment.

Stage 1 (always available): pitch bands — a preschooler's F0 sits well above
adult voices. Same idea as the old pitch_tag.py, but computed per ASR segment
and stored as features.

Stage 2 (once you've corrected ~40 speakers in review): k-nearest-neighbour on
pitch + spectral features using *your* labels, so the guesses improve as you
review.
"""
import numpy as np
import soundfile as sf

from . import db
from .config import CFG

SR = 16000


def features(audio, start, end):
    import librosa
    a = audio[int(start * SR): int(end * SR)]
    if len(a) < int(0.25 * SR):
        return None
    f0 = librosa.yin(a, fmin=70, fmax=650, sr=SR, frame_length=1024, hop_length=256)
    rms = librosa.feature.rms(y=a, frame_length=1024, hop_length=256)[0]
    n = min(len(f0), len(rms))
    f0, rms = f0[:n], rms[:n]
    voiced = (rms > max(0.01, np.percentile(rms, 60) * 0.5)) & (f0 > 75) & (f0 < 640)
    cent = librosa.feature.spectral_centroid(y=a, sr=SR, n_fft=1024, hop_length=256)[0][:n]
    if voiced.sum() < 4:
        return {"f0_med": None, "f0_p90": None, "centroid": float(np.mean(cent)), "voiced_frac": float(voiced.mean())}
    v = f0[voiced]
    return {
        "f0_med": float(np.median(v)),
        "f0_p90": float(np.percentile(v, 90)),
        "centroid": float(np.mean(cent[voiced])),
        "voiced_frac": float(voiced.mean()),
    }


def band_guess(f):
    p = CFG["pitch"]
    if not f or not f.get("f0_med"):
        return "other", 0.2
    x = f["f0_med"]
    if x >= p["kid_min"]:
        return "kid", min(0.95, 0.55 + (x - p["kid_min"]) / 150)
    if x >= p["adult_female_min"]:
        return "mom", min(0.9, 0.5 + min(x - p["adult_female_min"], p["kid_min"] - x) / 100)
    return "dad", min(0.95, 0.55 + (p["adult_female_min"] - x) / 80)


class KNN:
    def __init__(self):
        lab = db.rows(
            "SELECT spk, f0_med, f0_p90, centroid FROM segments "
            "WHERE spk IS NOT NULL AND spk!='other' AND f0_med IS NOT NULL"
        )
        self.ok = len(lab) >= 40 and len({r["spk"] for r in lab}) >= 2
        if not self.ok:
            return
        X = np.array([self._vec(r) for r in lab])
        self.mu, self.sd = X.mean(0), X.std(0) + 1e-6
        self.X = (X - self.mu) / self.sd
        self.y = [r["spk"] for r in lab]

    @staticmethod
    def _vec(r):
        return [np.log(r["f0_med"]), np.log(r["f0_p90"] or r["f0_med"]), (r["centroid"] or 1500) / 1000.0]

    def predict(self, f, k=7):
        if not self.ok or not f or not f.get("f0_med"):
            return None
        q = (np.array(self._vec(f)) - self.mu) / self.sd
        d = np.linalg.norm(self.X - q, axis=1)
        idx = np.argsort(d)[:k]
        votes = {}
        for i in idx:
            votes[self.y[i]] = votes.get(self.y[i], 0) + 1.0 / (d[i] + 0.1)
        best = max(votes, key=votes.get)
        return best, votes[best] / sum(votes.values())


def analyze(rec_id, wav_path):
    audio, sr = sf.read(str(wav_path), dtype="float32")
    knn = KNN()
    segs = db.rows("SELECT id, start, end FROM segments WHERE rec_id=? ORDER BY idx", rec_id)
    kid = 0
    work = (db.one("SELECT category FROM recordings WHERE id=?", rec_id) or {}).get("category") == "work"
    updates = []  # compute first, write in one short transaction (keeps the review app responsive)
    for s in segs:
        f = features(audio, s["start"], s["end"]) or {}
        g = knn.predict(f) or band_guess(f)
        if work and g[0] == "kid":
            g = ("other", 0.3)  # work calls: a high voice is a colleague, not her
        if g[0] == "kid":
            kid += 1
        updates.append((f.get("f0_med"), f.get("f0_p90"), f.get("centroid"), f.get("voiced_frac"), g[0], round(g[1], 3), s["id"]))
    with db.tx() as c:
        c.executemany(
            "UPDATE segments SET f0_med=?, f0_p90=?, centroid=?, voiced_frac=?, spk_guess=?, spk_conf=? WHERE id=?", updates)
        c.execute("UPDATE recordings SET kid_segments=? WHERE id=?", (kid, rec_id))
    return kid


def reguess_all():
    """Re-apply the (retrained) model to every unconfirmed segment."""
    knn = KNN()
    if not knn.ok:
        return 0
    n = 0
    with db.tx() as c:
        for s in db.rows("SELECT id, f0_med, f0_p90, centroid FROM segments WHERE spk IS NULL AND f0_med IS NOT NULL"):
            g = knn.predict(s)
            if g:
                c.execute("UPDATE segments SET spk_guess=?, spk_conf=? WHERE id=?", (g[0], round(g[1], 3), s["id"]))
                n += 1
    return n
