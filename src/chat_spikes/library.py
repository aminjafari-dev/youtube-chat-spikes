"""The stream library: everything saved about one stream lives in one folder, grouped by channel.

    streams/
      <Channel>/
        <YYYY-MM-DD> <Title> [<video id>]/
          stream.json          title, channel, link, start, where it came from (live / finished)
          chat/
            replay.jsonl       full chat replay of a finished stream     [seconds, author, text] per line
            replay.meta.json
            live.jsonl         chat recorded in live mode (same format)
            live.meta.json
            words.json         captions with word times (AI check)
          results/             windows.csv, spikes.csv, chart.html
          ai/                  saved AI answers (<peak>.json, <peak>.chat.json, <peak>.hook.json)
          clips/               reels (<peak>.mp4 + .jpg + .json)

Each folder describes itself (stream.json has the video id), so the app finds every stream by scanning
streams/ at start; there is no separate index to get out of date. A stream is filed under its channel as
soon as the app knows the title and channel. Before that (rare), files go to streams/_unsorted/[<id>]/ and are
moved into place on the first register().

Temp folders of running yt-dlp jobs stay in data/raw/ (they are deleted when the job ends).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIBRARY = ROOT / "streams"
UNSORTED = "_unsorted"
UNKNOWN_CHANNEL = "Unknown channel"

_lock = threading.RLock()
_folders: dict[str, Path] | None = None  # video id -> folder


def _scan() -> dict[str, Path]:
    global _folders
    if _folders is None:
        found = {}
        for f in LIBRARY.glob("*/*/stream.json") if LIBRARY.exists() else []:
            try:
                vid = json.loads(f.read_text()).get("id")
            except (OSError, ValueError):
                continue
            if vid:
                found[vid] = f.parent
        _folders = found
    return _folders


_BAD = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


def safe_name(s: str, limit: int = 80) -> str:
    """A string usable as a folder name on macOS, Windows and Linux."""
    s = _BAD.sub(" ", s or "")
    s = re.sub(r"\s+", " ", s).strip().strip(".")
    if len(s) > limit:
        s = s[:limit].rstrip() + "…"
    return s


def _folder_name(vid: str, title: str, started: float | None) -> str:
    date = time.strftime("%Y-%m-%d", time.localtime(started)) + " " if started else ""
    return f"{date}{safe_name(title) or 'Untitled'} [{vid}]"


def folder(vid: str) -> Path:
    """The stream's folder (may not exist yet)."""
    with _lock:
        return _scan().get(vid) or LIBRARY / UNSORTED / f"[{vid}]"


