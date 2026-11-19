"""chat-spikes: find the biggest chat spikes in a finished YouTube stream.

    uv run chat-spikes <youtube-url>
    uv run chat-spikes <youtube-url> --window 5 --threshold 3 --refresh
"""

from __future__ import annotations

import argparse
import re
import sys
import threading
from pathlib import Path

from .detector import Detector, Rules, group_spikes
from .report import hms, write_chart, write_spikes, write_windows, youtube_link

ROOT = Path(__file__).resolve().parents[2]


def video_id_from_or_none(url: str) -> str | None:
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([\w-]{11})", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[\w-]{11}", url):
        return url
    return None


def video_id_from(url: str) -> str:
    return video_id_from_or_none(url) or sys.exit(f"Couldn't find a YouTube video ID in: {url}")


def load_ignore_users() -> set[str]:
    f = ROOT / "config" / "ignore_users.txt"
    if not f.exists():
        return set()
    return {l.strip().lower() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")}


def main() -> None:
    from .library import chat_file, migrate, results_dir
    from .replay import ReplayError, load

    p = argparse.ArgumentParser(description="Find the biggest chat spikes in a finished YouTube stream.")
    p.add_argument("url", help="YouTube link of a stream that has ended")
    p.add_argument("--window", type=int, default=10, help="seconds per window (default 10)")
    p.add_argument("--threshold", type=float, default=2.5, help="spike = this many times normal (default 2.5)")
    p.add_argument("--min-score", type=float, default=8, help="minimum score to count as a spike (default 8)")
    p.add_argument("--lead", type=int, default=20, help="clip starts this many seconds before the spike (default 20)")
    p.add_argument("--tail", type=int, default=15, help="clip ends this many seconds after the spike (default 15)")
    p.add_argument("--top", type=int, default=10, help="how many moments to list (default 10)")
    p.add_argument("--refresh", action="store_true", help="download the chat again even if it's saved")
    a = p.parse_args()

    vid = video_id_from(a.url)
    migrate()
    if a.refresh:
        for f in (chat_file(vid, "replay.jsonl"), chat_file(vid, "replay.meta.json")):
            f.unlink(missing_ok=True)
    rules = Rules(window=a.window, threshold=a.threshold, min_score=a.min_score,
                  lead=a.lead, tail=a.tail, ignore_users=load_ignore_users())
    det = Detector(rules)
    title = vid
    last_pct = [-10.0]

    def on_info(info, from_cache):
        nonlocal title
        title = info.title
        print(f"{info.title}  ({hms(info.duration)}, {info.channel})")
        print("Using the saved chat." if from_cache else "Downloading the chat replay (busy streams take a few minutes)...")

    def on_progress(reached, count):
        dur = det_duration[0]
        if dur:
            pct = reached / dur * 100
            if pct - last_pct[0] >= 10:
                last_pct[0] = pct
                print(f"  {pct:3.0f}%  at {hms(reached)}, {count:,} messages", flush=True)

    det_duration = [0.0]
    stop = threading.Event()

    def info_hook(info, from_cache):
        det_duration[0] = info.duration
        on_info(info, from_cache)

    total = 0
    try:
        for t, author, text in load(vid, stop, info_hook, on_progress):
            total += 1
            det.add(t, author, text)
    except ReplayError as e:
        sys.exit(str(e))
    except KeyboardInterrupt:
        stop.set()
        sys.exit("\nCancelled. Nothing was saved.")
    det.flush()

    spikes = group_spikes(det.windows, rules)
    out_dir = results_dir(vid)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_windows(out_dir / "windows.csv", det.windows)
    write_spikes(out_dir / "spikes.csv", spikes, vid)
    write_chart(out_dir / "chart.html", det.windows, spikes, rules, title, vid)

    print(f"\n{total:,} messages; {len(spikes)} moments found. Biggest:")
    for i, s in enumerate(sorted(spikes, key=lambda s: -s.peak.score)[:a.top], 1):
        print(f"{i:2}. {hms(s.peak.start)}  {s.peak.score:5.0f} per 10 s  {s.peak.ratio:4.1f}x normal  "
              f"clip {hms(s.clip_start)}-{hms(s.clip_end)}  {youtube_link(vid, s.clip_start)}")
    print(f"\nResults: {out_dir}/  (chart.html, spikes.csv, windows.csv)")


if __name__ == "__main__":
    main()
