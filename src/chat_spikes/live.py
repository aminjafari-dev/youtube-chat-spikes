"""Live mode: read the chat of a stream WHILE it is live and find spikes in real time.

Only yt-dlp talks to YouTube (its live chat mode, the same thing it uses to save chat of a live
stream). No API key, no browser, and this code never calls YouTube's chat endpoints itself.

    yt-dlp --skip-download --write-subs --sub-langs live_chat <live url>

yt-dlp asks YouTube for new chat about every 10 s and appends it to a .live_chat.json file.
We read that file as it grows (like replay.py). A live line looks like a replay line, but its
`videoOffsetTimeMsec` counts from when yt-dlp started, so we use each message's `timestampUsec`:

    stream time = timestampUsec - the stream's real start (yt-dlp's release_timestamp)

So moments are always in STREAM time, never the computer's clock.

release_timestamp can be a little early (IShowSpeed, Nov 2026: 149 s before the video's 0:00), so the
live page measures the real start from YouTube's own player (ingestionTime − currentTime) and sends it
to sync(). The saved times are then shifted once, so moments match the video, its ?t= links and the
finished-stream mode. Without an open page, release_timestamp is used.

A watch runs on the server in its own thread: it keeps going when the page is reloaded or closed,
and the page reconnects to it. It stops when you press Stop or when the stream ends.

Saved in data/raw/ while it runs:
    <id>.live.jsonl      [stream_seconds, author, text] per message (same format as <id>.jsonl)
    <id>.live.meta.json  title, channel, start time, first/last message, running or not
so finished-stream mode can open the recording instantly after the stream ends.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass

from . import activity, replay
from .cli import load_ignore_users
from .detector import Detector, Rules, Window, group_spikes
from .replay import RAW, ROOT, _last_error, _text, _ytdlp
from .report import hms, ranked, save_outputs, window_row

STALL_SECONDS = 60     # no new message for this long on a busy chat = something is wrong
BUSY_PER_MIN = 10      # "busy" = at least this many messages per minute over the last 5 minutes
FIRST_CHAT_WAIT = 180  # seconds to wait for the first chat after starting yt-dlp
CLOSE_LAG = 45         # silent chat: close a window this long after it ends (yt-dlp is ~10-15 s behind)
QUIET_CHECK = 300      # quiet chat: ask if the stream is still live at most this often
MAX_RETRIES = 5        # reconnects in a row without any new chat before giving up
GAP = 120              # a hole this long in the recording: the detector starts learning again
CHAT_KEEP = 400        # last messages kept for the page's live chat panel
META_EVERY = 30        # seconds between updates of <id>.live.meta.json


class LiveError(Exception):
    pass


@dataclass
class LiveInfo:
    video_id: str
    title: str
    channel: str
    started: float   # unix time of the stream's 0:00 (release_timestamp, or measured by the player: sync())
    viewers: int
    release: float = 0.0  # yt-dlp's release_timestamp
    synced: bool = False  # True once the player measured the real start


def _info_json(video_id: str) -> dict:
    r = subprocess.run(_ytdlp("--skip-download", "--dump-single-json",
                              f"https://www.youtube.com/watch?v={video_id}"),
                       capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        raise LiveError(f"Couldn't open this video: {_last_error(r.stderr)}")
    return json.loads(r.stdout)


def check(video_id: str) -> LiveInfo:
    """Is it live right now, with live chat? When did it start?"""
    d = _info_json(video_id)
    status = d.get("live_status")
    if status == "is_upcoming":
        raise LiveError("This stream hasn't started yet. Try again when it is live.")
    if status in ("was_live", "post_live"):
        raise LiveError("This stream has already ended. Open it in Finished stream mode.")
    if status != "is_live":
        raise LiveError("This is not a live stream. For a past stream, use Finished stream mode.")
    if "live_chat" not in (d.get("subtitles") or {}):
        raise LiveError("This live stream has no live chat (turned off by the streamer, or members-only).")
    started = d.get("release_timestamp") or d.get("timestamp")
    if not started:
        raise LiveError("YouTube didn't say when this stream started, so stream times can't be worked out.")
    # yt-dlp adds the date and time to the title of a live stream; fulltitle is the real title.
    return LiveInfo(video_id, d.get("fulltitle") or d.get("title") or video_id, d.get("channel") or "", float(started),
                    int(d.get("concurrent_view_count") or 0), release=float(started))


def live_status(video_id: str) -> str | None:
    """'is_live', 'post_live', 'was_live'… or None if YouTube couldn't be asked."""
    try:
        return _info_json(video_id).get("live_status")
    except (LiveError, subprocess.TimeoutExpired, ValueError):
        return None


