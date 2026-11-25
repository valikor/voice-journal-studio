"""ASR on top of whisper.cpp.

Differences from the old batch scripts (transcribe_v3/v4):
  * Silero VAD first, then speech is packed into <=25 s chunks and each chunk is
    decoded separately with language auto-detect. whisper only detects language
    once per decode, so per-chunk decoding is what lets a Mandarin sentence that
    follows an English one come out as Mandarin instead of hallucinated English.
  * No cross-chunk context → the long repetition loops can't propagate.
  * whisper-server keeps large-v3 resident, so review-time re-decodes of a
    single clip (forced zh / forced en) take ~1-2 s.
"""
import io
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np
import requests
import soundfile as sf

from .config import CFG, DATA

W = CFG["whisper"]
BASE = f"http://{W['host']}:{W['port']}"
SR = 16000

HALLUCINATIONS = re.compile(
    r"(amara\.org|字幕由|字幕志愿者|请不吝点赞|订阅.*转发|thank(s)? for watching|"
    r"please subscribe|like and subscribe|明镜与点点|优优独播剧场|中文字幕|by bwd6|"
    r"谢谢(大家的?)?(收看|观看|支持)|感谢(您的)?(收看|观看)|请订阅|点赞订阅|下期再见|欢迎(收看|订阅)|订阅我的频道|"
    r"youtube\s*频道|diolch|wylio|fideo|merci d'avoir regardé|ご視聴|продолжение следует|субтитры|"
    r"subtitles by|transcribed by|www\.\w+\.(com|org)|"
    r"^\s*[*\[(（【]\s*[^*\])）】]{1,12}\s*[*\])）】]\s*$)",  # sound labels without a prompt: "* 声音 *", "* 哭声 *", "[music]"
    re.I,
)


def server_up():
    try:
        requests.get(BASE + "/", timeout=2)
        return True
    except Exception:
        return False


def ensure_server(wait=180):
    if server_up():
        return
    model = W["model"]
    if not os.path.exists(model):
        raise RuntimeError(f"whisper model not found: {model}")
    log = open(DATA / "whisper-server.log", "ab")
    subprocess.Popen(
        [W["server_bin"], "-m", model, "--host", W["host"], "--port", str(W["port"]),
         "-t", str(W["threads"]), "-l", "auto"],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
    )
    t0 = time.time()
    while time.time() - t0 < wait:
        if server_up():
            return
        time.sleep(1.5)
    raise RuntimeError("whisper-server did not come up; see data/whisper-server.log")


def vad_spans(wav_path):
    """Speech spans (sec) from Silero VAD."""
    out = subprocess.run(
        ["whisper-vad-speech-segments", "-np", "-vm", W["vad_model"], "-f", str(wav_path),
         # 0.3 dropped about half of real speech (short, high kid utterances); 0.12 keeps it
         "--vad-threshold", "0.12", "--vad-min-speech-duration-ms", "100",
         "--vad-min-silence-duration-ms", "400", "--vad-speech-pad-ms", "350",
         "--vad-max-speech-duration-s", "24"],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    ).stdout
    if "Detected" not in out:
        raise RuntimeError("VAD produced no output (check whisper-vad-speech-segments flags / model path)")
    spans = []
    for m in re.finditer(r"start = ([\d.]+), end = ([\d.]+)", out):
        spans.append((float(m.group(1)) / 100.0, float(m.group(2)) / 100.0))
    return spans


def pack_chunks(spans, max_len=25.0, max_gap=1.2):
    chunks = []
    for s, e in spans:
        if chunks and s - chunks[-1][1] <= max_gap and e - chunks[-1][0] <= max_len:
            chunks[-1][1] = e
        else:
            chunks.append([s, e])
    return chunks


def _post(samples, language="auto", prompt="", temperature=0.0, fmt="verbose_json"):
    buf = io.BytesIO()
    sf.write(buf, samples, SR, format="WAV", subtype="PCM_16")
    buf.seek(0)
    data = {"response_format": fmt, "language": language, "temperature": str(temperature),
            "temperature_inc": "0.2"}
    if W.get("beam_size"):
        data["beam_size"] = str(W["beam_size"])
    if prompt:
        data["prompt"] = prompt
    r = requests.post(BASE + "/inference", files={"file": ("a.wav", buf, "audio/wav")}, data=data, timeout=600)
    r.raise_for_status()
    return r.json()


def _clean(text):
    t = re.sub(r"\s+", " ", (text or "").replace("[BLANK_AUDIO]", "")).strip()
    t = re.sub(r"^[-–—]\s*", "", t)  # whisper's speaker-turn dashes
    # whisper sometimes emits the same sentence twice inside one segment
    m = re.fullmatch(r"(.{4,}?)[\s,，。.]*\1[\s,，。.]*", t)
    return m.group(1).strip() + (t[-1] if t[-1] in ".。!?！？," and not m.group(1).endswith(t[-1]) else "") if m else t


