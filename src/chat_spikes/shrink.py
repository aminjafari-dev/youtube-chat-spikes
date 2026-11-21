"""Make small upload copies of reels (under 10 MB) for browser uploads.

The Claude in Chrome upload tool takes at most 10 MB per file, while reels are made
almost lossless (often 20-150 MB). This writes a copy that fits, next to the reel:

    clips/<peak>.mp4  ->  clips/upload/<peak>.mp4

The original is never changed. A copy that already exists and is newer than its reel
is reused. Run:

    uv run python -m chat_spikes.shrink                 # every finished reel in streams/
    uv run python -m chat_spikes.shrink path/to/a.mp4   # only these reels

It prints one line per reel: OK <copy path> <size MB>, or FAIL <reel path> <reason>.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LIMIT = 10 * 1024 * 1024          # the upload tool's limit
TARGET = int(9.0 * 1024 * 1024)   # aim below it: two-pass sizes land within a few %
AUDIO_KBPS = 96
FPS = 30                           # halving 60 fps gives each frame twice the bits
SETTLE_SECONDS = 120               # skip reels still being written


def duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout
    return float(json.loads(out)["format"]["duration"])


def height_for(video_kbps: float) -> int:
    # Fewer pixels look better than a starved 1080p at low bitrates.
    if video_kbps >= 2500:
        return 1920
    if video_kbps >= 1000:
        return 1280
    return 960


def encode(src: Path, dst: Path, video_kbps: int) -> None:
    h = height_for(video_kbps)
    vf = f"scale=-2:{h}:flags=lanczos,fps={FPS}"
    with tempfile.TemporaryDirectory() as tmp:
        log = str(Path(tmp) / "pass")
        common = ["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", vf,
                  "-c:v", "libx264", "-preset", "slow", "-b:v", f"{video_kbps}k",
                  "-pix_fmt", "yuv420p", "-passlogfile", log]
        subprocess.run(common + ["-pass", "1", "-an", "-f", "mp4", "/dev/null"], check=True)
        subprocess.run(common + ["-pass", "2", "-c:a", "aac", "-b:a", f"{AUDIO_KBPS}k",
                                 "-movflags", "+faststart", str(dst)], check=True)


def shrink(src: Path) -> Path:
    dst = src.parent / "upload" / src.name
    if dst.exists() and dst.stat().st_mtime >= src.stat().st_mtime and dst.stat().st_size < LIMIT:
        return dst
    dst.parent.mkdir(exist_ok=True)
    secs = duration(src)
    if secs < 1:
        raise ValueError(f"too short ({secs:.2f}s)")
    if src.stat().st_size < TARGET:
        # Already small enough: a plain copy keeps full quality.
        dst.write_bytes(src.read_bytes())
        return dst
    total_kbps = TARGET * 8 / 1000 / secs
    video_kbps = int((total_kbps - AUDIO_KBPS) * 0.97)
    for _ in range(3):
        if video_kbps < 150:
            raise ValueError(f"too long to fit 10 MB ({secs:.0f}s)")
        encode(src, dst, video_kbps)
        if dst.stat().st_size < LIMIT:
            return dst
        video_kbps = int(video_kbps * 0.85)
    dst.unlink(missing_ok=True)
    raise ValueError("could not get under 10 MB")


def finished_reels(root: Path) -> list[Path]:
    now = time.time()
    return sorted(
        (p for p in root.glob("streams/*/*/clips/*.mp4")
         if now - p.stat().st_mtime > SETTLE_SECONDS),
        key=lambda p: p.stat().st_mtime,
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    reels = [Path(a) for a in args] if args else finished_reels(Path.cwd())
    failed = 0
    for src in reels:
        try:
            dst = shrink(src)
            print(f"OK {dst} {dst.stat().st_size / 1024 / 1024:.1f}MB", flush=True)
        except Exception as e:  # keep going: one bad reel must not stop the rest
            failed += 1
            print(f"FAIL {src} {e}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