def parse_live_line(line: str, started: float) -> list[tuple[float, str, str, str]]:
    """One yt-dlp live line -> [(stream_seconds, author, text, message_id), ...]."""
    try:
        d = json.loads(line)
    except ValueError:
        return []
    actions = (d.get("replayChatItemAction") or {}).get("actions") or d.get("actions") or []
    out = []
    for a in actions:
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r or not r.get("timestampUsec"):
            continue
        t = round(int(r["timestampUsec"]) / 1e6 - started, 3)
        author = (r.get("authorName") or {}).get("simpleText") or "?"
        out.append((t, author, _text((r.get("message") or {}).get("runs")), r.get("id") or ""))
    return out


class Reader:
    """One yt-dlp process reading the live chat into a temp folder. stop() leaves nothing behind."""

    def __init__(self, video_id: str):
        self.tmp = RAW / f"_live_{video_id}"
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.tmp.mkdir(parents=True)
        self.err_path = self.tmp / "errors.txt"
        with self.err_path.open("w") as err:  # a file, not a pipe: a full pipe would freeze yt-dlp
            self.proc = subprocess.Popen(
                _ytdlp("--skip-download", "--write-subs", "--sub-langs", "live_chat",
                       "-o", str(self.tmp / "%(id)s.%(ext)s"), f"https://www.youtube.com/watch?v={video_id}"),
                stdout=subprocess.DEVNULL, stderr=err, text=True,
                start_new_session=True)  # own process group, so stop() also ends anything yt-dlp started
        self.src = None
        self.buf = ""
        _readers.add(self)

    def lines(self) -> list[str]:
        """Complete new lines since the last call (never blocks)."""
        if self.src is None:
            files = [f for f in self.tmp.glob("*.live_chat.json*") if "Frag" not in f.name]
            if not files:
                return []
            self.src = files[0].open()  # stays valid when yt-dlp renames .part -> .json
        chunk = self.src.read()
        if not chunk:
            return []
        self.buf += chunk
        *done, self.buf = self.buf.split("\n")
        return done

    def alive(self) -> bool:
        return self.proc.poll() is None

    def error(self) -> str:
        try:
            text = self.err_path.read_text()
        except OSError:
            return ""
        return _last_error(text) if text.strip() else ""

    def stop(self) -> None:
        if self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                self.proc.wait()
        if self.src is not None:
            self.src.close()
        shutil.rmtree(self.tmp, ignore_errors=True)
        _readers.discard(self)


_readers: set[Reader] = set()


@atexit.register
def _cleanup() -> None:
    """App closed (Ctrl+C): end every yt-dlp and remove the temp folders."""
    for r in list(_readers):
        r.stop()


STEPS = [
    ("check", "Check stream", "Asks YouTube (through yt-dlp) if the stream is live right now and has live chat, and "
     "when it really started (release_timestamp). Every moment is shown in stream time, counted from that start."),
    ("connect", "Connect to chat", "Starts yt-dlp in live chat mode. It asks YouTube for new chat about every 10 s, "
     "like a chat window in a browser. Chat reaches the app about 10–15 s after it is written."),
    ("read", "Reading chat", f"Reads each new message as yt-dlp saves it. If a busy chat goes silent for "
     f"{STALL_SECONDS} s, or yt-dlp stops, the app checks whether the stream ended and otherwise reconnects."),
    ("detect", "Find spikes", "Cuts chat into 10-second windows and compares each with the normal level of the last "
     "5 minutes (the same rules as finished streams). New moments show up while the stream is live."),
    ("save", "Save", "Writes every message to data/raw/<id>.live.jsonl as it arrives. At the end, also "
     "windows.csv, spikes.csv and chart.html in output/<id>/."),
]


