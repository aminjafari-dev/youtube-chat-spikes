"""Clips: turn a chat spike into a vertical reel (1080×1920) ready for Shorts / Reels / TikTok.

    top    (1080×672):  the streamer's face cam, zoomed to the full width
    bottom (1080×1248): the middle of the stream screen (the game)

If a channel has no face-cam box yet, the whole reel is the middle of the screen.
The face-cam box is saved per channel in data/clip_layouts.json (as parts of the frame, 0..1),
and can be changed on the page by drawing a box on a frame of the clip.

Only yt-dlp downloads from YouTube (ffmpeg only works on the downloaded file on this Mac):

  Finished stream:  yt-dlp --download-sections "*START-END"   (only those seconds)
  Live stream:      yt-dlp can't cut a time range of a stream that is still live, so we use the live
                    HLS feed, which keeps the last ~15 minutes. yt-dlp starts N pieces back from the live
                    edge (ffmpeg's -live_start_index -N). Each piece is 2 s and numbered from the start of
                    the stream (".../sq/4249/..."), so piece number × 2 s = stream time. That tells us
                    exactly where the moment is inside the downloaded file.

Clips are saved in data/clips/<video>/<peak>.mp4 (+ .jpg, a frame of the full screen for the layout box).
One clip is made at a time (a queue), and each one is a job on the Activity panel.
"""

from __future__ import annotations

import atexit
import json
import math
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import activity, replay
from .replay import ROOT, _last_error, _ytdlp
from .report import hms

CLIPS = ROOT / "data" / "clips"
LAYOUTS = ROOT / "data" / "clip_layouts.json"
W, H, TOP_H = 1080, 1920, 672           # reel size and the face-cam part on top
LIVE_BACK_MAX = 840                      # the live feed keeps ~15 min: older moments must wait for the recording
LIVE_READY = 8                           # live: start this many seconds after the clip's end reached YouTube
LIVE_MARGIN = 12                         # live: start this much earlier on the feed (in case it runs behind)

# Face-cam boxes found by hand: [x, y, width, height] as parts of the frame (0..1).
DEFAULT_LAYOUTS = {
    # GTA 6 stream, Nov 2026: face cam 0–388 × 461–702 of 1920×1080, game between black bars at y 60–1020
    "IShowSpeed": {"face": [0.0, 0.428, 0.2015, 0.221], "game": [0.0, 0.0556, 1.0, 0.8889]},
}

STEPS = [
    ("wait", "Wait for the video", "Live only: waits until the end of the clip is on YouTube's live feed "
     "(usually a few seconds after the spike is found)."),
    ("download", "Download the piece", "yt-dlp downloads only these seconds of the video (up to 1080p)."),
    ("render", "Make the reel", "ffmpeg on your Mac: face cam on top, the middle of the screen below, 1080×1920."),
]


class ClipError(Exception):
    pass


# ---------- layouts ----------

def layouts() -> dict:
    saved = json.loads(LAYOUTS.read_text()) if LAYOUTS.exists() else {}
    return {**DEFAULT_LAYOUTS, **saved}


def layout_for(channel: str, vid: str = "") -> dict:
    """face: the face-cam box, or None. game: the part of the screen with the game (default: all of it).

    The stream's own box first (the same channel may stream with or without a face cam), else the channel's."""
    all_ = layouts()
    return {"face": None, "game": None, **all_.get(channel, {}), **all_.get(f"video:{vid}", {})}


def _box(box: list[float] | None, what: str) -> list[float] | None:
    if box is None:
        return None
    x, y, w, h = (min(1.0, max(0.0, float(v))) for v in box)
    if w < 0.03 or h < 0.03:
        raise ClipError(f"The {what} box is too small.")
    return [round(x, 4), round(y, 4), round(min(w, 1 - x), 4), round(min(h, 1 - y), 4)]


