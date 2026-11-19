"""Clips: turn a chat spike into a vertical reel (1080×1920) ready for Shorts / Reels / TikTok.

    top    (1080×672):  the streamer's face cam, zoomed to the full width
    bottom (1080×1248): the middle of the stream screen (the game)

If a channel has no face-cam box yet, the whole reel is the middle of the screen.
The face-cam box is saved per channel in data/clip_layouts.json (as parts of the frame, 0..1),
and can be changed on the page by drawing a box on a frame of the clip.

Only yt-dlp downloads from YouTube (ffmpeg only works on the downloaded file on this Mac):

  Finished stream:  yt-dlp --download-sections "*START-END"   (only those seconds)
  Live stream:      yt-dlp can't cut a time range of a stream that is still live, so we use the live
                    HLS feed, whose playlist lists the last ~15 minutes. yt-dlp starts N pieces back from the live
                    edge (ffmpeg's -live_start_index -N). Each piece is 2 s and numbered from the start of
                    the stream (".../sq/4249/..."), so piece number × 2 s = stream time. That tells us
                    exactly where the moment is inside the downloaded file.
                    Older moments: YouTube still serves those pieces (the player's DVR, hours back), just not
                    in the playlist, so they are fetched by number (_download_pieces).

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
LIVE_BACK_MAX = 840                      # the live playlist lists ~15 min: older moments are fetched piece by piece
LIVE_READY = 8                           # live: start this many seconds after the clip's end reached YouTube
# Best quality first: the highest resolution, then frame rate, then bitrate (= most detail). No size limit:
# a 1440p/4K stream is downloaded in 1440p/4K. (A live stream's feed tops out at 1080p60 on YouTube.)
BEST = "res,fps,br,abr"
LIVE_MARGIN = 12                         # live: start this much earlier on the feed (in case it runs behind)

# Face-cam boxes found by hand: [x, y, width, height] as parts of the frame (0..1).
DEFAULT_LAYOUTS = {
    # GTA 6 stream, Nov 2026: face cam 0–388 × 461–702 of 1920×1080, game between black bars at y 60–1020
    "IShowSpeed": {"face": [0.0, 0.428, 0.2015, 0.221], "game": [0.0, 0.0556, 1.0, 0.8889]},
}

STEPS = [
    ("queue", "In line", "One reel is made at a time. A reel waits here while another one is being made."),
    ("check", "Check the stream", "Asks yt-dlp if the stream is live now or finished (or still being processed by "
     "YouTube), and picks the face-cam box for this stream."),
    ("wait", "Wait for the video", "Live only: waits until the end of the clip is on YouTube's live feed "
     "(usually a few seconds after the spike is found)."),
    ("download", "Download the piece", "yt-dlp downloads only these seconds of the video (up to 1080p). "
     "From a live stream this runs at about real-time speed."),
    ("render", "Make the reel", "ffmpeg on your Mac: face cam on top, the middle of the screen below, 1080×1920."),
    ("save", "Save", "Writes the reel (.mp4), a frame of the full screen (.jpg) and its info (.json) to "
     "data/clips/<video>/ and deletes the temporary files."),
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
        return f"[0:v]{g}crop={even('ih*9/16')}:ih,scale={W}:{H}:flags=lanczos,setsar=1[v]"
    x, y, w, h = face
    bot = H - TOP_H
    return (f"[0:v]split=2[a][b];"
            f"[a]crop={even(f'iw*{w}')}:{even(f'ih*{h}')}:iw*{x}:ih*{y},"
            f"scale={W}:{TOP_H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{TOP_H},setsar=1[top];"
            f"[b]{g}crop={even(f'ih*{W}/{bot}')}:ih,scale={W}:{bot}:flags=lanczos,setsar=1[bot];"
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


_TIME = re.compile(r"time=(\d+):(\d+):([\d.]+)")


class Cancelled(Exception):
    pass


def _run(cmd: list[str], timeout: float, log: Path | None = None, on_time=None,
         stop: threading.Event | None = None) -> tuple[int, str]:
    """Run yt-dlp / ffmpeg in its own process group. on_time(seconds): ffmpeg's progress ("time=…")."""
    log = log or Path(CLIPS / f"_run_{os.getpid()}_{threading.get_ident()}.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "w") as out:
        p = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=out, text=True, start_new_session=True)
        _procs.add(p)
        t0 = time.time()
        try:
            while p.poll() is None:
                if time.time() - t0 > timeout:
                    _kill(p)
                    raise ClipError(f"took longer than {int(timeout)} s, stopped")
                if stop is not None and stop.is_set():
                    _kill(p)
                    raise Cancelled()
                time.sleep(0.5)
                if on_time:
                    tail = log.read_bytes()[-4000:].decode(errors="replace")
                    if m := list(_TIME.finditer(tail)):
                        h, mi, se = m[-1].groups()
                        on_time(int(h) * 3600 + int(mi) * 60 + float(se))
        finally:
            _procs.discard(p)
    text = log.read_text(errors="replace")
    if log.name.startswith("_run_"):
        log.unlink(missing_ok=True)
    return p.returncode, text


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

