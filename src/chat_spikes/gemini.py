"""Ask Gemini why a moment made chat explode, and whether it could go viral.

Gemini watches the clip itself (picture + sound) and reads the chat evidence.
Uses the free tier: only a few calls, spaced out, answers saved so nothing is asked twice.

The clip is downloaded with yt-dlp (only those seconds, low quality, a few MB) and sent
to Gemini directly. Sending a YouTube link instead may make Gemini process the audio of the
whole stream (reported Aug 2026), which would burn the free quota.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from .replay import RAW, ROOT

# Best first. All are on the free tier and accept video (Google pricing/models pages, Nov 2026).
# If a model has no free quota left (or none at all for this key), the next one is tried.
MODEL_CHAIN = ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
MIN_GAP = 13.0  # seconds between calls: stays under ~5 requests/minute free limits
CLIPS = ROOT / "data" / "processed" / "clips"
ANSWERS = ROOT / "data" / "processed" / "why"

_lock = threading.Lock()
_last_call = 0.0
_dead_models: set[str] = set()  # out of free quota for today in this run


class GeminiError(Exception):
    pass


class MomentType(str, Enum):
    funny = "funny"
    shock = "shock"
    skill_win = "skill_win"
    fail = "fail"
    rage = "rage"
    payoff = "payoff"
    wholesome = "wholesome"
    drama = "drama"
    announcement = "announcement"
    technical_issue = "technical_issue"
    other = "other"


class Why(BaseModel):
    what_happened: str = Field(description="What happens on screen and what the streamer says/does, 1-3 sentences.")
    why_chat_reacted: str = Field(description="Why viewers reacted so strongly at this exact moment, 1-2 sentences.")
    why_viral: str = Field(description="Why this could (or could not) work as a short viral clip, 1-2 sentences.")
    moment_type: MomentType
    viral_score: int = Field(ge=1, le=10, description="1 = only fans care, 10 = anyone would share it.")
    title: str = Field(description="A short, catchy clip title (max ~60 characters). No clickbait lies.")
    hook: str = Field(description="One line of on-screen text for the first second of the clip.")
    best_start_s: float = Field(description="Seconds from the start of THIS clip where the clip should begin.")
    best_end_s: float = Field(description="Seconds from the start of THIS clip where the clip should end.")
    confidence: str = Field(description="low, medium or high: how sure you are about what happened.")


def api_key() -> str | None:
    if os.environ.get("GEMINI_API_KEY"):
        return os.environ["GEMINI_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("GEMINI_API_KEY="):
                return line.split("=", 1)[1].strip().strip("\"'") or None
    return None


def model_chain() -> list[str]:
    forced = os.environ.get("GEMINI_MODEL")
    env = ROOT / ".env"
    if not forced and env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("GEMINI_MODEL="):
                forced = line.split("=", 1)[1].strip().strip("\"'")
    chain = ([forced] if forced else []) + [m for m in MODEL_CHAIN if m != forced]
    return [m for m in chain if m not in _dead_models]


def answer_path(video_id: str, peak: float) -> Path:
    return ANSWERS / video_id / f"{int(peak)}.json"


def saved_answer(video_id: str, peak: float) -> dict | None:
    p = answer_path(video_id, peak)
    return json.loads(p.read_text()) if p.exists() else None


def download_clip(video_id: str, start: float, end: float) -> Path:
    """Only these seconds of the video, small (<=360p), as mp4. Saved for reuse."""
    CLIPS.mkdir(parents=True, exist_ok=True)
    out = CLIPS / f"{video_id}_{int(start)}_{int(end)}.mp4"
    if out.exists() and out.stat().st_size > 0:
        return out
    tmp = CLIPS / f"_tmp_{video_id}_{int(start)}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress", "-q",
           "--download-sections", f"*{start:.0f}-{end:.0f}",
           "-f", "bv*[height<=360]+ba/b[height<=360]/wv*+ba/w",
           "--merge-output-format", "mp4", "-o", str(tmp / "clip.%(ext)s"),
           f"https://www.youtube.com/watch?v={video_id}"]
    if shutil.which("node"):
        cmd[3:3] = ["--js-runtimes", "node"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    files = list(tmp.glob("clip.*"))
    if r.returncode != 0 or not files:
        shutil.rmtree(tmp, ignore_errors=True)
        last = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
        raise GeminiError(f"Couldn't download the clip: {last.replace('ERROR: ', '')}")
    files[0].rename(out)
    shutil.rmtree(tmp, ignore_errors=True)
    return out


def _prompt(info: dict, moment: dict, ev: dict) -> str:
    def hms(s: float) -> str:
        s = int(s)
        return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"
    peak_in_clip = moment["peak"] - moment["clip_start"]
    kw = ", ".join(f'"{k["text"]}" ({k["count"]} people, {k["times_more"]}x more than usual)' for k in ev["keywords"]) or "none"
    types = ", ".join(f'{t["label"]} {t["share"]}%' for t in ev["types"]) or "unclear"
    top = f'"{ev["top_message"]["text"]}" x{ev["top_message"]["count"]}' if ev.get("top_message") else "none"
    sample = "\n".join(ev["sample"][:40])
    return f"""You are helping a video editor find clips from a finished livestream that could go viral as Shorts/TikToks.

