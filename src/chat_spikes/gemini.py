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


def _answers(video_id: str) -> Path:
    """Saved AI answers: the stream's folder, ai/ (library.py)."""
    from .library import ai_dir
    return ai_dir(video_id)

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


class ChatGuess(BaseModel):
    """Live mode: no captions yet, so Gemini only reads chat and can't pick exact cut words."""
    what_happened: str = Field(description="Your best guess of what happened, from chat only, 1-3 sentences.")
    why_chat_reacted: str = Field(description="Why viewers reacted so strongly at this moment, 1-2 sentences.")
    why_viral: str = Field(description="Why this could (or could not) work as a short clip for strangers, 1-2 sentences.")
    moment_type: MomentType
    viral_score: int = Field(ge=1, le=10, description="1 = only fans care, 10 = anyone would share it.")
    title: str = Field(description="A short, catchy clip title (max ~60 characters). No clickbait lies.")
    hook: str = Field(description="One line of on-screen text for the first second of the clip.")
    confidence: str = Field(description="low, medium or high: how sure you are, given you only see chat.")


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


def answer_path(video_id: str, peak: float, chat_only: bool = False) -> Path:
    return _answers(video_id) / f"{int(peak)}{'.chat' if chat_only else ''}.json"


def saved_answer(video_id: str, peak: float) -> dict | None:
    """The full answer (captions + exact cut) if there is one, else a chat-only answer from live mode."""
    for p in (answer_path(video_id, peak), answer_path(video_id, peak, True)):
        if p.exists():
            return json.loads(p.read_text())
    return None


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


def _chat_prompt(info: dict, moment: dict, ev: dict, chat: list[tuple], counts: list[tuple]) -> str:
    kw = ", ".join(f'"{k["text"]}" ({k["count"]} people)' for k in ev["keywords"]) or "none"
    types = ", ".join(f'{t["label"]} {t["share"]}%' for t in ev["types"]) or "unclear"
    timeline = "\n".join(f"{_mmss(t)}  {n} messages" for t, n in counts)
    chat_txt = "\n".join(f"[{_mmss(t)}] {a}: {x}" for t, a, x in chat)
    return f"""You are a video editor watching a livestream that is LIVE right now, looking for short viral clips.

Stream: "{info.get('title', '')}" by {info.get('channel', 'unknown')}.
Live chat exploded at {_mmss(moment['peak'])}: {moment['score']:.0f} messages in 10 seconds,
{moment['ratio']}x the stream's normal level. Chat usually reacts 5-20 seconds AFTER the thing that caused it.
Times are stream time (h:mm:ss.s).
You only have the chat: there is no transcript and no video yet.

CHAT MESSAGES PER 10 SECONDS:
{timeline}

Chat summary: words that stand out: {kw}. Reaction mix: {types}.

EVERY CHAT MESSAGE DURING THE SPIKE (may include spam, other languages, unrelated talk):
{chat_txt}

Tasks:
1. Guess what happened and why chat reacted so strongly. Only use what chat shows; don't invent details.
2. Judge whether it could work as a short clip for people who don't know this streamer.
If chat doesn't make it clear, say so and use low confidence. Write in plain English."""


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


def _call(model: str, prompt: str, schema=Cut):
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
                response_schema=schema,
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


AI_CHAT_STEPS = [
    ("evidence", "Chat evidence", "Counts the chat of this moment on your Mac: reaction mix, words that stand out, most repeated message. Free, no AI."),
    ("prompt", "Build the question", "Live mode: puts together every chat message of the spike and chat per 10 s over 2 minutes. No captions: YouTube makes them only after the stream is processed."),
    ("queue", "Wait for turn", "Free tier: one Gemini request at a time, at least 13 s apart, so the per-minute limit is never hit."),
    ("gemini", "Ask Gemini", "Sends the question to the best free model. If a model is busy (503), slow (no answer in 60 s) or out of free quota, the next model is tried."),
    ("save", "Save answer", "Saves the answer in the stream's folder, ai/<time>.chat.json."),
]