def info(vid: str) -> dict:
    p = folder(vid) / "stream.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def register(vid: str, title: str, channel: str, started: float | None = None, kind: str = "",
             **extra) -> Path:
    """File the stream under its channel (creates the folder, or moves an unsorted / unknown-channel one)."""
    with _lock:
        folders = _scan()
        old = folders.get(vid) or (LIBRARY / UNSORTED / f"[{vid}]")
        meta = info(vid)
        channel_dir = safe_name(channel) or UNKNOWN_CHANNEL
        known = vid in folders and old.parent.name not in (UNSORTED, UNKNOWN_CHANNEL)
        if known or (old.parent.name == UNKNOWN_CHANNEL and channel_dir == UNKNOWN_CHANNEL):
            dest = old  # keep a filed folder where it is, even if the title changes later
        else:
            dest = LIBRARY / channel_dir / _folder_name(vid, title or meta.get("title", ""),
                                                        started or meta.get("started"))
            dest.parent.mkdir(parents=True, exist_ok=True)
            if old.exists() and old != dest:
                if dest.exists():
                    _merge(old, dest)
                else:
                    old.rename(dest)
                if old.parent.name == UNSORTED and not any(old.parent.iterdir()):
                    old.parent.rmdir()
        dest.mkdir(parents=True, exist_ok=True)
        kinds = set(meta.get("kinds", [])) | ({kind} if kind else set())
        meta.update({k: v for k, v in extra.items() if v is not None})
        meta.update({
            "id": vid, "title": title or meta.get("title") or vid, "channel": channel or meta.get("channel", ""),
            "url": f"https://www.youtube.com/watch?v={vid}", "kinds": sorted(kinds),
            "started": started or meta.get("started"), "added": meta.get("added") or time.time(),
        })
        (dest / "stream.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        folders[vid] = dest
        return dest


def _merge(src: Path, dest: Path) -> None:
    for p in src.rglob("*"):
        if p.is_file():
            t = dest / p.relative_to(src)
            t.parent.mkdir(parents=True, exist_ok=True)
            if not t.exists():
                shutil.move(str(p), t)
    shutil.rmtree(src, ignore_errors=True)


# ---- Paths inside a stream's folder ----

def chat_file(vid: str, name: str) -> Path:
    """name: replay.jsonl, replay.meta.json, live.jsonl, live.meta.json, words.json"""
    return folder(vid) / "chat" / name


def results_dir(vid: str) -> Path:
    return folder(vid) / "results"


def ai_dir(vid: str) -> Path:
    return folder(vid) / "ai"


def clips_dir(vid: str) -> Path:
    return folder(vid) / "clips"


def rel(p: Path) -> str:
    """Path for showing on the page (relative to the project)."""
    try:
        return str(p.relative_to(ROOT))
    except ValueError:
        return str(p)


# ---- The page's library list ----

def streams() -> list[dict]:
    out = []
    with _lock:
        items = list(_scan().items())
    for vid, d in items:
        m = info(vid)
        if not m:
            continue
        chat = d / "chat"
        clips = d / "clips"
        out.append({
            **m, "folder": rel(d),
            "replay": (chat / "replay.jsonl").exists(), "live": (chat / "live.jsonl").exists(),
            "clips": len(list(clips.glob("*.mp4"))) if clips.exists() else 0,
            "results": (d / "results" / "spikes.csv").exists(),
        })
    out.sort(key=lambda s: (s.get("channel") or "").lower())
    return out


def reveal(vid: str) -> bool:
    """Open the stream's folder in Finder / Explorer / the file manager."""
    d = folder(vid)
    if not d.exists():
        return False
    cmd = {"darwin": ["open"], "win32": ["explorer"]}.get(sys.platform, ["xdg-open"])
    subprocess.Popen(cmd + [str(d)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


# ---- One-time move from the old layout (data/raw, data/clips, data/processed/why, output) ----

def migrate() -> int:
    """Move files saved by older versions into the library. Returns how many streams were moved."""
    data = ROOT / "data"
    raw = data / "raw"
    old_places = {
        "replay.jsonl": "{vid}.jsonl", "replay.meta.json": "{vid}.meta.json",
        "live.jsonl": "{vid}.live.jsonl", "live.meta.json": "{vid}.live.meta.json", "words.json": "{vid}.words.json",
    }
    vids: set[str] = set()
    if raw.exists():
        for f in raw.iterdir():
            if f.is_file() and not f.name.startswith("_") and f.name.endswith((".jsonl", ".json")):
                vids.add(f.name.split(".", 1)[0])
    for d in (data / "clips", data / "processed" / "why", ROOT / "output"):
        if d.exists():
            vids |= {p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith("_")
                     and re.fullmatch(r"[\w-]{11}", p.name)}
    moved = 0
    for vid in sorted(vids):
        meta = {}
        for name in (f"{vid}.meta.json", f"{vid}.live.meta.json"):
            try:
                meta = {**json.loads((raw / name).read_text()), **meta}
            except (OSError, ValueError):
                pass
        kinds = [k for k, n in (("finished", f"{vid}.jsonl"), ("live", f"{vid}.live.jsonl")) if (raw / n).exists()]
        dest = register(vid, meta.get("title") or vid, meta.get("channel") or "", meta.get("started"),
                        kinds[0] if kinds else "")
        for kind in kinds[1:]:
            register(vid, "", "", kind=kind)
        for new, old in old_places.items():
            src = raw / old.format(vid=vid)
            if src.exists():
                t = dest / "chat" / new
                t.parent.mkdir(parents=True, exist_ok=True)
                if not t.exists():
                    shutil.move(str(src), t)
        for src, sub in ((data / "clips" / vid, "clips"), (data / "processed" / "why" / vid, "ai"),
                         (ROOT / "output" / vid, "results")):
            if src.exists():
                _merge(src, dest / sub)
        moved += 1
    for d in (data / "clips", data / "processed" / "why", ROOT / "output"):  # leave no empty old folders behind
        try:
            d.rmdir()
        except OSError:
            pass
    return moved