def _download_finished(vid: str, a: float, b: float, work: Path, on_time=None, stop=None,
                       tries: int = 2) -> tuple[Path, float, float]:
    """A finished stream: yt-dlp downloads only [a, b].

    Returns (file, where the clip starts in its video, where it starts in its audio). Here both are 0: the
    video starts at the keyframe before `a`, but those frames are marked "skip", so its timestamps line
    up with the audio."""
    rc, err = _run(_ytdlp("-f", "bv*+ba/b", "-S", BEST, "--merge-output-format", "mkv",
                          "--downloader-args", "ffmpeg_o:-stats",  # progress lines ("time=…") for the page
                          "--download-sections", f"*{a:.1f}-{b:.1f}", "-o", str(work / "src.%(ext)s"),
                          f"https://www.youtube.com/watch?v={vid}"), timeout=900, log=work / "download.log",
                   on_time=on_time, stop=stop)
    src = next(iter(sorted(p for p in work.glob("src.*") if p.suffix != ".part")), None)
    if rc != 0 or not src:
        msg = _last_error(err)
        if "403" in msg and tries > 1:  # YouTube sometimes refuses a request for no reason: once more
            for f in work.glob("src.*"):
                f.unlink()
            time.sleep(3)
            return _download_finished(vid, a, b, work, on_time, stop, tries - 1)
        if "403" in msg:
            msg = "YouTube refused the download (403). If the stream ended a short time ago, YouTube may still " \
                  "be processing the recording: try again in a while."
        raise ClipError(msg)
    return src, 0.0, 0.0


_INPUT = re.compile(r"Input #(\d+), hls, from '[^']*?/itag/(\d+)/[^']*'.*?Duration: [^,]*, start: ([\d.]+)(.*?)(?=Input #|$)",
                    re.S)


def _live_starts(log: str) -> dict[str, float]:
    """Stream time of the first frame of the video and of the audio in the downloaded file.

    YouTube's video and audio lists of a live stream don't have the same length, so going N pieces back
    lands on DIFFERENT pieces (seen: video piece 5308, audio piece 5311 = 6 s apart). ffmpeg then glues
    both starts together and the sound is late. So each one gets its own start: the "start:" ffmpeg prints
    for each input is the stream time (pieces are 2 s and numbered from the start of the stream)."""
    out = {}
    for m in _INPUT.finditer(log):
        _, itag, start, rest = m.groups()
        first_stream = re.search(r"Stream #\d+:\d+[^\n]*?: (Video|Audio)", rest)
        if not first_stream:
            continue
        kind = first_stream.group(1).lower()
        sq = re.search(rf"/itag/{itag}/[^']*?/sq/(\d+)/", log)
        dur = re.search(r"/dur/([\d.]+)/", log)
        by_piece = int(sq.group(1)) * (float(dur.group(1)) if dur else 2.0) if sq else None
        t = float(start)
        if by_piece is not None and abs(t - by_piece) > 10:  # odd timestamps: trust the piece number
            t = by_piece
        out.setdefault(kind, t)
    return out