def save_layout(channel: str, vid: str, face: list[float] | None, game: list[float] | None = None) -> dict:
    """Saved for this stream, and (when there is a face cam) as the channel's default for its next streams."""
    saved = json.loads(LAYOUTS.read_text()) if LAYOUTS.exists() else {}
    lay = {"face": _box(face, "face-cam"), "game": _box(game, "game")}
    saved[f"video:{vid}"] = lay
    if channel and lay["face"]:  # "no face cam" is about this stream only (e.g. an outdoor stream)
        saved[channel] = lay
    LAYOUTS.parent.mkdir(parents=True, exist_ok=True)
    LAYOUTS.write_text(json.dumps(saved, indent=2))
    return lay


def _filter(face: list[float] | None, game: list[float] | None = None) -> str:
    even = lambda e: f"trunc(({e})/2)*2"
    g = ""
    if game:
        gx, gy, gw, gh = game
        g = f"crop={even(f'iw*{gw}')}:{even(f'ih*{gh}')}:iw*{gx}:ih*{gy},"
    if not face:
        return f"[0:v]{g}crop={even('ih*9/16')}:ih,scale={W}:{H},setsar=1[v]"
    x, y, w, h = face
    bot = H - TOP_H
    return (f"[0:v]split=2[a][b];"
            f"[a]crop={even(f'iw*{w}')}:{even(f'ih*{h}')}:iw*{x}:ih*{y},"
            f"scale={W}:{TOP_H}:force_original_aspect_ratio=increase,crop={W}:{TOP_H},setsar=1[top];"
            f"[b]{g}crop={even(f'ih*{W}/{bot}')}:ih,scale={W}:{bot},setsar=1[bot];"
            f"[top][bot]vstack[v]")


# ---------- files ----------

def _name(peak: float) -> str:
    return str(int(round(peak)))


def clip_path(vid: str, peak: float) -> Path:
    return CLIPS / vid / f"{_name(peak)}.mp4"


def saved(vid: str) -> dict[str, dict]:
    out = {}
    d = CLIPS / vid
    for f in sorted(d.glob("*.json")) if d.exists() else []:
        try:
            info = json.loads(f.read_text())
        except ValueError:
            continue
        if (d / f"{f.stem}.mp4").exists():
            out[f.stem] = {**info, "state": "done"}
    return out


# ---------- running yt-dlp / ffmpeg so they can be stopped ----------

_procs: set[subprocess.Popen] = set()


def _run(cmd: list[str], timeout: float, log: Path | None = None) -> tuple[int, str]:
    out = open(log, "w") if log else subprocess.PIPE
    p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=out, text=True, start_new_session=True)
    _procs.add(p)
    try:
        _, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(p)
        raise ClipError(f"took longer than {int(timeout)} s, stopped")
    finally:
        _procs.discard(p)
        if log:
            out.close()
    return p.returncode, (log.read_text(errors="replace") if log else err or "")


def _kill(p: subprocess.Popen) -> None:
    try:
        os.killpg(p.pid, signal.SIGTERM)
        p.wait(5)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


@atexit.register
def _cleanup() -> None:
    for p in list(_procs):
        _kill(p)


# ---------- downloading the piece ----------

def _download_finished(vid: str, a: float, b: float, work: Path) -> tuple[Path, float]:
    """A finished stream: yt-dlp downloads only [a, b]. Returns (file, where the clip starts in it)."""
    rc, err = _run(_ytdlp("-f", "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080]/b",
                          "--download-sections", f"*{a:.1f}-{b:.1f}", "-o", str(work / "src.%(ext)s"),
                          f"https://www.youtube.com/watch?v={vid}"), timeout=900)
    src = next(iter(sorted(work.glob("src.*"))), None)
    if rc != 0 or not src:
        msg = _last_error(err)
        if "403" in msg:
            msg = "YouTube refused the download (403). If the stream ended a short time ago, YouTube may still " \
                  "be processing the recording: try again in a while."
        raise ClipError(msg)
    return src, 0.0


