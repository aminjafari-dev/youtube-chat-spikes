"""Chat replay of a finished YouTube stream, downloaded with yt-dlp.

yt-dlp writes one JSON object per line. Each line holds the chat actions at one moment,
with `videoOffsetTimeMsec` = exact position in the recording. We read the file while
yt-dlp is still writing it, so results can be shown before the download finishes.

Saved in the stream's folder (library.py), streams/<Channel>/<date> <Title> [<id>]/chat/:
    replay.jsonl      [seconds, author, text] per message (small, reused next time)
    replay.meta.json  title, channel, duration

A stream recorded in live mode (live.py) is saved as live.jsonl + live.meta.json there,
same format. When there is no full replay copy, the live recording is used instead.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw"   # temp folders of running yt-dlp jobs only
LIVE_RUNNING: set[str] = set()  # video ids live.py is recording right now


class ReplayError(Exception):
    pass


@dataclass
class StreamInfo:
    video_id: str
    title: str
    channel: str
    duration: float
    live: bool = False   # True = chat recorded in live mode (may start late, if the watch started late)
    first: float = 0.0   # live recording: stream time of the first message
    started: float = 0.0  # unix time the stream started (for the folder name)


def _file(video_id: str, name: str) -> Path:
    from .library import chat_file
    return chat_file(video_id, name)


def _ytdlp(*args: str) -> list[str]:
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress"]
    if shutil.which("node"):
        cmd += ["--js-runtimes", "node"]
    return cmd + list(args)


def _last_error(stderr: str) -> str:
    lines = [l for l in stderr.strip().splitlines() if l.strip()]
    msg = lines[-1] if lines else "unknown error"
    return msg.replace("ERROR: ", "").split("] ", 1)[-1]


def chat_path(video_id: str) -> Path:
    """The saved chat of a stream: the full replay copy, or else the live recording."""
    full = _file(video_id, "replay.jsonl")
    live = _file(video_id, "live.jsonl")
    return full if full.exists() or not live.exists() else live


def live_meta(video_id: str) -> dict | None:
    p = _file(video_id, "live.meta.json")
    return json.loads(p.read_text()) if p.exists() else None


def cached(video_id: str, include_running: bool = False) -> StreamInfo | None:
    """Saved stream info. A live recording counts once it is no longer being recorded (or always with include_running)."""
    meta = _file(video_id, "replay.meta.json")
    if meta.exists() and _file(video_id, "replay.jsonl").exists():
        m = json.loads(meta.read_text())
        return StreamInfo(video_id, m["title"], m.get("channel", ""), m.get("duration", 0))
    m = live_meta(video_id)
    if m and _file(video_id, "live.jsonl").exists() and (video_id not in LIVE_RUNNING or include_running):
        return StreamInfo(video_id, m["title"], m.get("channel", ""), m.get("duration", 0), live=True,
                          first=m.get("first", 0))
    return None


def get_info(video_id: str) -> StreamInfo:
    """Check the video is a finished stream with chat replay."""
    r = subprocess.run(_ytdlp("--skip-download", "--dump-single-json",
                              f"https://www.youtube.com/watch?v={video_id}"),
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise ReplayError(f"Couldn't open this video: {_last_error(r.stderr)}")
    d = json.loads(r.stdout)
    status = d.get("live_status")
    if status in ("is_live", "is_upcoming", "post_live"):
        raise ReplayError("This stream is still live (or still processing). Try again after it has finished.")
    if "live_chat" not in (d.get("subtitles") or {}):
        if status != "was_live":
            raise ReplayError("This is a normal video, not a past live stream, so it has no chat.")
        raise ReplayError("This past stream has no chat replay (the streamer may have turned it off).")
    started = d.get("release_timestamp") or d.get("timestamp")
    if not started and d.get("upload_date"):
        import time
        started = time.mktime(time.strptime(d["upload_date"], "%Y%m%d"))
    return StreamInfo(video_id, d.get("title") or video_id, d.get("channel") or "", float(d.get("duration") or 0),
                      started=float(started or 0))


# ---- Parsing YouTube chat items ----

def _text(runs: list | None) -> str:
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            custom = e.get("isCustomEmoji") and e.get("shortcuts")
            out.append(e["shortcuts"][0] if custom else e.get("emojiId", ""))
    return "".join(out)


def parse_line(line: str) -> list[tuple[float, str, str]]:
    """One yt-dlp line -> [(seconds, author, text), ...] for real chat messages."""
    try:
        d = json.loads(line)
    except ValueError:
        return []
    rep = d.get("replayChatItemAction") or {}
    offset = rep.get("videoOffsetTimeMsec")
    if offset is None:
        return []
    t = int(offset) / 1000
    out = []
    for a in rep.get("actions", []):
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        author = (r.get("authorName") or {}).get("simpleText") or "?"
        out.append((t, author, _text((r.get("message") or {}).get("runs"))))
    return out


# ---- Loading ----

def read_cached(video_id: str) -> Iterator[tuple[float, str, str]]:
    with chat_path(video_id).open() as f:
        for line in f:
            t, a, x = json.loads(line)
            yield t, a, x


def download(info: StreamInfo, stop: threading.Event,
             on_progress: Callable[[float, int], None] | None = None) -> Iterator[tuple[float, str, str]]:
    """Download the chat replay; yields messages while the download is still running.

    on_progress(seconds_reached, messages_so_far) is called as it goes.
    If stop is set, the download is cancelled and nothing is kept.
    """
    RAW.mkdir(parents=True, exist_ok=True)
    vid = info.video_id
    tmp = RAW / f"_dl_{vid}"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    err_log = tmp / "errors.txt"
    with err_log.open("w") as err:  # a file, not a pipe: a full pipe would freeze yt-dlp
        proc = subprocess.Popen(
            _ytdlp("--skip-download", "--write-subs", "--sub-langs", "live_chat",
                   "-o", str(tmp / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"),
            stdout=subprocess.DEVNULL, stderr=err, text=True)
    out_path = _file(vid, "replay.jsonl")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    part = out_path.with_suffix(".jsonl.part")
    count, done = 0, False
    src = None
    try:
        with part.open("w") as out:
            buf = ""
            while True:
                finished = proc.poll() is not None  # check before reading, so the last read gets everything
                if src is None:
                    files = list(tmp.glob("*.live_chat.json*"))
                    if files:
                        src = files[0].open()  # stays valid when yt-dlp renames .part -> .json
                chunk = src.read() if src is not None else ""
                if chunk:
                    buf += chunk
                    *lines, buf = buf.split("\n")
                    reached = None
                    for line in lines:
                        for m in parse_line(line):
                            out.write(json.dumps(m, ensure_ascii=False) + "\n")
                            count += 1
                            reached = m[0]
                            yield m
                    if on_progress and reached is not None:
                        on_progress(reached, count)
                if stop.is_set():
                    raise ReplayError("Cancelled.")
                if finished and not chunk:
                    break
                if not chunk:
                    stop.wait(0.3)
            if buf.strip():
                for m in parse_line(buf):
                    out.write(json.dumps(m, ensure_ascii=False) + "\n")
                    count += 1
                    yield m
        if proc.returncode != 0:
            raise ReplayError(f"Chat download failed: {_last_error(err_log.read_text())}")
        if count == 0:
            raise ReplayError("The chat replay was empty.")
        part.rename(out_path)
        _file(vid, "replay.meta.json").write_text(json.dumps(
            {"title": info.title, "channel": info.channel, "duration": info.duration, "messages": count}))
        done = True
    finally:
        if proc.poll() is None:
            proc.kill()
        if src is not None:
            src.close()
        shutil.rmtree(tmp, ignore_errors=True)
        if not done:
            part.unlink(missing_ok=True)


def load(video_id: str, stop: threading.Event | None = None,
         on_info: Callable[[StreamInfo, bool], None] | None = None,
         on_progress: Callable[[float, int], None] | None = None) -> Iterator[tuple[float, str, str]]:
    """Messages of a finished stream: from the saved copy, or downloaded now."""
    stop = stop or threading.Event()
    info = cached(video_id)
    if info:
        if on_info:
            on_info(info, True)
        yield from read_cached(video_id)
        return
    info = get_info(video_id)
    from .library import register
    register(video_id, info.title, info.channel, info.started or None, "finished")
    if on_info:
        on_info(info, False)
    yield from download(info, stop, on_progress)
