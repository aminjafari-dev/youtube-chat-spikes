"""chat-spikes: find when a YouTube stream's chat explodes.

    uv run chat-spikes <youtube-url>            # finished stream or live stream
    uv run chat-spikes <youtube-url> --window 5 --threshold 3
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

from .detector import Detector, Rules, Window, group_spikes
from .report import hms, write_chart, write_spikes, write_windows, youtube_link
from .youtube import ChatError, open_chat

ROOT = Path(__file__).resolve().parents[2]


def video_id_from(url: str) -> str:
    m = re.search(r"(?:v=|youtu\.be/|/live/|/shorts/|/embed/)([\w-]{11})", url)
    if m:
        return m.group(1)
    if re.fullmatch(r"[\w-]{11}", url):
        return url
    sys.exit(f"Couldn't find a YouTube video ID in: {url}")


def load_ignore_users() -> set[str]:
    f = ROOT / "config" / "ignore_users.txt"
    if not f.exists():
        return set()
    return {l.strip().lower() for l in f.read_text().splitlines() if l.strip() and not l.startswith("#")}


def print_window(w: Window, live: bool, wall: float | None) -> None:
    bar = "█" * min(int(w.score), 60)
    when = datetime.fromtimestamp(wall).strftime("%H:%M:%S") if wall else hms(w.start)
    tag = f"  <<< SPIKE {w.ratio:.1f}x normal" if w.is_spike else ""
    if live or w.is_spike:
        print(f"{when}  {w.score:5.0f}  {bar}{tag}", flush=True)


def main() -> None:
    p = argparse.ArgumentParser(description="Find chat spikes in a YouTube stream (Option A: rules, no AI).")
    p.add_argument("url", help="YouTube video or live URL")
    p.add_argument("--window", type=int, default=10, help="seconds per window (default 10)")
    p.add_argument("--threshold", type=float, default=2.5, help="spike = this many times normal (default 2.5)")
    p.add_argument("--min-score", type=float, default=8, help="minimum score to count as a spike (default 8)")
    p.add_argument("--lead", type=int, default=20, help="clip starts this many seconds before the spike (default 20)")
    p.add_argument("--tail", type=int, default=15, help="clip ends this many seconds after the spike (default 15)")
    p.add_argument("--refresh", action="store_true", help="download the chat again even if it's saved")
    a = p.parse_args()

    vid = video_id_from(a.url)
    rules = Rules(window=a.window, threshold=a.threshold, min_score=a.min_score,
                  lead=a.lead, tail=a.tail, ignore_users=load_ignore_users())
    raw_dir = ROOT / "data" / "raw"
    out_dir = ROOT / "output" / vid
    raw_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = raw_dir / f"{vid}.jsonl"
    meta_file = raw_dir / f"{vid}.meta.json"

    det = Detector(rules)
    title, live = vid, False
    started = time.time()

    if cache.exists() and meta_file.exists() and not a.refresh:
        meta = json.loads(meta_file.read_text())
        title = meta.get("title") or vid
        print(f"Using saved chat for: {title}")
        for line in cache.open():
            t, author, text = json.loads(line)
            for w in det.add(t, author, text):
                print_window(w, False, None)
    else:
        try:
            info, source = open_chat(vid)
        except (ChatError, requests.RequestException) as e:
            sys.exit(f"Couldn't get chat: {e}")
        title, live = info.title, info.live
        print(f"{'LIVE' if live else 'Replay'}: {title}")
        print("Watching chat... press Ctrl+C to stop." if live else "Downloading chat (long streams take a few minutes)...")
        n = 0
        with cache.open("w") as f:
            try:
                for m in source:
                    f.write(json.dumps([m.t, m.author, m.text], ensure_ascii=False) + "\n")
                    n += 1
                    for w in det.add(m.t, m.author, m.text):
                        print_window(w, live, started + w.start if live else None)
                    if not live and n % 5000 == 0:
                        print(f"  ...{n:,} messages, at {hms(m.t)}", flush=True)
            except KeyboardInterrupt:
                print("\nStopped.")
            except ChatError as e:
                print(f"\nChat stopped early: {e}")
        # Only reuse finished streams next time; a live recording is partial.
        if not live:
            meta_file.write_text(json.dumps({"title": title, "messages": n}))

    det.flush()
    spikes = group_spikes(det.windows, rules)
    write_windows(out_dir / "windows.csv", det.windows)
    write_spikes(out_dir / "spikes.csv", spikes, vid)
    write_chart(out_dir / "chart.html", det.windows, spikes, rules, title, vid)

    total = sum(w.raw for w in det.windows)
    print(f"\n{total:,} messages in {len(det.windows)} windows; {len(spikes)} moments found.")
    for i, s in enumerate(sorted(spikes, key=lambda s: -s.peak.ratio)[:10], 1):
        print(f"{i:2}. {hms(s.clip_start)} → {hms(s.clip_end)}  {s.peak.ratio:4.1f}x normal  "
              f"{youtube_link(vid, s.clip_start)}")
    print(f"\nResults: {out_dir}/  (chart.html, spikes.csv, windows.csv)")


if __name__ == "__main__":
    main()