AI_STEPS = [
    ("evidence", "Chat evidence", "Counts the chat of this moment on your Mac: reaction mix, words that stand out, most repeated message. Free, no AI."),
    ("captions", "Get captions", "What the streamer says, with a time for every word: YouTube's captions, downloaded once per stream with yt-dlp, then read from the saved copy."),
    ("prompt", "Build the question", "Puts together the transcript from 1 minute before to 1 minute after the spike, every chat message of the spike and chat per 10 s. Text only."),
    ("queue", "Wait for turn", "Free tier: one Gemini request at a time, at least 13 s apart, so the per-minute limit is never hit."),
    ("gemini", "Ask Gemini", "Sends the question to the best free model. If a model is busy (503), slow (no answer in 60 s) or out of free quota, the next model is tried."),
    ("cut", "Exact cut", "Finds the start and end words Gemini chose in YouTube's captions and takes their exact times (+0.3 s before, +0.7 s after)."),
    ("save", "Save answer", "Saves the answer in the stream's folder (ai/), so this moment never uses quota again."),
]


def explain(video_id: str, info: dict, moment: dict, ev: dict, run=None) -> dict:
    """Ask Gemini about one moment. Returns the saved answer dict. `run` shows each step on the Activity panel."""
    from . import activity
    from .transcript import TranscriptError, lines_between, words

    run = run or activity.NULL
    full = answer_path(video_id, moment["peak"])
    if full.exists():  # a chat-only answer from live mode doesn't count: this one also uses captions
        run.step("save").skip("answer was saved before")
        return json.loads(full.read_text())
    if not api_key():
        raise GeminiError("No Gemini key. Put GEMINI_API_KEY=... in the project's .env file "
                          "(free key: aistudio.google.com → Get API key).")
    t0, t1 = moment["peak"] - BEFORE, moment["peak"] + AFTER
    with run.step("captions") as st:
        try:
            all_words = words(video_id, st)
        except TranscriptError as e:
            raise GeminiError(str(e)) from None
        lines = lines_between(all_words, t0, t1)
        if not lines:
            last = all_words[-1][0] if all_words else 0
            raise GeminiError("Nothing is said in the captions around this moment, so there's nothing to cut on"
                              + (f" (the captions end at {_mmss(last)})." if last < t0 else "."))
        st.done(f"{len(lines)} lines between {_mmss(t0)} and {_mmss(t1)}")
    with run.step("prompt") as st:
        chat, counts = _chat_around(video_id, moment)
        prompt = _prompt(info, moment, ev, lines, chat, counts)
        st.set(transcript_lines=len(lines), chat_messages=len(chat), characters=len(prompt), tokens_about=len(prompt) // 4)
        st.done(f"{len(lines)} transcript lines + {len(chat):,} chat messages · about {len(prompt) // 4:,} tokens")

    model, cut = _ask(prompt, Cut, run, moment["peak"])
    with run.step("cut") as st:
        start, end, exact = cut_times(lines, cut)
        st.set(start_line=cut.start_line, start_words=cut.start_words,
               end_line=cut.end_line, end_words=cut.end_words, words_found=exact)
        st.done(f"{_mmss(start)} → {_mmss(end)} ({end - start:.1f} s)"
                + ("" if exact else " · words not found, used whole lines"))
    result = {"model": model, "peak": moment["peak"], **cut.model_dump(mode="json"),
              "best_start": start, "best_end": end, "words_found": exact,
              "transcript": [{k: l[k] for k in ("n", "start", "end", "text")} for l in lines],
              "chat_sent": len(chat)}
    return _save(video_id, moment["peak"], result, run)


def _save(video_id: str, peak: float, result: dict, run, chat_only: bool = False) -> dict:
    with run.step("save") as st:
        p = answer_path(video_id, peak, chat_only)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, ensure_ascii=False, indent=1))
        st.done(str(p.relative_to(ROOT)) if ROOT in p.parents else str(p))
    return result


def _chat_around(video_id: str, moment: dict) -> tuple[list, list]:
    """Chat of the spike (no bots/!commands) and messages per 10 s over 2 minutes."""
    from .cli import load_ignore_users
    from .detector import Rules, keep_message
    from .evidence import messages_between

    t0, t1 = moment["peak"] - BEFORE, moment["peak"] + AFTER
    rules = Rules(ignore_users=load_ignore_users())
    msgs = [m for m in messages_between(video_id, t0, t1) if keep_message(m[1], m[2], rules)]
    chat = [m for m in msgs if moment["clip_start"] <= m[0] <= moment["clip_end"]][:MAX_CHAT]
    counts = [(t, sum(1 for m in msgs if t <= m[0] < t + 10)) for t in range(int(t0), int(t1), 10)]
    return chat, counts


