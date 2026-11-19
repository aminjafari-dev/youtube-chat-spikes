"""Double-check a chat spike with Gemini and get exact clip cuts. Text only, no video.

Only runs when you press the button on a moment. Gemini gets:
  - what the streamer says from 1 minute before to 1 minute after the spike
    (YouTube's captions, numbered lines, each with its stream time)
  - every chat message of the spike, with its time
  - chat messages per 10 seconds over those 2 minutes
and answers what happened, why chat reacted, and which line the clip should start
and end on, plus the exact words. The times are then taken from YouTube's word timing,
not from Gemini, so Gemini can't invent a time.

Free tier: one call per button press, spaced out, answers saved so nothing is asked twice.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from .replay import ROOT

# Best first. All are on the free tier (Google pricing/models pages, Nov 2026).
# If a model has no free quota left (or none at all for this key), the next one is tried.
MODEL_CHAIN = ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"]
MIN_GAP = 13.0      # seconds between calls: stays under ~5 requests/minute free limits
TIMEOUT = 60         # seconds to wait for one answer; then try the next model
BEFORE = AFTER = 60  # transcript sent: 1 minute before and after the spike
MAX_CHAT = 2000      # chat messages sent at most (a busy spike has a few hundred)
PAD_START, PAD_END = 0.3, 0.7  # small safety margin around the cut (caption timing is ~0.5 s exact)
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


class Cut(BaseModel):
    what_happened: str = Field(description="What happens, based on what is said and how chat reacts, 1-3 sentences.")
    why_chat_reacted: str = Field(description="Why viewers reacted so strongly at this exact moment, 1-2 sentences.")
    why_viral: str = Field(description="Why this could (or could not) work as a short clip for strangers, 1-2 sentences.")
    moment_type: MomentType
    viral_score: int = Field(ge=1, le=10, description="1 = only fans care, 10 = anyone would share it.")
    title: str = Field(description="A short, catchy clip title (max ~60 characters). No clickbait lies.")
    hook: str = Field(description="One line of on-screen text for the first second of the clip.")
    start_line: int = Field(description="Number of the transcript line the clip should START on.")
    start_words: str = Field(description="The first 2-6 words of the clip, copied exactly from that line.")
    end_line: int = Field(description="Number of the transcript line the clip should END on.")
    end_words: str = Field(description="The last 2-6 words of the clip, copied exactly from that line.")
    cut_reason: str = Field(description="Why the clip starts and ends there, 1 sentence.")
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


def log(msg: str) -> None:
    print(f"[gemini {time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _mmss(s: float) -> str:
    s = max(0, s)
    return f"{int(s) // 3600}:{int(s) % 3600 // 60:02d}:{s % 60:04.1f}"


def _prompt(info: dict, moment: dict, ev: dict, lines: list[dict], chat: list[tuple], counts: list[tuple]) -> str:
    kw = ", ".join(f'"{k["text"]}" ({k["count"]} people)' for k in ev["keywords"]) or "none"
    types = ", ".join(f'{t["label"]} {t["share"]}%' for t in ev["types"]) or "unclear"
    transcript = "\n".join(f'L{l["n"]} [{_mmss(l["start"])}] {l["text"]}' for l in lines)
    timeline = "\n".join(f"{_mmss(t)}  {n} messages" for t, n in counts)
    chat_txt = "\n".join(f"[{_mmss(t)}] {a}: {x}" for t, a, x in chat)
    return f"""You are a video editor cutting short viral clips (Shorts/TikTok) from a finished livestream.

Stream: "{info.get('title', '')}" by {info.get('channel', 'unknown')}.
Live chat exploded at {_mmss(moment['peak'])}: {moment['score']:.0f} messages in 10 seconds,
{moment['ratio']}x the stream's normal level. Chat usually reacts 5-20 seconds AFTER the thing that caused it.
Times are stream time (h:mm:ss.s).

TRANSCRIPT from YouTube's automatic captions, 1 minute before to 1 minute after the spike.
">>" means a different person starts talking. [sounds] are in brackets. Captions can mishear words.
{transcript}

CHAT MESSAGES PER 10 SECONDS:
{timeline}

Chat summary: words that stand out: {kw}. Reaction mix: {types}.

EVERY CHAT MESSAGE DURING THE SPIKE (may include spam, other languages, unrelated talk):
{chat_txt}

Tasks:
1. Work out what happened and why chat reacted so strongly.
2. Judge whether it works as a short clip for people who don't know this streamer.
3. Choose the exact clip: start on the transcript line where the build-up begins (so a stranger understands),
   end right after the payoff or reaction. Usually 15-60 seconds. Give the line numbers and copy the
   first and last words exactly as written in those lines.