def _download_live(vid: str, a: float, b: float, started: float, work: Path, on_time=None, stop=None,
                   margin: float = LIVE_MARGIN) -> tuple[Path, float, float]:
    """A stream that is live now: go N pieces back on the live HLS feed (see the top of this file).

    Returns (file, where the clip starts in its video, where it starts in its audio)."""
    edge = time.time() - started
    # 2-second pieces. The feed's newest piece is some seconds behind real time, so go `margin` further
    # back; the timestamps then tell exactly where the clip starts inside the file.
    back = math.ceil((edge - a + margin) / 2)
    length = (b - a) + margin + 10
    log = work / "ffmpeg.log"
    rc, err = _run(_ytdlp("-f", "bv*[protocol^=m3u8]+ba[protocol^=m3u8]/b[protocol^=m3u8]", "-S", BEST,
                          "--downloader", "ffmpeg",
                          "--downloader-args", f"ffmpeg_i:-live_start_index -{back} -loglevel verbose",
                          "--downloader-args", f"ffmpeg_o:-t {length:.1f} -stats",
                          "-o", str(work / "src.%(ext)s"), f"https://www.youtube.com/watch?v={vid}"),
                   timeout=900, log=log, on_time=on_time, stop=stop)
    src = next(iter(sorted(p for p in work.glob("src.*") if p.suffix != ".part")), None)
    if rc != 0 or not src:
        raise ClipError(_last_error(err))
    starts = _live_starts(err)
    if "video" not in starts:
        raise ClipError("couldn't read where the live feed starts")
    v0 = starts["video"]
    a0 = starts.get("audio", v0)
    _download_live.info = (f"{back} pieces back: video starts at stream time {v0:.1f} s, audio at {a0:.1f} s "
                           f"({a0 - v0:+.1f} s), the clip at {a:.1f} s")
    if a < max(v0, a0):
        if margin < 60:  # the feed was further behind than expected: once more, from further back
            for f in work.glob("src.*"):
                f.unlink()
            return _download_live(vid, a, b, started, work, on_time, stop, margin + 60)
        raise ClipError(f"the live feed started {max(v0, a0) - a:.0f} s after the clip start; try again")
    return src, a - v0, a - a0