def transcribe_wav(wav_path, prompt="", progress=None):
    ensure_server()
    audio, sr = sf.read(str(wav_path), dtype="float32")
    assert sr == SR
    chunks = pack_chunks(vad_spans(wav_path))
    segs = []
    for i, (cs, ce) in enumerate(chunks):
        a = audio[int(cs * SR): int(ce * SR)]
        if len(a) < SR * 0.3:
            continue
        # pad so whisper sees a little silence around short utterances
        a = np.concatenate([np.zeros(int(0.2 * SR), np.float32), a, np.zeros(int(0.3 * SR), np.float32)])
        try:
            res = _post(a, "auto", prompt)
        except Exception as e:  # one bad chunk shouldn't kill a 2-hour file
            print(f"    chunk {i} failed: {e}")
            continue
        lang = res.get("language") or res.get("detected_language")
        for s in res.get("segments", []):
            text = _clean(s.get("text"))
            if not text:
                continue
            st = cs + max(0.0, float(s.get("start", 0)) - 0.2)
            en = cs + max(0.0, float(s.get("end", 0)) - 0.2)
            segs.append({
                "start": round(st, 2), "end": round(min(en, ce + 0.3), 2), "text": text,
                "avg_logprob": s.get("avg_logprob"), "no_speech": s.get("no_speech_prob"),
                "asr_lang": (lang or "")[:12],
            })
        if progress and i % 25 == 0:
            progress(i, len(chunks))
    return _flag(segs)


def _flag(segs):
    """Mark loops / known whisper hallucinations instead of silently keeping them."""
    out = []
    run = 0
    for i, s in enumerate(segs):
        flags = []
        if i and s["text"] == segs[i - 1]["text"]:
            run += 1
        else:
            run = 0
        if run >= 2:
            flags.append("loop")
        if HALLUCINATIONS.search(s["text"]):
            flags.append("halluc")
        if (s.get("no_speech") or 0) > 0.8 and (s.get("avg_logprob") or 0) < -0.9:
            flags.append("nospeech")
        s["flags"] = ",".join(flags)
        out.append(s)
    return out


def _tuned(samples, language="auto"):
    """Re-decode with the fine-tuned model (vjs/ft_server.py, config "asr_tuned_url", e.g. http://127.0.0.1:8179) when it is configured
    and up; None falls back to the stock whisper-server. Clip re-decodes only: the tuned server returns text, not timestamps."""
    from .config import CFG
    url = (CFG.get("asr_tuned_url") if isinstance(CFG, dict) else None)
    if not url:
        return None
    try:
        buf = io.BytesIO(); sf.write(buf, samples, SR, format="WAV", subtype="PCM_16")
        r = requests.post(url + "/inference", params={"language": language or "auto"}, data=buf.getvalue(), timeout=120)
        return _clean(r.json().get("text", ""))
    except Exception:
        return None


def decode_clip(src_path, start, end, language, prompt="", with_probs=False):
    """Re-decode one span of the original recording with a forced language.
    with_probs=True also returns whisper's language probabilities for the clip."""
    from .audio import clip
    ensure_server()
    wav = clip(src_path, max(0, start - 0.25), end + 0.35, fmt="wav")
    a, _ = sf.read(str(wav), dtype="float32")
    if len(a) < SR * 0.2:
        return ""
    a = np.concatenate([np.zeros(int(0.2 * SR), np.float32), a, np.zeros(int(0.3 * SR), np.float32)])
    tuned = None if with_probs else _tuned(a, language)
    if tuned is not None:
        return tuned
    res = _post(a, language, prompt, fmt="verbose_json")
    text = _clean(" ".join(s.get("text", "") for s in res.get("segments", []))) or _clean(res.get("text"))
    if with_probs:
        return text, res.get("language_probabilities") or {}
    return text


COMMON_EN = set("""a an the and or but so to of in on at for with from by is am are was were be been do does did have has had
not no yes yeah okay ok oh i you he she it we they me my your his her its our their this that these those what who where when
why how here there now then up down out over under again one two three dad daddy mom mommy mama papa baby go going gonna got
get let lets look see say said want wanna know think like good right left big little come came make made can could will would
should thing things one ones some any all just really very too also well ya gotta""".split())


def is_vocabulary(right, wrong=""):
    """Only household vocabulary belongs in whisper's prompt — Chinese words, names/places ("Kid's Cove"),
    kid words ("stuffies") — not sentence rewrites ("Yeah" → "Ya gotta", "Daddy" → "Dad")."""
    import re
    if re.search(r"[\u4e00-\u9fff]", right):
        return len(right) <= 8
    words = re.findall(r"[A-Za-z']+", right)
    if not words or len(words) > 3:
        return False
    if all(w.lower() in COMMON_EN for w in words):
        return False
    if any(w[0].isupper() and w.lower() not in COMMON_EN for w in words):
        return True  # a name or place
    return len(words) == 1  # a single unusual word ("stuffies", "muscles")


def asr_prompt():
    """Whisper's initial prompt. OFF by default: the 2026-09-24 bake-off and the blind quality check both
    found transcripts at least as good without it, and it once pushed a clip into hallucinating
    "生日快乐!". Set whisper.use_prompt=true in config.local.json to turn it back on."""
    if not W.get("use_prompt", False):
        return ""
    return vocabulary_prompt()


def vocabulary_prompt():
    """Configured household vocabulary + words the owner has taught by correcting transcripts."""
    from . import db
    base = CFG.get("asr_prompt", "")
    try:
        from .names import prompt_terms
        base = (base + " " + prompt_terms()).strip()  # every listed spelling of the family's names (config.local.json)
    except Exception:
        pass
    try:
        learned = [r["right"] for r in db.rows("SELECT right, wrong FROM lexicon WHERE length(right) BETWEEN 2 AND 30 "
                                               "ORDER BY n DESC, last DESC LIMIT 200") if is_vocabulary(r["right"], r["wrong"])][:30]
    except Exception:
        learned = []
    extra = ", ".join(dict.fromkeys(w for w in learned if w not in base))
    return (base + (" " + extra + "." if extra else ""))[:700]
