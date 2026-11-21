"""Find the streamer's face cam with Gemini, from a few random frames of the stream.

Streamers put their cam anywhere (top, bottom, left, right, a corner) or have none. Instead of drawing the
box by hand, this takes FRAMES screenshots at random times of the stream and sends them to Gemini in one
request. Gemini says, for each frame, whether there is a webcam overlay of the real streamer and where
(a box). Faces of game characters don't count: the prompt asks only for the camera overlay.

The answers are combined on this Mac: the cam counts as found only if Gemini sees it in at least half of
the frames, and the box is the median of those boxes (one odd frame can't move it). The result is saved as
this stream's face-cam box (clips.save_layout), so the reels use it, and as the channel's default.

  Finished stream: frames at random times of the recording (ffmpeg seeks in YouTube's video URL).
  Live stream:     frames at random points of the last ~15 minutes (the live playlist), N pieces back.

One Gemini call per stream, a handful of small images: about a cent or less, often free tier.
The frames and the answer are kept in the stream's ai/ folder (facecam.json, facecam/*.jpg).

Run by hand:  uv run python -m chat_spikes.facecam <video id or link> [--save]
"""

from __future__ import annotations

import json
import random
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

from pydantic import BaseModel, Field

from . import gemini
from .library import ai_dir
from .replay import _last_error, _ytdlp

FRAMES = 6            # screenshots sent to Gemini
MIN_SHARE = 0.5       # the cam must be seen in at least this part of the frames
PAD = 0.01            # a little room around Gemini's box (part of the frame)
MAX_SIDE = 0.75       # a "cam" wider or taller than this is the whole screen (IRL stream), not an overlay
FRAME_WIDTH = 1280    # frames are scaled to this width (enough to see the cam; fewer bytes to send)
LIVE_BACK = (5, 420)  # live: pieces back on the live playlist (2 s each, ~15 min listed)


class FaceCamError(Exception):
    pass


class FrameAnswer(BaseModel):
    frame: int = Field(description="The number of the frame (1, 2, 3...), in the order the images were given.")
    has_face_cam: bool = Field(description="True only if this frame shows a webcam overlay of the real streamer "
                                           "(a live camera picture placed over or beside the game).")
    box_2d: list[int] = Field(description="The face-cam overlay's box as [ymin, xmin, ymax, xmax], each 0-1000 "
                                          "relative to the image size. The whole camera rectangle, not only the "
                                          "face. Empty list if there is no face cam.")


class FaceCamAnswer(BaseModel):
    frames: list[FrameAnswer]
    position: str = Field(description="Where the face cam usually is: top-left, top, top-right, left, center, right, "
                                      "bottom-left, bottom, bottom-right, or none.")
    notes: str = Field(description="One short sentence: what you saw (e.g. 'cam with green border in the bottom-left').")


PROMPT = """These {n} images are screenshots taken at random times from one YouTube live stream (a game or IRL stream).

For EACH image, decide if it shows the streamer's FACE CAM: a live webcam picture of the real person streaming,
placed as an overlay (a rectangle or shape over or next to the gameplay). It is usually in a corner or along an
edge, often with a border, and is in the same place in most images.

Do NOT count:
- faces or bodies of characters inside the game (GTA, cutscenes, NPCs), even if they look realistic
- photos, emotes, avatars, logos, chat boxes or alerts
- a stream where the camera IS the whole screen (IRL / just chatting): answer has_face_cam = false

When there is a face cam, give the box of the WHOLE camera rectangle (the full overlay, not only the face) as
box_2d = [ymin, xmin, ymax, xmax], each from 0 to 1000 relative to the image. Answer for every image, numbered
1 to {n} in the order given."""


# ---------- frames ----------

LIVE_FMT = "bv*[protocol^=m3u8][height<=720]/bv*[protocol^=m3u8]/b[protocol^=m3u8]"
FINISHED_FMT = "bv*[height<=720]/bv*/b"