Stream: "{info.get('title', '')}" by {info.get('channel', 'unknown')}.
The attached video is a clip from stream time {hms(moment['clip_start'])} to {hms(moment['clip_end'])}.
Live chat exploded here: {moment['score']:.0f} messages in 10 seconds ({moment['ratio']}x the stream's normal level),
peaking about {peak_in_clip:.0f} seconds into this clip. Chat usually reacts 5-20 seconds AFTER the event.

Chat evidence (counted automatically):
- Words that stand out: {kw}
- Reaction mix: {types}
- Most repeated message: {top}
- Sample of chat during the clip (may include spam, other languages, unrelated talk):
{sample}

Watch the clip (picture and sound) and use the chat evidence to explain:
1. What actually happens.
2. Why chat reacted so strongly at this moment.
3. Whether it would work as a short viral clip for people who don't know this streamer, and why.
Then give a title, a hook line, and the tightest start/end (seconds from the start of THIS clip) that keeps the build-up and the payoff.
If the video doesn't show anything clear (e.g. chat reacts to something off-screen), say so and lower the confidence and viral score.
Write in plain English."""


def _call(model: str, video: bytes, prompt: str) -> Why:
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=api_key())
    try:
        resp = client.models.generate_content(
            model=model,
            contents=[types.Part.from_bytes(data=video, mime_type="video/mp4"), prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Why,
                media_resolution=types.MediaResolution.MEDIA_RESOLUTION_LOW,  # ~3x fewer tokens
                temperature=0.4,
            ),
        )
    except errors.APIError as e:
        raise GeminiError(f"{e.code}|{e.message or e}") from e
    if resp.parsed is None:
        raise GeminiError(f"0|Gemini returned no usable answer: {(resp.text or '')[:200]}")
    return resp.parsed


def explain(video_id: str, info: dict, moment: dict, ev: dict) -> dict:
    """Ask Gemini about one moment. Returns the saved answer dict."""
    global _last_call
    saved = saved_answer(video_id, moment["peak"])
    if saved:
        return saved
    if not api_key():
        raise GeminiError("No Gemini key. Put GEMINI_API_KEY=... in the project's .env file "
                          "(free key: aistudio.google.com → Get API key).")
    clip = download_clip(video_id, moment["clip_start"], moment["clip_end"])
    data = clip.read_bytes()
    if len(data) > 19 * 1024 * 1024:
        raise GeminiError("The clip is too big to send (over 19 MB).")
    prompt = _prompt(info, moment, ev)

    last_err = "no model available"
    with _lock:  # one call at a time, spaced out for the free tier
        for model in model_chain():
            for attempt in range(3):
                wait = MIN_GAP - (time.time() - _last_call)
                if wait > 0:
                    time.sleep(wait)
                _last_call = time.time()
                try:
                    why = _call(model, data, prompt)
                    result = {"model": model, "clip_start": moment["clip_start"], "peak": moment["peak"],
                              **why.model_dump(mode="json")}
                    result["best_start"] = round(moment["clip_start"] + max(0.0, why.best_start_s), 1)
                    result["best_end"] = round(moment["clip_start"] + min(why.best_end_s,
                                               moment["clip_end"] - moment["clip_start"]), 1)
                    p = answer_path(video_id, moment["peak"])
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(result, ensure_ascii=False, indent=1))
                    return result
                except GeminiError as e:
                    code, _, msg = str(e).partition("|")
                    last_err = f"{model}: {msg}"
                    if code == "429":
                        # Per-minute limit: wait and retry. Daily / zero quota: try the next model.
                        delay = re.search(r"retry in ([\d.]+)s|retryDelay['\"]?: ?['\"]?(\d+)", msg)
                        per_day = re.search(r"per ?day|PerDay|limit: 0", msg, re.I)
                        if per_day or attempt == 2:
                            _dead_models.add(model)
                            break
                        secs = float(next(g for g in delay.groups() if g)) if delay else 30
                        time.sleep(min(secs + 1, 65))
                        continue
                    if code in ("404", "400") and ("not found" in msg.lower() or "not supported" in msg.lower()):
                        _dead_models.add(model)
                        break
                    if code in ("500", "503") and attempt < 2:
                        time.sleep(10)
                        continue
                    if code in ("400", "401", "403"):
                        raise GeminiError(f"Gemini refused the request: {msg}") from None
                    break
    raise GeminiError(f"Gemini couldn't answer (free limit may be used up for today). Last error: {last_err}")