def _download_pieces(vid: str, a: float, b: float, work: Path, on_time=None, stop=None) -> tuple[Path, float, float]:
    """A live moment older than the live playlist (~15 min): fetch its pieces by number.

    The playlist YouTube hands out lists only the last 900 s, but the server still serves older pieces
    (the DVR the player seeks in, hours back) at the same URL with another "/sq/N/". yt-dlp gives the
    playlist URLs; the pieces of [a, b] are fetched, listed in a local playlist each (video + audio), and
    ffmpeg joins them. The video pieces' timestamps and the audio's ID3 timestamps are stream time, so
    ffprobe on each local playlist says exactly where each one starts.

    Returns (file, where the clip starts in its video, where it starts in its audio)."""
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    p = subprocess.run(_ytdlp("-f", "bv*[protocol^=m3u8]+ba[protocol^=m3u8]/b[protocol^=m3u8]", "-S", BEST, "-g",
                              f"https://www.youtube.com/watch?v={vid}"), capture_output=True, text=True, timeout=180)
    lists = [u for u in p.stdout.split() if u.startswith("http")]
    if p.returncode != 0 or not lists:
        raise ClipError(_last_error(p.stderr))

    def get(url: str) -> bytes:
        for i in range(3):
            if stop is not None and stop.is_set():
                raise Cancelled()
            try:
                with urllib.request.urlopen(url, timeout=30) as r:
                    return r.read()
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise ClipError("YouTube no longer keeps this part of the live stream. Make the clip after "
                                    "the stream ends (Finished stream page).")
                if i == 2:
                    raise ClipError(f"YouTube refused a piece of the live stream ({e.code})")
            except (urllib.error.URLError, TimeoutError) as e:
                if i == 2:
                    raise ClipError(f"couldn't download a piece of the live stream: {e}")
            time.sleep(2)
        raise AssertionError

    jobs, parts = [], []
    for k, url in enumerate(lists[:2]):
        seg = next((l for l in get(url).decode(errors="replace").splitlines() if "/sq/" in l), None)
        if not seg:
            raise ClipError("couldn't read the live playlist")
        dur = float(m.group(1)) if (m := re.search(r"/dur/([\d.]+)/", seg)) else 2.0
        first, last = max(0, math.floor(a / dur) - 1), math.ceil(b / dur) + 1  # one spare piece each side
        names = []
        for n in range(first, last + 1):
            names.append(f"p{k}_{n}.ts")
            jobs.append((re.sub(r"/sq/\d+/", f"/sq/{n}/", seg), work / names[-1]))
        m3u8 = work / f"p{k}.m3u8"
        m3u8.write_text(f"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:{math.ceil(dur)}\n"
                        f"#EXT-X-MEDIA-SEQUENCE:{first}\n" +
                        "".join(f"#EXTINF:{dur:.3f},\n{x}\n" for x in names) + "#EXT-X-ENDLIST\n")
        parts.append(m3u8)

    done, want = 0, b - a + LIVE_MARGIN + 10  # progress in the same units as _download_live's

    def fetch(job: tuple[str, Path]) -> None:
        job[1].write_bytes(get(job[0]))

    with ThreadPoolExecutor(6) as pool:
        for _ in pool.map(fetch, jobs):
            done += 1
            if on_time:
                on_time(done / len(jobs) * want)

    def start(m3u8: Path) -> float:
        out = subprocess.run(["ffprobe", "-v", "error", "-allowed_extensions", "ALL", "-show_entries",
                              "format=start_time", "-of", "csv=p=0", str(m3u8)],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        try:
            return float(out)
        except ValueError:
            raise ClipError(f"couldn't read where the downloaded piece starts ({m3u8.name})")

    v0 = start(parts[0])
    a0 = start(parts[1]) if len(parts) > 1 else v0
    src = work / "src.mkv"
    cmd = ["ffmpeg", "-v", "error", "-y"]
    for m3u8 in parts:
        cmd += ["-allowed_extensions", "ALL", "-i", str(m3u8)]
    cmd += ["-map", "0:v:0", "-map", f"{len(parts) - 1}:a:0?", "-c", "copy", str(src)]
    rc, err = _run(cmd, timeout=300, log=work / "ffmpeg.log", stop=stop)
    if rc != 0 or not src.exists():
        raise ClipError("ffmpeg: " + _last_error(err))
    _download_pieces.info = (f"{len(jobs)} pieces by number: video starts at stream time {v0:.1f} s, audio at "
                             f"{a0:.1f} s ({a0 - v0:+.1f} s), the clip at {a:.1f} s")
    if a < max(v0, a0) - 0.5:
        raise ClipError(f"the downloaded piece starts {max(v0, a0) - a:.0f} s after the clip start")
    return src, max(0.0, a - v0), max(0.0, a - a0)


def _quality(src: Path, log: str) -> str:
    """e.g. "1920×1080 · 60 fps · avc1 + mp4a (YouTube formats 312+140)"."""
    fmt = re.search(r"Downloading 1 format\(s\): (\S+)", log)
    try:
        info = json.loads(subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                          "stream=width,height,avg_frame_rate,codec_name", "-of", "json", str(src)],
                                         capture_output=True, text=True, timeout=20).stdout)["streams"][0]
        n, d = info["avg_frame_rate"].split("/")
        fps = round(int(n) / int(d)) if int(d) else 0
        q = f"{info['width']}×{info['height']} · {fps} fps · {info['codec_name']}"
    except (ValueError, KeyError, IndexError, subprocess.SubprocessError, ZeroDivisionError):
        q = "unknown"
    return q + (f" (YouTube formats {fmt.group(1)})" if fmt else "")


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
            if j["video"] == vid and (j["state"] not in ("done", "cancelled") or j["peak_name"] not in out):
                out[j["peak_name"]] = {x: j[x] for x in ("state", "note", "peak", "start", "end", "run")}
                out[j["peak_name"]]["steps"] = [[st.key, st.label, st.status, st.text, st.progress]
                                                for st in j["_run"].steps.values()]
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
        a, b = moment["clip_start"], moment["clip_end"]
        run = activity.start("clip", f"Reel · {channel or vid} · moment {hms(moment['peak'])}",
                             {"video": vid, "title": title, "peak": moment["peak"], "clip": [a, b]}, STEPS)
        busy = sum(1 for x in _jobs.values() if x["state"] in ("queued", "waiting", "working"))
        run.step("queue").begin(f"{busy} reel(s) before this one" if busy else "starting now")
        run.step("queue").log(("made by hand" if not auto else "made on its own: new live moment") +
                              f" · {hms(a)} → {hms(b)} ({int(b - a)} s)")
        j = {"video": vid, "peak": moment["peak"], "peak_name": name, "start": a, "end": b, "channel": channel,
             "title": title, "state": "queued", "note": f"{busy} reel(s) before this one" if busy else "",
             "run": run.id, "_run": run, "_stop": threading.Event()}
        _jobs[k] = j
        _q.put(j)
        _ensure_worker()
    return j