def _length(vid: str) -> float | None:
    p = subprocess.run(_ytdlp("--print", "%(duration)s", f"https://www.youtube.com/watch?v={vid}"),
                       capture_output=True, text=True, timeout=180)
    try:
        return float(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        if p.returncode != 0:
            raise FaceCamError("yt-dlp: " + _last_error(p.stderr)) from None
        return None


def _grab(vid: str, out: Path, seek: float | None = None, back: int | None = None) -> bool:
    """One screenshot. yt-dlp downloads ~1 s of video there (YouTube refuses plain ffmpeg: 403), ffmpeg takes
    its first frame."""
    tmp = out.with_suffix(".part.mkv")
    for f in out.parent.glob(out.stem + ".part*"):
        f.unlink()
    url = f"https://www.youtube.com/watch?v={vid}"
    if back is not None:  # live: N pieces back on the live playlist
        args = ("-f", LIVE_FMT, "--downloader", "ffmpeg",
                "--downloader-args", f"ffmpeg_i:-live_start_index -{back}",
                "--downloader-args", "ffmpeg_o:-t 1", "-o", str(tmp), url)
    else:
        args = ("-f", FINISHED_FMT, "--download-sections", f"*{seek:.1f}-{seek + 1:.1f}",
                "--merge-output-format", "mkv", "-o", str(tmp), url)
    try:
        subprocess.run(_ytdlp(*args), capture_output=True, timeout=180)
        src = next((p for p in out.parent.glob(out.stem + ".part*") if p.suffix != ".part"), None)
        if not src:
            return False
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", str(src), "-frames:v", "1",
                        "-vf", f"scale={FRAME_WIDTH}:-2", "-q:v", "3", str(out)], capture_output=True, timeout=60)
    except subprocess.TimeoutExpired:
        return False
    finally:
        for f in out.parent.glob(out.stem + ".part*"):
            f.unlink()
    return out.exists() and out.stat().st_size > 2000


def grab_frames(vid: str, live: bool, n: int = FRAMES, log=print) -> list[Path]:
    """n screenshots at random times of the stream, saved in ai/facecam/."""
    length = None if live else _length(vid)
    d = ai_dir(vid) / "facecam"
    d.mkdir(parents=True, exist_ok=True)
    for old in d.glob("*.jpg"):
        old.unlink()
    if live:
        spots = sorted(random.sample(range(*LIVE_BACK), n), reverse=True)  # oldest first
    else:
        if not length or length < 60:
            raise FaceCamError("couldn't read the stream's length")
        # spread out: one random time in each of n equal parts of the stream (skipping the first/last 3%)
        lo, hi = length * 0.03, length * 0.97
        part = (hi - lo) / n
        spots = [lo + part * i + random.uniform(0, part) for i in range(n)]
    frames = []
    for i, s in enumerate(spots, 1):
        out = d / f"{i}.jpg"
        ok = _grab(vid, out, back=s) if live else _grab(vid, out, seek=s)
        where = f"{s * 2 // 60:.0f} min back" if live else time.strftime("%H:%M:%S", time.gmtime(s))
        log(f"frame {i}: {where} {'ok' if ok else 'failed'}")
        if ok:
            frames.append(out)
    if len(frames) < 2:
        raise FaceCamError("couldn't take screenshots of the stream")
    return frames


# ---------- Gemini ----------

def _call(model: str, frames: list[Path]) -> FaceCamAnswer:
    from google import genai
    from google.genai import errors, types

    client = genai.Client(api_key=gemini.api_key(), http_options=types.HttpOptions(
        timeout=gemini.TIMEOUT * 1000, retry_options=types.HttpRetryOptions(attempts=1)))
    parts = [types.Part.from_bytes(data=f.read_bytes(), mime_type="image/jpeg") for f in frames]
    try:
        resp = client.models.generate_content(
            model=model,
            contents=parts + [PROMPT.format(n=len(frames))],
            config=types.GenerateContentConfig(response_mime_type="application/json",
                                               response_schema=FaceCamAnswer, temperature=0.1))
    except errors.APIError as e:
        raise gemini.GeminiError(f"{e.code}|{e.message or e}") from e
    except Exception as e:
        raise gemini.GeminiError(f"504|no answer within {gemini.TIMEOUT} s ({type(e).__name__})") from e
    if resp.parsed is None:
        raise gemini.GeminiError(f"0|no usable answer: {(resp.text or '')[:200]}")
    return resp.parsed