def _download_live(vid: str, a: float, b: float, started: float, work: Path, margin: float = LIVE_MARGIN) -> tuple[Path, float]:
    """A stream that is live now: go N pieces back on the live HLS feed (see the top of this file)."""
    edge = time.time() - started
    # 2-second pieces. The feed's newest piece is some seconds behind real time, so go LIVE_MARGIN further
    # back; the piece numbers then tell exactly where the clip starts inside the file.
    back = math.ceil((edge - a + margin) / 2)
    length = (b - a) + margin + 10
    log = work / "ffmpeg.log"
    rc, err = _run(_ytdlp("-f", "312+234/311+234/bv*[height<=1080][protocol^=m3u8]+ba[protocol^=m3u8]/b",
                          "--downloader", "ffmpeg",
                          "--downloader-args", f"ffmpeg_i:-live_start_index -{back} -loglevel verbose",
                          "--downloader-args", f"ffmpeg_o:-t {length:.1f}",
                          "-o", str(work / "src.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"),
                   timeout=600, log=log)
    src = next(iter(sorted(p for p in work.glob("src.*") if p.suffix != ".part")), None)
    if rc != 0 or not src:
        raise ClipError(_last_error(err))
    sq = re.search(r"/sq/(\d+)/", err)
    dur = re.search(r"/dur/([\d.]+)/", err)
    if not sq:
        raise ClipError("couldn't read the piece numbers of the live feed")
    first = int(sq.group(1)) * (float(dur.group(1)) if dur else 2.0)  # stream time of the file's first frame
    if a - first < 0:
        if margin < 60:  # the feed was further behind than expected: once more, from further back
            for f in work.glob("src.*"):
                f.unlink()
            return _download_live(vid, a, b, started, work, margin + 60)
        raise ClipError(f"the live feed started {first - a:.0f} s after the clip start (piece {sq.group(1)}); try again")
    return src, a - first


# ---------- the queue ----------

_q: "queue.Queue[dict]" = queue.Queue()
_jobs: dict[str, dict] = {}
_lock = threading.Lock()
_worker: threading.Thread | None = None


def _key(vid: str, peak: float) -> str:
    return f"{vid}:{_name(peak)}"


def jobs(vid: str) -> dict[str, dict]:
    """Every clip of a video: made earlier (on disk) and queued / being made now."""
    out = saved(vid)
    with _lock:
        for k, j in _jobs.items():
            if j["video"] == vid and (j["state"] != "done" or j["peak_name"] not in out):
                out[j["peak_name"]] = {x: j[x] for x in ("state", "note", "peak", "start", "end", "run")}
    return out


def make(vid: str, moment: dict, channel: str, title: str = "", auto: bool = False) -> dict:
    """Queue a clip of a moment (again, if it was made before). auto=True: skipped when it already exists."""
    name = _name(moment["peak"])
    k = _key(vid, moment["peak"])
    with _lock:
        j = _jobs.get(k)
        if j and j["state"] in ("queued", "waiting", "working"):
            return j
        if auto and clip_path(vid, moment["peak"]).exists():
            return {"state": "done", "note": ""}
        j = {"video": vid, "peak": moment["peak"], "peak_name": name, "start": moment["clip_start"],
             "end": moment["clip_end"], "channel": channel, "title": title, "state": "queued",
             "note": "waiting for other clips" if not _q.empty() else "", "run": None}
        _jobs[k] = j
        _q.put(j)
        _ensure_worker()
    return j


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, name="clips", daemon=True)
        _worker.start()


def _loop() -> None:
    while True:
        j = _q.get()
        try:
            _make(j)
        except Exception as e:  # never let one clip stop the queue
            j["state"], j["note"] = "error", str(e)


def _started(vid: str) -> float | None:
    """Live: the stream's real 0:00 (synced by the live page when it was open)."""
    from . import live
    w = live.get(vid)
    if w and w.info:
        return w.info.started
    m = replay.live_meta(vid)
    return m.get("started") if m else None


