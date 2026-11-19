"""Free evidence from chat for one moment: what words stand out, what kind of reaction, top message.

No AI: counting and comparing with the rest of the stream.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from functools import lru_cache

from .replay import RAW

# Reaction types and the chat words that signal them.
TYPES = {
    "funny": {"lol", "lmao", "lmfao", "rofl", "😂", "🤣", "kekw", "lul", "omegalul", "dead", "haha", "hahaha", "xd", "💀"},
    "shock": {"no way", "what", "omg", "wtf", "bro", "😱", "😳", "holy", "how", "insane", "crazy", "wait"},
    "win": {"w", "clutch", "goat", "🔥", "lets go", "let's go", "letsgo", "ez", "gg", "pog", "poggers", "🐐", "king"},
    "fail": {"l", "rip", "unlucky", "noo", "😭", "💔", "f", "sad", "nooo", "bruh"},
    "hype": {"hype", "finally", "congrats", "congratulations", "🎉", "🥳", "yes", "lets gooo", "w stream"},
}
TYPE_LABELS = {"funny": "😂 Funny", "shock": "😱 Shock", "win": "🔥 Win / skill",
               "fail": "💔 Fail", "hype": "🎉 Hype / payoff"}

_EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")
_WORD = re.compile(r"[a-z0-9']+")
_STOP = set("""a an the i you he she it we they me my your his her our their is are was were be been am to of in on at
for and or but so if then this that these those with as by from up down out just do did does not no yes ok okay
im i'm its it's u ur can will would should could have has had get got go going gonna wanna what's thats that's
chat stream like really very too also all any some one two there here when who why how much more now""".split())
_KEEP_SHORT = {"w", "l", "f", "gg", "ez", "xd", "no", "how", "what", "yes"}


def normalize(text: str) -> str:
    t = text.lower()
    t = re.sub(r":[a-z0-9_-]+:", " ", t)        # custom emoji codes like :_kekw:
    t = re.sub(r"(.)\1{2,}", r"\1", t)          # noooo -> no, lmaooo -> lmao
    return t


def tokens(text: str) -> list[str]:
    """Words, two-word phrases and emojis in one message (each counted once per message)."""
    t = normalize(text)
    words = _WORD.findall(t)
    out = set(_EMOJI.findall(t))
    for w in words:
        if w in _KEEP_SHORT or (w not in _STOP and len(w) > 1):
            out.add(w)
    for a, b in zip(words, words[1:]):
        if a not in _STOP or b not in _STOP:
            out.add(f"{a} {b}")
    return list(out)


@lru_cache(maxsize=8)
def stream_counts(video_id: str) -> tuple[Counter, int]:
    """How often each token appears across the whole stream (cached)."""
    c: Counter = Counter()
    n = 0
    with (RAW / f"{video_id}.jsonl").open() as f:
        for line in f:
            _, _, text = json.loads(line)
            c.update(tokens(text))
            n += 1
    return c, n


def messages_between(video_id: str, t0: float, t1: float) -> list[tuple[float, str, str]]:
    out = []
    with (RAW / f"{video_id}.jsonl").open() as f:
        for line in f:
            t, a, x = json.loads(line)
            if t > t1:
                break
            if t >= t0:
                out.append((t, a, x))
    return out


def evidence(video_id: str, clip_start: float, clip_end: float) -> dict:
    msgs = [m for m in messages_between(video_id, clip_start, clip_end) if not m[2].lstrip().startswith("!")]
    total, n_all = stream_counts(video_id)
    # Count each word once per person, so one spammer can't make a keyword.
    per_person: dict[str, set] = {}
    for _, a, x in msgs:
        per_person.setdefault(a, set()).update(tokens(x))
    here: Counter = Counter()
    for toks in per_person.values():
        here.update(toks)
    n = max(len(per_person), 1)

    # Words that stand out: much more common here than in the rest of the stream.
    standout = []
    for tok, k in here.items():
        if k < 3:
            continue
        lift = (k / n) / ((total[tok] + 1) / max(n_all, 1))
        if lift >= 3:
            standout.append((lift * math.log(1 + k), tok, k, lift))
    standout.sort(reverse=True)
    picked: list[dict] = []
    for _, tok, k, lift in standout:
        if any(tok in p["text"] or p["text"] in tok for p in picked):  # skip overlaps ("no" vs "no way")
            continue
        picked.append({"text": tok, "count": k, "times_more": round(lift, 1)})
        if len(picked) == 6:
            break

    # Reaction type: share of messages that match each word list.
    type_hits = Counter()
    for _, _, x in msgs:
        toks = set(tokens(x))
        for name, words in TYPES.items():
            if toks & words:
                type_hits[name] += 1
    hits = sum(type_hits.values()) or 1
    types = [{"type": name, "label": TYPE_LABELS[name], "share": round(100 * k / hits)}
             for name, k in type_hits.most_common() if k / hits >= 0.08][:3]

    # The message repeated most word for word (copy-pasted reactions).
    repeated = Counter(normalize(x).strip() for _, _, x in msgs if len(x.strip()) > 1).most_common(1)
    top_msg = {"text": repeated[0][0], "count": repeated[0][1]} if repeated and repeated[0][1] >= 3 else None

    return {"messages": len(msgs), "keywords": picked, "types": types, "top_message": top_msg,
            "sample": [f"{a}: {x}" for _, a, x in msgs[:: max(1, len(msgs) // 40)][:40]]}