def explain_chat(video_id: str, info: dict, moment: dict, ev: dict, run=None) -> dict:
    """Live mode: ask Gemini about one moment from chat only (captions don't exist yet). No exact cut."""
    from . import activity

    run = run or activity.NULL
    if not api_key():
        raise GeminiError("No Gemini key. Put GEMINI_API_KEY=... in the project's .env file "
                          "(free key: aistudio.google.com → Get API key).")
    with run.step("prompt") as st:
        chat, counts = _chat_around(video_id, moment)
        prompt = _chat_prompt(info, moment, ev, chat, counts)
        st.set(chat_messages=len(chat), characters=len(prompt), tokens_about=len(prompt) // 4)
        st.done(f"{len(chat):,} chat messages, no captions · about {len(prompt) // 4:,} tokens")
    model, guess = _ask(prompt, ChatGuess, run, moment["peak"])
    result = {"model": model, "peak": moment["peak"], "chat_only": True, **guess.model_dump(mode="json"),
              "chat_sent": len(chat)}
    return _save(video_id, moment["peak"], result, run, chat_only=True)


# ---------- the hook on the reel ----------

HOOK_WORDS = 10


def cut_hook(text: str) -> str:
    """At most HOOK_WORDS words (emoji don't count), no quotes, no hashtags."""
    out, n = [], 0
    for w in text.replace('"', "").replace("“", "").replace("”", "").split():
        if w.startswith("#"):
            continue
        if any(c.isalnum() for c in w):
            n += 1
            if n > HOOK_WORDS:
                break
        out.append(w)
    return " ".join(out)


class ReelHook(BaseModel):
    hook: str = Field(description=f"The caption: at most {HOOK_WORDS} words plus 1-2 emoji at the end, shown for the first 5 seconds.")
    alternatives: list[str] = Field(description="2 other captions, same rules.")
    why: str = Field(description="Why this hook makes a stranger watch to the end, 1 sentence.")


def hook_path(video_id: str, peak: float) -> Path:
    return _answers(video_id) / f"{int(peak)}.hook.json"


def _hook_prompt(info: dict, moment: dict, lines: list[dict], chat: list[tuple], known: dict | None) -> str:
    transcript = "\n".join(f'[{_mmss(l["start"])}] {l["text"]}' for l in lines) or \
        "(no transcript: the stream is live, YouTube makes captions only after it ends)"
    chat_txt = "\n".join(f"[{_mmss(t)}] {x}" for t, _, x in chat[:600])
    about = ""
    if known:
        about = f"\nWhat happened (an earlier analysis): {known.get('what_happened', '')}\n" \
                f"Why chat reacted: {known.get('why_chat_reacted', '')}\n"
    return f"""You write the on-screen hook for a vertical short (TikTok / Reels / Shorts) cut from a livestream.

Stream: "{info.get('title', '')}" by {info.get('channel', 'unknown')}.
The clip runs {_mmss(moment['clip_start'])} → {_mmss(moment['clip_end'])}; chat exploded at {_mmss(moment['peak'])}.
{about}
WHAT THE STREAMER SAYS IN THE CLIP:
{transcript}

CHAT DURING THE CLIP:
{chat_txt}

Write the caption: dark text in a white box in the middle of the screen, between the streamer's face cam
(top) and the game (bottom), shown for the FIRST 5 SECONDS only, then it disappears. It tells a stranger in one
glance what this clip is and why to keep watching, like the best TikTok clip-channel captions, e.g.
  "Lacy shows off how BIG the GTA VI Map is 😳🔥"
  "Kai did NOT expect chat to do this 💀"
Rules:
- At most {HOOK_WORDS} words (5-9 is ideal), then 1-2 emoji at the end that fit the mood (😳 🔥 💀 😂 😭 🤯 👀 😤 🥶 🫡).
  Simple single emoji only (no flags, no skin tones, no combined ones). No hashtags, no quotes around it.
- Name the streamer if it helps (as people know them, e.g. their channel name). Normal sentence case, with ONE
  or two words in CAPITALS for emphasis.
- It must stop the scroll and make a stranger watch until the end: a curiosity gap, a bold claim, the
  stakes. Don't spoil the payoff.
- Controversial is good; lying is not. Base it on what really happens in the transcript and chat.
  No slurs, no hate against groups, no invented crimes or accusations against real people.
- English, written for people who have never heard of this streamer."""


def reel_hook(video_id: str, info: dict, moment: dict, live_now: bool, run=None) -> dict:
    """A caption (<= 10 words + emoji) for the reel's first 5 s, from the transcript (finished streams) and the chat. Saved, asked once."""
    from . import activity
    from .transcript import TranscriptError, lines_between, words

    run = run or activity.NULL
    p = hook_path(video_id, moment["peak"])
    if p.exists():
        return json.loads(p.read_text())
    if not api_key():
        raise GeminiError("No Gemini key (GEMINI_API_KEY in .env)")
    with run.step("prompt") as st:
        lines = []
        if not live_now:
            try:
                lines = lines_between(words(video_id), moment["clip_start"], moment["clip_end"])
            except TranscriptError as e:
                st.log(f"no transcript: {e}")
        chat, _ = _chat_around(video_id, moment)
        prompt = _hook_prompt(info, moment, lines, chat, saved_answer(video_id, moment["peak"]))
        st.done(f"{len(lines)} transcript lines + {len(chat):,} chat messages")
    model, ans = _ask(prompt, ReelHook, run, moment["peak"])
    result = {"model": model, "hook": cut_hook(ans.hook), "alternatives": [cut_hook(a) for a in ans.alternatives],
              "why": ans.why, "transcript_lines": len(lines), "chat_sent": len(chat)}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def _ask(prompt: str, schema, run, peak: float):
    """Send one question, trying the free models in order. Returns (model, parsed answer)."""
    global _last_call

    def say(step, msg: str) -> None:
        log(msg)
        step.log(msg)

    last_err = "no model available"
    busy = False
    q = run.step("queue").begin("waiting for any other Gemini request to finish")
    with _lock:  # one call at a time, spaced out for the free tier
        q.done("my turn")
        g = run.step("gemini").begin()
        for model in model_chain():
            for attempt in range(3):
                g.set(model=model, attempt=attempt + 1)
                wait = MIN_GAP - (time.time() - _last_call)
                if wait > 0:
                    g.detail(f"pausing {wait:.0f} s (free tier spacing) before asking {model}")
                    time.sleep(wait)
                _last_call = time.time()
                g.detail(f"asking {model} (try {attempt + 1}), waiting for the answer… (up to {TIMEOUT} s)")
                say(g, f"asking {model} about {_mmss(peak)} (attempt {attempt + 1}, ~{len(prompt) // 4:,} tokens)")
                try:
                    answer = _call(model, prompt, schema)
                    say(g, f"{model} answered in {time.time() - _last_call:.0f} s")
                    g.set(seconds=round(time.time() - _last_call, 1), viral_score=getattr(answer, "viral_score", None))
                    if hasattr(answer, "viral_score"):
                        g.done(f"{model} answered: “{answer.title}” · {answer.viral_score}/10 · confidence {answer.confidence}")
                    else:
                        g.done(f"{model} answered: “{answer.hook}”")
                    return model, answer
                except GeminiError as e:
                    code, _, msg = str(e).partition("|")
                    last_err = f"{model}: {msg}"
                    say(g, f"{model} error {code}: {msg[:200]}")
                    if code == "429":
                        # Per-minute limit: wait and retry. Daily / zero quota: try the next model.
                        delay = re.search(r"retry in ([\d.]+)s|retryDelay['\"]?: ?['\"]?(\d+)", msg)
                        per_day = re.search(r"per ?day|PerDay|limit: 0", msg, re.I)
                        if per_day or attempt == 2:
                            _dead_models.add(model)
                            say(g, f"{model}: free quota used up, skipping it from now on")
                            break
                        secs = float(next(x for x in delay.groups() if x)) if delay else 30
                        g.detail(f"{model}: per-minute limit, waiting {min(secs + 1, 65):.0f} s")
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