def ask(frames: list[Path], log=print) -> tuple[str, FaceCamAnswer]:
    """One request with all frames, trying the models in order (same spacing and lock as gemini.py)."""
    if not gemini.api_key():
        raise FaceCamError("no GEMINI_API_KEY (in .env)")
    last = "no model available"
    with gemini._lock:
        for model in gemini.model_chain():
            for attempt in range(2):
                wait = gemini.MIN_GAP - (time.time() - gemini._last_call)
                if wait > 0:
                    time.sleep(wait)
                gemini._last_call = time.time()
                log(f"asking {model} about {len(frames)} frames")
                try:
                    return model, _call(model, frames)
                except gemini.GeminiError as e:
                    code, _, msg = str(e).partition("|")
                    last = f"{model}: {code} {msg[:160]}"
                    log(last)
                    if code == "429" and not re.search(r"per ?day|PerDay|limit: 0", msg, re.I) and attempt == 0:
                        time.sleep(30)
                        continue
                    if code in ("500", "503", "504", "0") and attempt == 0:
                        time.sleep(3)
                        continue
                    if code in ("400", "401", "403") and "not found" not in msg.lower():
                        raise FaceCamError(f"Gemini refused the request: {msg[:200]}") from None
                    break
    raise FaceCamError(f"Gemini couldn't answer. Last error: {last}")


def combine(answer: FaceCamAnswer, n: int) -> list[float] | None:
    """The face-cam box [x, y, w, h] (parts of the frame), or None if there is no face cam."""
    boxes = []
    for f in answer.frames:
        if not f.has_face_cam or len(f.box_2d) != 4:
            continue
        y0, x0, y1, x1 = (min(1000, max(0, v)) / 1000 for v in f.box_2d)
        if x1 - x0 < 0.03 or y1 - y0 < 0.03:
            continue
        if x1 - x0 > MAX_SIDE and y1 - y0 > MAX_SIDE:
            continue  # the camera is the whole screen: not an overlay
        boxes.append((x0, y0, x1, y1))
    if len(boxes) < max(2, round(n * MIN_SHARE)):
        return None
    x0, y0, x1, y1 = (statistics.median(b[i] for b in boxes) for i in range(4))
    x0, y0 = max(0.0, x0 - PAD), max(0.0, y0 - PAD)
    x1, y1 = min(1.0, x1 + PAD), min(1.0, y1 + PAD)
    return [round(x0, 4), round(y0, 4), round(x1 - x0, 4), round(y1 - y0, 4)]


def detect(vid: str, channel: str = "", live: bool = False, save: bool = True, log=print) -> dict:
    """Screenshots → Gemini → the face-cam box. Saved as this stream's layout when save is True."""
    frames = grab_frames(vid, live, log=log)
    model, answer = ask(frames, log=log)
    face = combine(answer, len(frames))
    seen = sum(1 for f in answer.frames if f.has_face_cam)
    result = {"face": face, "position": answer.position if face else "none", "notes": answer.notes,
              "model": model, "frames": len(frames), "frames_with_cam": seen, "live": live, "at": time.time(),
              "answer": answer.model_dump()}
    (ai_dir(vid) / "facecam.json").write_text(json.dumps(result, ensure_ascii=False, indent=1))
    log(f"{model}: face cam {'at ' + answer.position + ' ' + str(face) if face else 'not found'} "
        f"({seen} of {len(frames)} frames) · {answer.notes}")
    if save:
        from . import clips
        game = clips.layout_for(channel)["game"] if face else None  # keep the channel's game box, if any
        clips.save_layout(channel, vid, face, game)
    return result


def needed(vid: str) -> bool:
    """True if this stream has no face-cam box yet (found by Gemini or drawn by hand)."""
    from . import clips
    return f"video:{vid}" not in clips.layouts()


def main(argv: list[str] | None = None) -> int:
    from . import library
    from .cli import video_id_from_or_none
    from .live import live_status
    args = sys.argv[1:] if argv is None else argv
    if not args:
        print(__doc__.strip().splitlines()[-1])
        return 2
    vid = video_id_from_or_none(args[0]) or args[0]
    info = library.info(vid)
    live = live_status(vid) == "is_live"
    try:
        r = detect(vid, info.get("channel", ""), live, save="--save" in args)
    except (FaceCamError, gemini.GeminiError) as e:
        print(f"error: {e}")
        return 1
    print(json.dumps({k: r[k] for k in ("face", "position", "notes", "frames_with_cam", "frames", "model")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
