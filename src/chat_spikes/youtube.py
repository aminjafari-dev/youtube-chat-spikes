"""Read YouTube chat (live or replay) through the same internal API the youtube.com chat box uses.

No API key and no login needed. If YouTube changes its page, this file is the one to fix.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Iterator

import requests

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
BASE = "https://www.youtube.com"


class ChatError(Exception):
    pass


@dataclass
class Message:
    t: float      # seconds from stream start (replay) or since we started listening (live)
    author: str
    text: str
    paid: bool = False


@dataclass
class VideoInfo:
    video_id: str
    title: str
    live: bool
    continuation: str
    client_version: str


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    # Skip the EU cookie-consent page (choose "reject all").
    s.cookies.set("SOCS", "CAI", domain=".youtube.com")
    return s


def _extract_json(page: str, marker: str) -> dict:
    i = page.find(marker)
    if i < 0:
        raise ChatError(f"YouTube page changed: {marker!r} not found")
    i = page.find("{", i)
    return json.JSONDecoder().raw_decode(page[i:])[0]


def _first(d: dict, *path):
    for p in path:
        if isinstance(d, list):
            d = d[p] if len(d) > p else None
        elif isinstance(d, dict):
            d = d.get(p)
        if d is None:
            return None
    return d


def _continuation_of(conts: list | None) -> tuple[str | None, int]:
    """Return (token, wait_ms) from a 'continuations' list."""
    for c in conts or []:
        for v in c.values():
            if isinstance(v, dict) and "continuation" in v:
                return v["continuation"], int(v.get("timeoutMs", 0))
    return None, 0


def get_video_info(session: requests.Session, video_id: str) -> VideoInfo:
    page = session.get(f"{BASE}/watch", params={"v": video_id}, timeout=20).text
    data = _extract_json(page, "ytInitialData")
    player = _extract_json(page, "ytInitialPlayerResponse")
    details = player.get("videoDetails", {})
    title = details.get("title", video_id)
    live = bool(details.get("isLive"))
    ver = "2.20241101.00.00"
    k = page.find('"INNERTUBE_CONTEXT_CLIENT_VERSION":"')
    if k >= 0:
        ver = page[k + 36:page.find('"', k + 36)]
    chat = _first(data, "contents", "twoColumnWatchNextResults", "conversationBar", "liveChatRenderer")
    if not chat:
        raise ChatError("This video has no chat (or chat replay is turned off).")
    # Prefer "All chat" (sub-menu item 1) over "Top chat" so nothing is filtered out.
    token = None
    items = _first(chat, "header", "liveChatHeaderRenderer", "viewSelector", "sortFilterSubMenuRenderer", "subMenuItems")
    if items and len(items) > 1:
        token = _first(items[1], "continuation", "reloadContinuationData", "continuation")
    if not token:
        token, _ = _continuation_of(chat.get("continuations"))
    if not token:
        raise ChatError("Couldn't find the chat on this video.")
    return VideoInfo(video_id, title, live, token, ver)


def _text(runs: list | None) -> str:
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            out.append(e["shortcuts"][0] if e.get("isCustomEmoji") and e.get("shortcuts") else e.get("emojiId", ""))
    return "".join(out)


def _parse_item(item: dict) -> tuple[str, str, int, bool] | None:
    for key, paid in (("liveChatTextMessageRenderer", False), ("liveChatPaidMessageRenderer", True)):
        r = item.get(key)
        if r:
            author = _first(r, "authorName", "simpleText") or "?"
            text = _text(_first(r, "message", "runs"))
            return author, text, int(r.get("timestampUsec", 0)), paid
    return None


def iter_chat(video: VideoInfo, session: requests.Session | None = None) -> Iterator[Message]:
    """Yield chat messages in time order. Live: runs until the stream ends or Ctrl+C."""
    s = session or _session()
    endpoint = "get_live_chat" if video.live else "get_live_chat_replay"
    token: str | None = video.continuation
    ctx = {"client": {"clientName": "WEB", "clientVersion": video.client_version, "hl": "en"}}
    first_us: int | None = None
    failures = 0

    while token:
        body = {"context": ctx, "continuation": token}
        if not video.live:
            body["currentPlayerState"] = {"playerOffsetMs": "0"}
        try:
            resp = s.post(f"{BASE}/youtubei/v1/live_chat/{endpoint}", params={"prettyPrint": "false"},
                          json=body, timeout=20)
            resp.raise_for_status()
            data = resp.json()
            failures = 0
        except (requests.RequestException, ValueError) as e:
            failures += 1
            if failures > 5:
                raise ChatError(f"YouTube stopped answering: {e}")
            time.sleep(2 * failures)
            continue

        cont = _first(data, "continuationContents", "liveChatContinuation")
        if not cont:
            return  # stream/chat ended
        for action in cont.get("actions", []):
            offset_ms = None
            if "replayChatItemAction" in action:
                rep = action["replayChatItemAction"]
                offset_ms = int(rep.get("videoOffsetTimeMsec", 0))
                inner = rep.get("actions", [])
            else:
                inner = [action]
            for a in inner:
                item = _first(a, "addChatItemAction", "item")
                parsed = _parse_item(item) if item else None
                if not parsed:
                    continue
                author, text, ts_us, paid = parsed
                if offset_ms is not None:
                    t = offset_ms / 1000
                else:
                    first_us = first_us or ts_us
                    t = (ts_us - first_us) / 1e6
                if t >= 0:
                    yield Message(t, author, text, paid)

        token, wait_ms = _continuation_of(cont.get("continuations"))
        if video.live:
            time.sleep(max(wait_ms, 1000) / 1000)


def open_chat(video_id: str) -> tuple[VideoInfo, Iterator[Message]]:
    s = _session()
    info = get_video_info(s, video_id)
    return info, iter_chat(info, s)