If the transcript doesn't explain the reaction (it may be something only visible on screen), say so and
lower the confidence and viral score. Write in plain English."""


def _norm(w: str) -> str:
    return re.sub(r"[^\w']", "", w.lower())


def _find(words: list[list], quote: str, first: bool) -> int | None:
    """Index (in this line's words) of the first word of the quote, or of its last word."""
    q = [_norm(w) for w in quote.split() if _norm(w)]
    lw = [_norm(w[1]) for w in words]
    if not q:
        return None
    for i in range(len(lw) - len(q) + 1):
        if lw[i:i + len(q)] == q:
            return i if first else i + len(q) - 1
    target = q[0] if first else q[-1]  # partial match: just the first/last word
    hits = [i for i, w in enumerate(lw) if w == target]
    return (hits[0] if first else hits[-1]) if hits else None


def cut_times(lines: list[dict], cut: Cut) -> tuple[float, float, bool]:
    """Exact (start, end) from YouTube's word timing. Third value: True if the quoted words were found."""
    by_n = {l["n"]: l for l in lines}
    sl = by_n.get(cut.start_line) or lines[0]
    el = by_n.get(cut.end_line) or lines[-1]
    if el["n"] < sl["n"]:
        sl, el = el, sl
    i = _find(sl["words"], cut.start_words, True)
    j = _find(el["words"], cut.end_words, False)
    start = sl["words"][i][0] if i is not None else sl["start"]
    if j is not None and j + 1 < len(el["words"]):
        end = el["words"][j + 1][0]
    else:
        end = el["end"]
    return round(start - PAD_START, 1), round(end + PAD_END, 1), i is not None and j is not None


def _call(model: str, prompt: str) -> Cut:
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=api_key(), http_options=types.HttpOptions(
        timeout=TIMEOUT * 1000, retry_options=types.HttpRetryOptions(attempts=1)))  # retries are handled below
    try:
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=Cut,
                temperature=0.3,
            ),
        )
    except errors.APIError as e:
        raise GeminiError(f"{e.code}|{e.message or e}") from e
    except Exception as e:  # timeout or network problem
        raise GeminiError(f"504|no answer within {TIMEOUT} s ({type(e).__name__})") from e
    if resp.parsed is None:
        raise GeminiError(f"0|Gemini returned no usable answer: {(resp.text or '')[:200]}")
    return resp.parsed


def explain(video_id: str, info: dict, moment: dict, ev: dict) -> dict:
    """Ask Gemini about one moment. Returns the saved answer dict."""
    global _last_call
    from .cli import load_ignore_users
    from .detector import Rules, keep_message
    from .evidence import messages_between
    from .transcript import TranscriptError, lines_between, words

    saved = saved_answer(video_id, moment["peak"])
    if saved:
        return saved
    if not api_key():
        raise GeminiError("No Gemini key. Put GEMINI_API_KEY=... in the project's .env file "
                          "(free key: aistudio.google.com → Get API key).")
    t0, t1 = moment["peak"] - BEFORE, moment["peak"] + AFTER
    try:
        lines = lines_between(words(video_id), t0, t1)
    except TranscriptError as e:
        raise GeminiError(str(e)) from None
    if not lines:
        raise GeminiError("Nothing is said in the captions around this moment, so there's nothing to cut on.")
    rules = Rules(ignore_users=load_ignore_users())
    msgs = [m for m in messages_between(video_id, t0, t1) if keep_message(m[1], m[2], rules)]  # no bots/!commands
    chat = [m for m in msgs if moment["clip_start"] <= m[0] <= moment["clip_end"]][:MAX_CHAT]
    counts = [(t, sum(1 for m in msgs if t <= m[0] < t + 10)) for t in range(int(t0), int(t1), 10)]
    prompt = _prompt(info, moment, ev, lines, chat, counts)

    last_err = "no model available"
    busy = False
    with _lock:  # one call at a time, spaced out for the free tier
        for model in model_chain():
            for attempt in range(3):
                wait = MIN_GAP - (time.time() - _last_call)
                if wait > 0:
                    time.sleep(wait)
                _last_call = time.time()
                log(f"asking {model} about {_mmss(moment['peak'])} (attempt {attempt + 1}, ~{len(prompt) // 4:,} tokens)")
                try:
                    cut = _call(model, prompt)
                    log(f"{model} answered in {time.time() - _last_call:.0f} s")
                    start, end, exact = cut_times(lines, cut)
                    result = {"model": model, "peak": moment["peak"], **cut.model_dump(mode="json"),
                              "best_start": start, "best_end": end, "words_found": exact,
                              "transcript": [{k: l[k] for k in ("n", "start", "end", "text")} for l in lines],
                              "chat_sent": len(chat)}
                    p = answer_path(video_id, moment["peak"])
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(json.dumps(result, ensure_ascii=False, indent=1))
                    return result
                except GeminiError as e:
                    code, _, msg = str(e).partition("|")
                    last_err = f"{model}: {msg}"
                    log(f"{model} error {code}: {msg[:200]}")
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
                    if code in ("500", "503", "504", "0"):
                        # Google's free models are busy ("high demand") or slow: one quick retry,
                        # then the next model. Not marked dead: it's usually over in minutes.
                        if attempt == 0:
                            time.sleep(3)
                            continue
                        busy = True
                        break
                    if code in ("400", "401", "403"):
                        raise GeminiError(f"Gemini refused the request: {msg}") from None
                    break
    if busy:
        raise GeminiError("Google's free Gemini models are busy right now (high demand). "
                          "Try again in a few minutes.")
    raise GeminiError(f"Gemini couldn't answer (free limit may be used up for today). Last error: {last_err}")