def cancel(vid: str, peak: float) -> bool:
    """Stop a reel that is in line or being made (its yt-dlp / ffmpeg are ended at once)."""
    with _lock:
        j = _jobs.get(_key(vid, peak))
        if not j or j["state"] not in ("queued", "waiting", "working"):
            return False
        j["_stop"].set()
        if j["state"] == "queued":  # not started yet: the worker will skip it
            j["state"], j["note"] = "cancelled", "cancelled"
            j["_run"].step("queue").done("cancelled")
            j["_run"].finish("cancelled", "cancelled")
    return True


def _ensure_worker() -> None:
    global _worker
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_loop, name="clips", daemon=True)
        _worker.start()


def _loop() -> None:
    while True:
        j = _q.get()
        if j["_stop"].is_set():
            continue
        try:
            _make(j)
        except Exception as e:  # never let one clip stop the queue
            j["state"], j["note"] = "error", f"unexpected error: {e}"
            j["_run"].finish("error", j["note"])


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
    vid, a, b, run, stop = j["video"], j["start"], j["end"], j["_run"], j["_stop"]
    length = b - a
    j["state"] = "working"
    run.step("queue").done("its turn")
    work = CLIPS / vid / f"_work_{j['peak_name']}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        with run.step("check") as st:
            j["note"] = "checking the stream…"
            status = live.live_status(vid)
            lay = layout_for(j["channel"], vid)
            st.set(stream=status or "unknown", face_cam=lay["face"] or "none", game_box=lay["game"] or "whole screen")
            if status == "post_live":
                raise ClipError("the stream just ended and YouTube is still processing the recording. "
                                "Try again in a while.")
            started = _started(vid) if status == "is_live" else None
            if status == "is_live" and started is None:
                raise ClipError("don't know when this live stream started: open it on the Live page first")
            st.done(("live now: the piece comes from the live feed" if status == "is_live" else
                     "finished: the piece comes from the recording") +
                    (" · face cam on top" if lay["face"] else " · no face cam: middle of the screen only"))
        if status == "is_live":
            if b - (time.time() - started) > 600:
                raise ClipError(f"the clip ends at {hms(b)}, but the stream is only at {hms(time.time() - started)}")
            with run.step("wait") as st:
                j["state"] = "waiting"
                total = max(1.0, b + LIVE_READY - (time.time() - started))
                while (lag := b + LIVE_READY - (time.time() - started)) > 0:
                    j["note"] = f"the end of the clip reaches YouTube in {int(lag)} s"
                    st.detail(j["note"], 1 - lag / total)
                    if stop.wait(min(lag, 1)):
                        raise Cancelled()
                old = time.time() - started - a > LIVE_BACK_MAX
                st.done("the whole clip is on YouTube" + (" · older than the ~15 min live playlist: its pieces "
                                                           "are fetched by number" if old else " live feed"))
            j["state"] = "working"
        else:
            run.step("wait").skip("not needed: the stream has ended")
        with run.step("download") as st:
            where = "live feed" if status == "is_live" else "recording"
            j["note"] = "downloading the piece…"
            st.detail(f"{hms(a)} → {hms(b)} ({int(length)} s) from the {where}", 0)
            t0 = time.time()
            # The live download also has a few seconds before the clip (see LIVE_MARGIN).
            want = length + (LIVE_MARGIN + 10 if status == "is_live" else 0)

            def dl_time(t: float) -> None:
                j["note"] = f"downloading… {int(t)} of ~{int(want)} s"
                st.detail(f"{int(t)} of ~{int(want)} s of video from the {where} · {time.time() - t0:.0f} s so far",
                          min(0.99, t / want))

            old = status == "is_live" and time.time() - started - a > LIVE_BACK_MAX
            fn = _download_pieces if old else _download_live if status == "is_live" else None
            src, offset, a_offset = (_download_pieces(vid, a, b, work, dl_time, stop) if old else
                                     _download_live(vid, a, b, started, work, dl_time, stop) if status == "is_live"
                                     else _download_finished(vid, a, b, work, dl_time, stop))
            if fn and getattr(fn, "info", ""):
                st.log(fn.info)
            dl_log = work / ("ffmpeg.log" if status == "is_live" else "download.log")
            quality = _quality(src, dl_log.read_text(errors="replace") if dl_log.exists() else "")
            st.log(f"quality: {quality}")
            st.set(quality=quality)
            st.set(file_MB=round(src.stat().st_size / 1e6, 1), seconds=round(time.time() - t0),
                   video_starts_at=f"{offset:.2f} s into the file", audio_starts_at=f"{a_offset:.2f} s into the file")
            st.done(f"{quality.split(' (')[0]} · {src.stat().st_size / 1e6:.1f} MB in {time.time() - t0:.0f} s · "
                    f"audio lined up ({a_offset - offset:+.1f} s)")
        with run.step("render") as st:
            j["note"] = "making the reel…"
            st.detail("face cam on top + middle of the screen" if lay["face"] else "middle of the screen only", 0)
            tmp = work / "out.mp4"
            t0 = time.time()

            def enc_time(t: float) -> None:
                j["note"] = f"making the reel… {int(t / length * 100)}%"
                st.detail(f"{int(t)} of {int(length)} s encoded", min(0.99, t / length))

            # The same file twice: the video cut at its offset, the audio at its own (they can differ, live).
            rc, err = _run(["ffmpeg", "-v", "error", "-stats", "-y", "-ss", f"{offset:.3f}", "-i", str(src),
                            "-ss", f"{a_offset:.3f}", "-i", str(src),
                            "-t", f"{length:.2f}", "-filter_complex", _filter(lay["face"], lay["game"]),
                            "-map", "[v]", "-map", "1:a?",
                            # High quality: keep the source frame rate (60 fps), near-lossless x264.
                            "-c:v", "libx264", "-preset", "slow", "-crf", "16", "-profile:v", "high",
                            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                            "-movflags", "+faststart", str(tmp)], timeout=1800, log=work / "render.log",
                           on_time=enc_time, stop=stop)
            if rc != 0 or not tmp.exists():
                raise ClipError("ffmpeg: " + _last_error(err))
            st.done(f"1080×1920 · {int(length)} s · encoded in {time.time() - t0:.0f} s")
        with run.step("save") as st:
            out = clip_path(vid, j["peak"])
            # A frame of the full screen, so the face-cam box can be drawn on the page.
            _run(["ffmpeg", "-v", "error", "-y", "-ss", f"{offset + length / 2:.2f}", "-i", str(src),
                  "-frames:v", "1", "-vf", "scale=960:-2", str(out.with_suffix(".jpg"))], timeout=60)
            tmp.replace(out)
            out.with_suffix(".json").write_text(json.dumps({
                "peak": j["peak"], "start": a, "end": b, "channel": j["channel"], "title": j["title"],
                "face": lay["face"], "game": lay["game"], "made": time.time(), "size": out.stat().st_size}))
            st.set(file=str(out.relative_to(ROOT)))
            st.done(f"{out.relative_to(ROOT)} · {out.stat().st_size / 1e6:.1f} MB")
        j["state"], j["note"] = "done", ""
        run.finish("done", f"reel ready · {int(length)} s")
    except Cancelled:
        j["state"], j["note"] = "cancelled", "cancelled"
        for st in run.steps.values():
            if st.status == "running":
                st.log("cancelled")
        run.finish("cancelled", "cancelled")
    except ClipError as e:
        j["state"], j["note"] = "error", str(e)
        for st in run.steps.values():
            if st.status == "running":
                st.fail(str(e))
        run.finish("error", str(e))
    finally:
        shutil.rmtree(work, ignore_errors=True)
