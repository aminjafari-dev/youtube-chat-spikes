"""Where live chat comes from. Each source runs in a thread and puts events on a queue:

    ("info",    {"title": str, "video_id": str})
    ("message", (t_seconds, author, text))
    ("viewers", int)
    ("error",   str)
    ("end",     None)

t_seconds is stream time: seconds since the streamer went live (demo: since Start was pressed).
"""

from __future__ import annotations

import os
import queue
import random
import threading
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
API = "https://www.googleapis.com/youtube/v3"


def load_api_key() -> str | None:
    if os.environ.get("YOUTUBE_API_KEY"):
        return os.environ["YOUTUBE_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.strip().startswith("YOUTUBE_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"\'') or None
    return None


def youtube_api_source(video_id: str, q: queue.Queue, stop: threading.Event, start: float) -> None:
    """Official YouTube Data API v3. Works for streams that are live right now."""
    key = load_api_key()
    if not key:
        q.put(("error", "No YouTube API key. Put YOUTUBE_API_KEY=... in the project's .env file "
                        "(free key from console.cloud.google.com → YouTube Data API v3), or use Demo mode."))
        return
    s = requests.Session()
    try:
        r = s.get(f"{API}/videos", params={"part": "snippet,liveStreamingDetails", "id": video_id, "key": key}, timeout=15)
        data = r.json()
        if "error" in data:
            q.put(("error", f"YouTube API: {data['error'].get('message')}"))
            return
        items = data.get("items") or []
        if not items:
            q.put(("error", "Video not found."))
            return
        v = items[0]
        live = v.get("liveStreamingDetails") or {}
        chat_id = live.get("activeLiveChatId")
        # The stream's own clock starts when the streamer went live.
        started = live.get("actualStartTime")
        stream_start = (datetime.fromisoformat(started.replace("Z", "+00:00")).timestamp()
                        if started else start)
        q.put(("info", {"title": v["snippet"]["title"], "video_id": video_id, "stream_start": stream_start}))
        if not chat_id:
            q.put(("error", "This video isn't live right now (the official API only reads chat during a live stream)."))
            return
        if live.get("concurrentViewers"):
            q.put(("viewers", int(live["concurrentViewers"])))

        page_token = None
        last_viewers = time.time()
        while not stop.is_set():
            params = {"liveChatId": chat_id, "part": "snippet,authorDetails", "maxResults": 2000, "key": key}
            if page_token:
                params["pageToken"] = page_token
            d = s.get(f"{API}/liveChat/messages", params=params, timeout=15).json()
            if "error" in d:
                q.put(("error", f"YouTube API: {d['error'].get('message')}"))
                return
            for it in d.get("items", []):
                sn = it.get("snippet", {})
                ts = datetime.fromisoformat(sn.get("publishedAt", "").replace("Z", "+00:00")).timestamp()
                if ts < start:  # older messages YouTube sends on the first request
                    continue
                t = ts - stream_start  # seconds into the stream (stream time)
                text = sn.get("displayMessage") or ""
                q.put(("message", (t, it.get("authorDetails", {}).get("displayName", "?"), text)))
            page_token = d.get("nextPageToken")
            # Each chat request costs quota; never poll faster than every 5 s.
            wait = max(d.get("pollingIntervalMillis", 5000) / 1000, 5)
            if time.time() - last_viewers > 60:
                last_viewers = time.time()
                vv = s.get(f"{API}/videos", params={"part": "liveStreamingDetails", "id": video_id, "key": key},
                           timeout=15).json()
                cv = ((vv.get("items") or [{}])[0].get("liveStreamingDetails") or {}).get("concurrentViewers")
                if cv:
                    q.put(("viewers", int(cv)))
            stop.wait(wait)
    except requests.RequestException as e:
        q.put(("error", f"Network problem: {e}"))
    finally:
        q.put(("end", None))


DEMO_NAMES = [f"{a}{b}" for a in ("Pixel", "Nova", "Turbo", "Lazy", "Ghost", "Mango", "Rex", "Echo", "Blitz", "Zen")
              for b in ("Fox", "Gamer", "_99", "Cat", "Wolf", "King", "Bee", "Sky")]
NORMAL = ["hi chat", "what game is this", "gg", "lets gooo", "first time here", "love the stream",
          "how long you been playing?", "nice", "hello from Brazil", "can you play with viewers?", "lol",
          "that was close", "what rank are you", "good morning", "chat is slow today"]
HYPE = ["LMAOOOO", "NO WAY 😂😂", "CLIP IT", "💀💀💀", "WHAT WAS THAT", "OMG", "clip that!!", "LOL",
        "bro 😂", "INSANE", "W", "no wayyyy", "🔥🔥🔥", "HOLY", "KEKW"]


def demo_source(video_id: str, q: queue.Queue, stop: threading.Event, start: float) -> None:
    """Fake chat with realistic quiet periods, highlights, a giveaway and a bot."""
    q.put(("info", {"title": "Demo chat (fake messages)", "video_id": video_id, "stream_start": start}))
    rnd = random.Random()
    viewers = rnd.randint(800, 1500)
    q.put(("viewers", viewers))
    next_spike = 45.0
    spike_end = -1.0
    giveaway_at = rnd.uniform(70, 100)
    t = 0.0
    while not stop.is_set():
        now = time.time() - start
        if now >= next_spike and now > spike_end:
            spike_end = now + rnd.uniform(8, 15)
            next_spike = spike_end + rnd.uniform(40, 80)
        hype = now < spike_end
        rate = rnd.uniform(14, 24) if hype else rnd.uniform(1.5, 3.5)
        if abs(now - giveaway_at) < 6:  # a giveaway: lots of !enter that must NOT count
            for _ in range(4):
                q.put(("message", (now, rnd.choice(DEMO_NAMES), "!enter")))
        if rnd.random() < 0.01:
            q.put(("message", (now, "Nightbot", "Follow the stream and join the Discord!")))
        q.put(("message", (now, rnd.choice(DEMO_NAMES), rnd.choice(HYPE if hype else NORMAL))))
        if int(now) % 30 == 0 and int(now) != int(t):
            viewers = max(100, viewers + rnd.randint(-40, 60))
            q.put(("viewers", viewers))
        t = now
        stop.wait(rnd.expovariate(rate))
    q.put(("end", None))


SOURCES = {"youtube": youtube_api_source, "demo": demo_source}
