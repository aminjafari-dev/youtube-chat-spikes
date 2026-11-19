"""What the streamer says: YouTube's captions of the stream, with a time for every word.

Downloaded once with yt-dlp (text only, no video) and saved in data/raw/<id>.words.json
as [[seconds, word], ...]. YouTube makes automatic captions for most finished streams;
if the streamer uploaded their own captions, those are used instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading

from .replay import RAW, _last_error, _ytdlp


class TranscriptError(Exception):
    pass


_lock = threading.Lock()


def _words_path(video_id: str):
    return RAW / f"{video_id}.words.json"


def _pick_language(d: dict) -> tuple[str, bool] | None:
    """(language key, is_automatic). Prefer the streamer's own captions, then the original-language auto captions."""
    lang = (d.get("language") or "").split("-")[0]
    manual = {k: v for k, v in (d.get("subtitles") or {}).items() if k != "live_chat"}
    for k in manual:
        if lang and k.split("-")[0] == lang:
            return k, False
    auto = d.get("automatic_captions") or {}
    for k in ([f"{lang}-orig"] if lang else []) + ["en-orig"]:
        if k in auto:
            return k, True
    origs = [k for k in auto if k.endswith("-orig")]
    if origs:
        return origs[0], True
    if manual:
        return next(iter(manual)), False
    return None


def _parse_json3(data: dict) -> list[list]:
    """YouTube json3 captions -> [[seconds, word], ...]. Auto captions have a time per word."""
    words: list[list] = []
    for ev in data.get("events", []):
        start = ev.get("tStartMs")
        if start is None:
            continue
        for seg in ev.get("segs") or []:
            text = (seg.get("utf8") or "").replace("\n", " ").strip()
            if not text:
                continue
            t = (start + seg.get("tOffsetMs", 0)) / 1000
            for w in text.split():
                words.append([round(t, 2), w])
    words.sort(key=lambda w: w[0])
    return words


def words(video_id: str) -> list[list]:
    """All words of the stream with their times. Downloads once (a few seconds), then reads the saved copy."""
    path = _words_path(video_id)
    with _lock:
        if path.exists():
            return json.loads(path.read_text())
        r = subprocess.run(_ytdlp("--skip-download", "--dump-single-json",
                                  f"https://www.youtube.com/watch?v={video_id}"),
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise TranscriptError(f"Couldn't open this video: {_last_error(r.stderr)}")
        pick = _pick_language(json.loads(r.stdout))
        if not pick:
            raise TranscriptError("YouTube has no captions for this stream, so there is no transcript.")
        key, auto = pick
        tmp = RAW / f"_subs_{video_id}"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir(parents=True)
        try:
            r = subprocess.run(_ytdlp("--skip-download", "--write-auto-subs" if auto else "--write-subs",
                                      "--sub-langs", key, "--sub-format", "json3",
                                      "-o", str(tmp / "%(id)s.%(ext)s"),
                                      f"https://www.youtube.com/watch?v={video_id}"),
                               capture_output=True, text=True, timeout=300)
            files = list(tmp.glob("*.json3"))
            if r.returncode != 0 or not files:
                raise TranscriptError(f"Couldn't download the captions: {_last_error(r.stderr)}")
            result = _parse_json3(json.loads(files[0].read_text()))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if not result:
            raise TranscriptError("The captions of this stream are empty.")
        path.write_text(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return result


def lines_between(all_words: list[list], t0: float, t1: float,
                  max_words: int = 12, max_gap: float = 1.2) -> list[dict]:
    """Group the words between t0 and t1 into short numbered lines: {n, start, end, text, words}.

    A new line starts after a pause or every ~12 words, so each line is a few seconds long.
    The end of a line is when the next word starts (or +1 s for the last one).
    """
    sel = [w for w in all_words if t0 <= w[0] <= t1]
    lines: list[dict] = []
    cur: list[list] = []
    for w in sel:
        new_speaker = w[1] == ">>"  # YouTube marks a change of speaker with >>
        if cur and (new_speaker or w[0] - cur[-1][0] > max_gap or len(cur) >= max_words):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    out = []
    for i, ws in enumerate(lines):
        nxt = lines[i + 1][0][0] if i + 1 < len(lines) else ws[-1][0] + 1.0
        end = min(nxt, ws[-1][0] + 1.5)
        out.append({"n": i + 1, "start": ws[0][0], "end": round(end, 2),
                    "text": " ".join(w[1] for w in ws), "words": ws})
    return out
