"""Local LLM (LM Studio / Qwen) — structured JSON calls only. Nothing leaves the Mac."""
import json
import os
import subprocess
import time

import requests

from .config import CFG

L = CFG["llm"]


def _loaded():
    try:
        r = requests.get(L["base_url"].replace("/v1", "/api/v0") + "/models", timeout=5).json()
        return any(m.get("id") == L["model"] and m.get("state") == "loaded" for m in r.get("data", []))
    except Exception:
        return False


def ensure_ready():
    lms = os.path.expanduser(L["lms_bin"])
    try:
        requests.get(L["base_url"] + "/models", timeout=3)
    except Exception:
        subprocess.run([lms, "server", "start"], capture_output=True, timeout=60)
    if not _loaded():
        r = subprocess.run([lms, "load", L["model"], "--context-length", "16384", "-y"],
                           capture_output=True, timeout=600)
        if not _loaded():  # 36 GB can't hold two big models + whisper: make room, then load ours
            subprocess.run([lms, "unload", "--all"], capture_output=True, timeout=300)
            subprocess.run([lms, "load", L["model"], "--context-length", "16384", "-y"], capture_output=True, timeout=600)
    if not _loaded():
        raise RuntimeError(f"LM Studio model {L['model']} is not loaded")


def chat_json(system, user, schema, max_tokens=2500, temperature=0.2, retries=2):
    body = {
        "model": L["model"],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "reasoning_effort": "none",   # Qwen3.5 thinking otherwise eats the budget; with a json_schema LM Studio returns empty content
        "response_format": {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    last = None
    for attempt in range(retries + 1):
        try:
            r = requests.post(L["base_url"] + "/chat/completions", json=body, timeout=600)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"].get("content") or ""
            return json.loads(content)
        except Exception as e:
            last = e
            time.sleep(1 + attempt)
    raise RuntimeError(f"LLM call failed: {last}")