def _make(j: dict) -> None:
    from . import live
    vid, a, b = j["video"], j["start"], j["end"]
    run = activity.start("clip", f"Clip · {j['channel'] or vid} · moment {hms(j['peak'])}",
                         {"video": vid, "peak": j["peak"], "clip": [a, b]}, STEPS)
    j["run"], j["state"] = run.id, "working"
    work = CLIPS / vid / f"_work_{j['peak_name']}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        status = live.live_status(vid)
        if status == "is_live":
            started = _started(vid)
            if started is None:
                raise ClipError("don't know when this live stream started: open it on the Live page first")
            with run.step("wait") as st:
                j["state"] = "waiting"
                while (lag := b + LIVE_READY - (time.time() - started)) > 0:
                    j["note"] = f"the end of the clip reaches YouTube in {int(lag)} s"
                    st.detail(j["note"])
                    time.sleep(min(lag, 2))
                if time.time() - started - a > LIVE_BACK_MAX:
                    raise ClipError("this moment is older than the ~15 minutes YouTube keeps on the live feed. "
                                    "Make the clip after the stream ends (Finished stream page).")
                st.done("ready")
            j["state"] = "working"
        else:
            run.step("wait").skip("not live: the recording is complete")
            if status == "post_live":
                raise ClipError("the stream just ended and YouTube is still processing the recording. "
                                "Try again in a while.")
        with run.step("download") as st:
            j["note"] = "downloading the piece…"
            st.detail(f"{hms(a)} → {hms(b)} ({int(b - a)} s) · {'live feed' if status == 'is_live' else 'recording'}")
            t0 = time.time()
            src, offset = (_download_live(vid, a, b, _started(vid), work) if status == "is_live"
                           else _download_finished(vid, a, b, work))
            st.set(offset=round(offset, 1))
            st.log(f"downloaded {src.name}; the clip starts {offset:.1f} s into it")
            st.done(f"{src.stat().st_size / 1e6:.1f} MB in {time.time() - t0:.0f} s · clip starts {offset:.1f} s in")
        with run.step("render") as st:
            j["note"] = "making the reel…"
            lay = layout_for(j["channel"], vid)
            st.detail("face cam on top + middle of the screen" if lay["face"] else
                      "no face-cam box for this channel yet: middle of the screen only")
            out = clip_path(vid, j["peak"])
            tmp = work / "out.mp4"
            rc, err = _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{offset:.2f}", "-i", str(src),
                            "-t", f"{b - a:.2f}", "-filter_complex", _filter(lay["face"], lay["game"]),
                            "-map", "[v]", "-map", "0:a?", "-c:v", "libx264", "-preset", "veryfast",
                            "-crf", "20", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-b:a", "160k",
                            "-movflags", "+faststart", str(tmp)], timeout=900)
            if rc != 0 or not tmp.exists():
                raise ClipError("ffmpeg: " + _last_error(err))
            # A frame of the full screen, so the face-cam box can be drawn on the page.
            _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{offset + (b - a) / 2:.2f}", "-i", str(src),
                  "-frames:v", "1", "-vf", "scale=960:-2", str(out.with_suffix(".jpg"))], timeout=60)
            tmp.replace(out)
            out.with_suffix(".json").write_text(json.dumps({
                "peak": j["peak"], "start": a, "end": b, "channel": j["channel"], "title": j["title"],
                "face": lay["face"], "game": lay["game"], "made": time.time(), "size": out.stat().st_size}))
            st.done(f"{out.stat().st_size / 1e6:.1f} MB · {int(b - a)} s · 1080×1920")
        j["state"], j["note"] = "done", ""
        run.finish("done", f"reel ready · {int(b - a)} s")
    except ClipError as e:
        j["state"], j["note"] = "error", str(e)
        for st in run.steps.values():
            if st.status == "running":
                st.fail(str(e))
        run.finish("error", str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