class Watch:
    """One live stream being watched: reader + detector + everything the page needs to (re)draw."""

    def __init__(self, video_id: str):
        self.vid = video_id
        self.info: LiveInfo | None = None
        self.rules = Rules(ignore_users=load_ignore_users())
        self.det = Detector(self.rules)
        self.rows: list[list] = []        # finished windows, compact, for the chart
        self.moments: list[dict] = []
        self.moments_version = 0
        self.chat: deque = deque(maxlen=CHAT_KEEP)  # (seq, t, author, text)
        self.chat_seq = 0
        self.total = 0
        self.recent: deque = deque()      # stream times of the last 5 minutes of messages
        self.first_t: float | None = None
        self.last_t: float | None = None
        self.last_msg_wall: float | None = None
        self.state = "starting"            # starting, connecting, reading, reconnecting, ended, stopped, error
        self.note = ""
        self.ended_at: float | None = None
        self.version = 0
        self.cond = threading.Condition()
        self.stop_evt = threading.Event()
        self._seen: set[str] = set()
        self._seen_order: deque = deque()
        self._resume_t = -1e18
        self._out = None
        self._meta_at = 0.0
        self._quiet = False                # True while rebuilding from the saved file (no "new moment" logs)
        self._new_start: float | None = None
        self.epoch = 0                     # +1 when all times change (sync): pages reload everything
        self.auto_clip = True              # make a reel of every new moment (clips.py)
        self.run = activity.start("live", f"Live watch · {video_id}", {"video": video_id}, STEPS)
        self.thread = threading.Thread(target=self._main, name=f"live-{video_id}", daemon=True)

    # ---- page side ----
    @property
    def running(self) -> bool:
        return self.ended_at is None

    def summary(self) -> dict:
        i = self.info
        return {"video_id": self.vid, "title": i.title if i else "", "channel": i.channel if i else "",
                "started": i.started if i else None, "state": self.state, "running": self.running,
                "messages": self.total}

    def stats(self) -> dict:
        now = time.time()
        last5 = len(self.recent)
        span = min(300.0, (self.last_t - self.recent[0]) if self.recent and self.last_t else 0) or 60.0
        per_min_now = sum(1 for t in self.recent if self.last_t is not None and t >= self.last_t - 60)
        top = max(self.moments, key=lambda m: m["score"], default=None)
        return {"state": self.state, "note": self.note, "running": self.running,
                "live_for": (now if self.running else self.ended_at) - self.info.started if self.info else 0,
                "messages": self.total, "per_min": per_min_now, "per_min_5": round(last5 / span * 60, 1),
                "normal": round(self.det.windows[-1].baseline, 1) if self.det.windows else None,
                "top": top, "first": self.first_t, "last": self.last_t,
                "last_msg_ago": round(now - self.last_msg_wall, 1) if self.last_msg_wall else None,
                "viewers": self.info.viewers if self.info else 0, "synced": bool(self.info and self.info.synced)}

    def wait(self, version: int, timeout: float) -> int:
        with self.cond:
            if self.version == version:
                self.cond.wait(timeout)
            return self.version

    def snapshot(self) -> dict:
        with self.cond:
            return {"info": asdict(self.info) if self.info else {"video_id": self.vid},
                    "rules": {"window": self.rules.window, "threshold": self.rules.threshold,
                              "min_score": self.rules.min_score, "lead": self.rules.lead, "tail": self.rules.tail},
                    "windows": list(self.rows), "moments": self.moments, "moments_version": self.moments_version,
                    "chat": [list(c[1:]) for c in self.chat], "chat_seq": self.chat_seq,
                    "stats": self.stats(), "epoch": self.epoch}

    def delta(self, n_rows: int, chat_seq: int, moments_version: int) -> dict:
        with self.cond:
            out = {"windows": self.rows[n_rows:], "chat": [list(c[1:]) for c in self.chat if c[0] > chat_seq],
                   "chat_seq": self.chat_seq, "stats": self.stats(), "info": asdict(self.info) if self.info else None}
            if moments_version != self.moments_version:
                out["moments"], out["moments_version"] = self.moments, self.moments_version
            return out

    def sync(self, start: float) -> dict:
        """The page's player measured the real 0:00 of the stream. Applied by the watch thread."""
        i = self.info
        if not i or not self.running:
            return {"ok": False, "why": "not watching"}
        if abs(start - i.release) > 3600:
            return {"ok": False, "why": "measured start is more than an hour off, ignored"}
        if abs(start - i.started) < 2:
            if not i.synced:
                i.synced = True
                self._changed()
            return {"ok": True, "changed": False}
        self._new_start = start
        return {"ok": True, "changed": True}

    def stop(self) -> None:
        if self.running:
            self.note = "stopping…"
        self.stop_evt.set()
        self._changed()

    # ---- server side ----
    def _changed(self) -> None:
        with self.cond:
            self.version += 1
            self.cond.notify_all()

    def _set_state(self, state: str, note: str = "") -> None:
        self.state, self.note = state, note
        self._changed()

    def _busy(self) -> bool:
        if not self.recent or self.last_t is None:
            return False
        span = max(60.0, min(300.0, self.last_t - self.recent[0]))
        return len(self.recent) / span * 60 >= BUSY_PER_MIN

    def _write_meta(self, running: bool) -> None:
        i = self.info
        if not i:
            return
        (RAW / f"{self.vid}.live.meta.json").write_text(json.dumps({
            "title": i.title, "channel": i.channel, "started": i.started, "release": i.release,
            "synced": i.synced, "first": self.first_t or 0,
            "duration": self.last_t or 0, "messages": self.total, "running": running,
            "ended": self.state == "ended", "recorded": "live"}))
        self._meta_at = time.time()

    def _resume(self) -> None:
        """Continue an earlier recording of the same stream (e.g. after the app was restarted)."""
        path = RAW / f"{self.vid}.live.jsonl"
        old = replay.live_meta(self.vid)
        if not path.exists():
            return
        if not old or abs(old.get("release", old.get("started", 0)) - self.info.release) > 1:
            path.unlink()  # a different broadcast on the same video id: start fresh
            return
        if old.get("synced"):  # keep the start the player measured last time
            self.info.started, self.info.synced = old["started"], True
        n = self._load_file()
        self._resume_t = self.last_t if self.last_t is not None else self._resume_t
        self.last_msg_wall = None
        if n:
            self.run.step("save").log(f"continued the earlier recording: {n:,} messages up to {hms(self._resume_t)}")

    def _load_file(self, shift: float = 0.0) -> int:
        """Rebuild everything from the saved recording (times moved by -shift)."""
        path = RAW / f"{self.vid}.live.jsonl"
        self.det = Detector(self.rules)
        self.rows, self.moments, self.total = [], [], 0
        self.chat.clear()
        self.recent.clear()
        self.first_t = self.last_t = None
        rows = []
        with path.open() as f:
            for line in f:
                try:
                    t, a, x = json.loads(line)
                except ValueError:
                    continue
                rows.append([round(t - shift, 3), a, x])
        if shift:
            tmp = path.with_suffix(".jsonl.tmp")
            tmp.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
            tmp.replace(path)
        self._quiet = True
        for t, a, x in rows:
            self._add(t, a, x, write=False)
        self._quiet = False
        self.recent.clear()  # rate/stall checks start fresh
        self.moments_version += 1
        return len(rows)

    def _apply_sync(self) -> None:
        """Move every saved time to the start the player measured (watch thread only)."""
        start, self._new_start = self._new_start, None
        shift = start - self.info.started
        with self.cond:
            self._out.close()
            n = self._load_file(shift)
            self._out = (RAW / f"{self.vid}.live.jsonl").open("a")
            self.info.started, self.info.synced = start, True
            if self._resume_t > -1e17:
                self._resume_t -= shift
            self.epoch += 1
            self._write_meta(True)
            self.version += 1
            self.cond.notify_all()
        self.run.step("check").log(f"matched stream time to YouTube's player: 0:00 moved by {shift:+.1f} s "
                                   f"({n:,} saved messages shifted)")
        self.run.step("check").set(start_measured_by_player=True, start_shift_s=round(shift, 1))

    def _add(self, t: float, author: str, text: str, write: bool = True) -> None:
        if self.last_t is not None and t - self.last_t > GAP:
            self._closed(self.det.restart())
        if write:
            self._out.write(json.dumps([t, author, text], ensure_ascii=False) + "\n")
        self._closed(self.det.add(t, author, text))
        self.chat_seq += 1
        self.chat.append((self.chat_seq, t, author, text))
        self.total += 1
        self.recent.append(t)
        if self.first_t is None:
            self.first_t = t
        self.last_t = t if self.last_t is None else max(self.last_t, t)

    def _closed(self, done: list[Window]) -> None:
        if not done:
            return
        self.rows += [window_row(w) for w in done]
        before = {m["peak"] for m in self.moments}
        moments = ranked(group_spikes(self.det.windows, self.rules))
        if moments != self.moments:  # pages redraw the list only when it really changed
            self.moments = moments
            self.moments_version += 1
        new = [m for m in self.moments if m["peak"] not in before]
        if self.ended_at is None and not self._quiet:
            for m in new:
                if m["peak"] >= self._resume_t:
                    self.run.step("detect").log(f"new moment at {hms(m['peak'])}: {m['score']:.0f} per 10 s, "
                                                f"{m['ratio']}× normal")
                    if self.auto_clip and self.info:
                        from . import clips
                        clips.make(self.vid, m, self.info.channel, self.info.title, auto=True)
        self.run.step("detect").detail(f"{len(self.det.windows):,} windows checked · {len(self.moments)} moments")

    def _take(self, msgs: list[tuple[float, str, str, str]]) -> int:
        fresh = []
        for t, a, x, mid in sorted(msgs, key=lambda m: m[0]):
            if t <= self._resume_t or (mid and mid in self._seen):
                continue  # already saved (yt-dlp repeats recent chat after a reconnect)
            if mid:
                self._seen.add(mid)
                self._seen_order.append(mid)
                if len(self._seen_order) > 20000:
                    self._seen.discard(self._seen_order.popleft())
            fresh.append((t, a, x))
        if not fresh:
            return 0
        with self.cond:
            for t, a, x in fresh:
                self._add(t, a, x)
            self._out.flush()
            while self.recent and self.recent[0] < self.last_t - 300:
                self.recent.popleft()
            self.last_msg_wall = time.time()
            self.version += 1
            self.cond.notify_all()
        return len(fresh)

    def _advance(self) -> None:
        """Quiet chat: close windows by the clock, so the chart keeps moving."""
        cur = self.det.current
        if cur is None or self.info is None:
            return
        now_stream = time.time() - self.info.started - CLOSE_LAG
        if now_stream >= cur.start + self.rules.window:
            with self.cond:
                self._closed(self.det.advance(now_stream))
                self.version += 1
                self.cond.notify_all()

    def _read_once(self, attempt: int) -> tuple[str, int]:
        """Run yt-dlp until something happens. Returns (why it stopped, messages read)."""
        conn, read = self.run.step("connect"), self.run.step("read")
        conn.begin("starting yt-dlp…" if attempt == 0 else f"reconnecting (try {attempt})…")
        if attempt:
            conn.log(f"reconnect #{attempt}")
        self._set_state("connecting" if attempt == 0 else "reconnecting", conn.text)
        reader = Reader(self.vid)
        got = 0
        t_start = last_detail = last_status = time.time()
        try:
            while True:
                if self.stop_evt.is_set():
                    return "stopped", got
                if self._new_start is not None:
                    self._apply_sync()
                msgs = [m for line in reader.lines() for m in parse_live_line(line, self.info.started)]
                n = self._take(msgs) if msgs else 0
                if n and not got:
                    conn.done("connected · yt-dlp is reading the live chat")
                    read.begin()
                    self._set_state("reading")
                got += n
                now = time.time()
                self._advance()
                silent = now - (self.last_msg_wall or t_start)
                if got and now - last_detail > 2:
                    last_detail = now
                    s = self.stats()
                    read.detail(f"{s['per_min']} msgs/min · {self.total:,} messages · last message "
                                f"{silent:.0f} s ago (stream time {hms(self.last_t or 0)})")
                    read.set(messages=self.total, per_minute=s["per_min"], last_message_stream_time=hms(self.last_t or 0))
                    self.note = ""
                    self._changed()
                if now - self._meta_at > META_EVERY:
                    self._write_meta(True)
                if not reader.alive():
                    why = reader.error() or "yt-dlp stopped reading (the stream may have ended)"
                    (read if got else conn).fail(why)
                    return f"yt-dlp stopped: {why}", got
                if not got and now - t_start > FIRST_CHAT_WAIT:
                    conn.fail(f"yt-dlp is running but no chat arrived in {FIRST_CHAT_WAIT} s. "
                              f"{reader.error() or 'YouTube may be blocking it.'}")
                    return "no chat", got
                if got and silent > STALL_SECONDS and self._busy():
                    read.fail(f"No new messages for {silent:.0f} s although chat was busy. "
                              f"{reader.error() or 'YouTube may have cut the connection.'} Reconnecting…")
                    return "stalled", got
                if silent > QUIET_CHECK and now - last_status > QUIET_CHECK:
                    last_status = now
                    read.log(f"quiet for {silent:.0f} s: asking if the stream is still live")
                    if live_status(self.vid) not in ("is_live", None):
                        return "ended", got
                self.stop_evt.wait(0.5)
        finally:
            reader.stop()

    def _main(self) -> None:
        run = self.run
        try:
            with run.step("check") as st:
                st.detail("asking YouTube…")
                self.info = check(self.vid)
                i = self.info
                run.title = f"Live watch · {i.channel or i.title}"
                run.target.update(title=i.title, channel=i.channel, started=i.started)
                st.set(title=i.title, channel=i.channel, viewers=i.viewers,
                       started=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(i.started)),
                       live_for=hms(time.time() - i.started))
                st.done(f"live for {hms(time.time() - i.started)} · {i.viewers:,} watching · chat on")
            self._changed()
            save = run.step("save").begin(f"writing data/raw/{self.vid}.live.jsonl")
            RAW.mkdir(parents=True, exist_ok=True)
            with self.cond:
                self._resume()
            self._out = (RAW / f"{self.vid}.live.jsonl").open("a")
            replay.LIVE_RUNNING.add(self.vid)
            self._write_meta(True)
            run.step("detect").begin("waiting for chat…")
            fails = 0
            while True:
                why, got = self._read_once(fails)
                if why in ("stopped", "ended"):
                    break
                fails = 0 if got else fails + 1
                self._set_state("reconnecting", "checking if the stream is still live…")
                status = live_status(self.vid)
                if status not in ("is_live", None):
                    why = "ended"
                    run.step("read").log(f"YouTube says the stream is {status}")
                    break
                if fails >= MAX_RETRIES:
                    raise LiveError(f"Gave up after {MAX_RETRIES} reconnects without new chat. Last problem: {why}")
                wait = min(60, 5 * 2 ** fails)
                self._set_state("reconnecting", f"{why} · reconnecting in {wait} s")
                if self.stop_evt.wait(wait):
                    why = "stopped"
                    break
            self.state = "ended" if why == "ended" else "stopped"
            self._finish(save, "stream ended" if why == "ended" else "stopped")
        except LiveError as e:
            for st in run.steps.values():
                if st.status == "running":
                    st.fail(str(e))
            self.state, self.note = "error", str(e)
            self._finish(None, str(e), error=True)
        except Exception as e:  # never leave a watch half-dead without saying why
            self.state, self.note = "error", f"{type(e).__name__}: {e}"
            self._finish(None, self.note, error=True)
            raise

    def _finish(self, save, why: str, error: bool = False) -> None:
        with self.cond:
            if self.det.current is not None:
                self._closed(self.det.flush())
            if self._out:
                self._out.close()
        replay.LIVE_RUNNING.discard(self.vid)
        self.ended_at = time.time()
        if self.info and self.total:
            self._write_meta(False)
            spikes = group_spikes(self.det.windows, self.rules)
            save_outputs(ROOT / "output" / self.vid, self.vid, self.info.title, self.det.windows, spikes, self.rules)
            if save:
                save.set(messages=self.total, file=f"data/raw/{self.vid}.live.jsonl")
                save.done(f"{self.total:,} messages in data/raw/{self.vid}.live.jsonl · output/{self.vid}/")
        result = f"{why} · {self.total:,} messages · {len(self.moments)} moments"
        self.run.finish("error" if error else "done", result)
        if not error:
            self.note = why
        self._changed()


_watches: dict[str, Watch] = {}
_lock = threading.Lock()


def start(video_id: str) -> Watch:
    """Start watching (or return the watch that is already running for this stream)."""
    with _lock:
        w = _watches.get(video_id)
        if w and w.running:
            return w
        w = _watches[video_id] = Watch(video_id)
    w.thread.start()
    return w


def get(video_id: str) -> Watch | None:
    return _watches.get(video_id)


def watches() -> list[dict]:
    return [w.summary() for w in sorted(_watches.values(), key=lambda w: w.run.created, reverse=True)]
