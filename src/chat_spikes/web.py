"""Local web page: find the biggest chat spikes of a YouTube stream.

Two modes (start page at /):
  - Finished stream (/finished.html): paste a stream that has ended, the chat replay is downloaded.
  - Live stream (/live.html): watch a stream while it is live (live.py), spikes appear in real time.

    uv run chat-spikes-web          # then open http://localhost:8765
"""

from __future__ import annotations

import json
import signal
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import activity, library, live
from .cli import load_ignore_users, video_id_from_or_none
from .detector import Detector, Rules, Window, group_spikes, keep_message
from .replay import ReplayError, StreamInfo, chat_path, load
from .report import hms, ranked, save_outputs, window_row

WEB = Path(__file__).parent / "webui"
PORT = 8765


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet
        pass

    def send_response(self, code, message=None):
        self._status = code
        super().send_response(code, message)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/activity":
            return self.activity_stream()
        started, self._status = time.time(), 0
        try:
            self.route(u)
        finally:  # every request shows up on the Activity panel
            activity.request("GET", self.path, self._status, time.time() - started, started)

    def activity_stream(self) -> None:
        """Live feed for the Activity panel: a snapshot, then every change as it happens."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        try:
            snap = activity.snapshot()
            self.send_event("snapshot", snap)
            seq = snap["seq"]
            while True:
                seq, events = activity.wait_changes(seq)
                if events is None:
                    snap = activity.snapshot()
                    self.send_event("snapshot", snap)
                    seq = snap["seq"]
                    continue
                if not events:
                    self.wfile.write(b": keep-alive\n\n")
                    self.wfile.flush()
                for e in events:
                    self.send_event(e["type"], e["data"])
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def route(self, u) -> None:
        qs = parse_qs(u.query)
        if u.path == "/api/analyze":
            return self.analyze(qs)
        if u.path == "/api/chat":
            return self.chat_between(qs)
        if u.path == "/api/why":
            return self.why(qs)
        if u.path.startswith("/api/live/"):
            return self.live_api(u.path.rsplit("/", 1)[-1], qs)
        if u.path.startswith("/api/clip/"):
            return self.clip_api(u.path.rsplit("/", 1)[-1], qs)
        if u.path.startswith("/api/library/"):
            return self.library_api(u.path.rsplit("/", 1)[-1], qs)
        if u.path.startswith("/clips/"):
            return self.send_clip(u.path)
        name = "app.html" if u.path in ("/", "") else u.path.lstrip("/")  # the tabbed window
        f = (WEB / name).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            self.send_error(404)
            return
        ctype = {"html": "text/html", "css": "text/css", "js": "application/javascript"}.get(f.suffix[1:], "text/plain")
        self.send_bytes(f.read_bytes(), ctype)

    def send_bytes(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_event(self, kind: str, data) -> None:
        self.wfile.write(f"data: {json.dumps({'type': kind, 'data': data}, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def chat_between(self, qs: dict) -> None:
        """Chat messages of a saved stream between two times (for the selected moment)."""
        vid = (qs.get("v") or [""])[0]
        t0, t1 = float((qs.get("from") or [0])[0]), float((qs.get("to") or [0])[0])
        rows = []
        replay = library.chat_file(vid, "replay.jsonl")
        for path in (replay, replay.with_suffix(".jsonl.part"), library.chat_file(vid, "live.jsonl")):
            if video_id_from_or_none(vid) == vid and path.exists():
                ignore = load_ignore_users()
                rules = Rules(ignore_users=ignore)
                with path.open() as f:
                    for line in f:
                        t, a, x = json.loads(line)
                        if t > t1:
                            break
                        if t >= t0:
                            rows.append({"t": t, "author": a, "text": x, "counted": keep_message(a, x, rules)})
                break
        self.send_bytes(json.dumps(rows[:2000], ensure_ascii=False).encode(), "application/json")

    def why(self, qs: dict) -> None:
        """Why did chat explode? Free chat evidence, plus Gemini's answer when ai=1 (or saved earlier).

        mode=chat (live mode): Gemini gets chat only, because captions don't exist yet.
        """
        from .evidence import evidence
        from .gemini import AI_CHAT_STEPS, AI_STEPS, GeminiError, api_key, explain, explain_chat, saved_answer
        from .replay import cached

        g = lambda k, d="0": (qs.get(k) or [d])[0]
        vid = g("v", "")
        chat_only = g("mode", "full") == "chat"
        if video_id_from_or_none(vid) != vid or not chat_path(vid).exists():
            return self.send_bytes(b'{"error": "Analyze the stream first."}', "application/json")
        moment = {"peak": float(g("peak")), "clip_start": float(g("clip_start")), "clip_end": float(g("clip_end")),
                  "score": float(g("score")), "ratio": float(g("ratio"))}
        info = cached(vid, include_running=True)
        name = f"{info.channel or info.title} · " if info else ""
        target = {"video": vid, "title": info.title if info else "", "peak": moment["peak"],
                  "clip": [moment["clip_start"], moment["clip_end"]]}
        saved = saved_answer(vid, moment["peak"])  # a full answer first, else a chat-only one
        # Finished mode may ask again for the full check (with captions) after a chat-only answer from live mode.
        ask_ai = g("ai") == "1" and (not saved or (saved.get("chat_only") and not chat_only))
        if ask_ai:
            run = activity.start("ai", f"AI check{' (chat only)' if chat_only else ''} · {name}moment "
                                 f"{hms(moment['peak'])}", target, AI_CHAT_STEPS if chat_only else AI_STEPS)
        else:
            run = activity.start("evidence", f"Chat evidence · {name}moment {hms(moment['peak'])}", target, [
                ("evidence", "Chat evidence", "Counts the chat of this moment on your Mac: reaction mix, words that "
                 "stand out, most repeated message. Free, no AI."),
                ("saved", "Saved AI answer?", "Looks in the stream's ai/ folder for an AI answer from before. "
                 "Nothing is sent to Gemini here.")])
        with run.step("evidence") as st:
            ev = evidence(vid, moment["clip_start"], moment["clip_end"])
            kws = ", ".join(k["text"] for k in ev["keywords"]) or "nothing stands out"
            st.set(messages=ev.get("messages"), keywords=[k["text"] for k in ev["keywords"]],
                   reaction={t["label"]: t["share"] for t in ev["types"]})
            st.done(f"{hms(moment['clip_start'])}–{hms(moment['clip_end'])} · chat kept saying: {kws}")
        out = {"evidence": {k: v for k, v in ev.items() if k != "sample"}, "has_key": bool(api_key()), "ai": saved}
        if not ask_ai:
            with run.step("saved") as st:
                st.done(f"yes, from {saved['model']}" if saved else "none yet")
            run.finish("done", "evidence ready")
        else:
            try:
                meta = {"title": info.title if info else "", "channel": info.channel if info else ""}
                out["ai"] = (explain_chat if chat_only else explain)(vid, meta, moment, ev, run)
                a = out["ai"]
                run.finish("done", f"{a['viral_score']}/10 · chat only" if chat_only else
                           f"cut {hms(a['best_start'])} → {hms(a['best_end'])} · {a['viral_score']}/10")
            except GeminiError as e:
                out["ai_error"] = str(e).split("|", 1)[-1]
                for st in run.steps.values():
                    if st.status == "running":
                        st.fail(out["ai_error"])
                run.finish("error", out["ai_error"])
        self.send_bytes(json.dumps(out, ensure_ascii=False).encode(), "application/json")

    def analyze(self, qs: dict) -> None:
        url = (qs.get("url") or [""])[0].strip()
        vid = video_id_from_or_none(url)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not vid:
            self.send_event("error", "That doesn't look like a YouTube link.")
            return

        rules = Rules(ignore_users=load_ignore_users())
        det = Detector(rules)
        stop = threading.Event()
        state: dict = {"info": None, "reached": 0.0, "count": 0}
        run = activity.start("analyze", f"Analyze stream · {vid}", {"video": vid}, [
            ("check", "Check the video", "Asks YouTube (through yt-dlp) for the title and length, and checks it is a "
             "finished stream with chat replay. Instant when the chat was saved before."),
            ("chat", "Get the chat replay", "Downloads every chat message with yt-dlp, a few seconds of stream at a "
             "time, or reads the saved copy in data/raw/."),
            ("detect", "Find spikes", f"Cuts chat into {rules.window}-second windows, compares each with the normal "
             f"level (×{rules.threshold} or far above normal swings, at least {rules.min_score}), merges spikes into "
             "moments. Runs while the chat downloads."),
            ("save", "Save results", "Writes windows.csv, spikes.csv and chart.html to output/<video>/."),
        ])
        self.run = run
        self.send_event("rules", {"window": rules.window, "threshold": rules.threshold,
                                  "min_score": rules.min_score, "lead": rules.lead, "tail": rules.tail})

        def on_info(info: StreamInfo, from_cache: bool) -> None:
            state["info"] = info
            run.title = f"Analyze stream · {info.channel or info.title}"
            run.target.update(title=info.title, channel=info.channel, duration=info.duration)
            run.step("check").set(title=info.title, channel=info.channel, length=hms(info.duration), saved_before=from_cache)
            run.step("check").done(f"{info.title} · {hms(info.duration)}" + (" · saved before" if from_cache else ""))
            run.step("chat").begin("reading the saved copy…" if from_cache else "downloading with yt-dlp…")
            run.step("detect").begin("waiting for chat…")
            self.send_event("info", {"video_id": info.video_id, "title": info.title, "channel": info.channel,
                                     "duration": info.duration, "cached": from_cache,
                                     "recorded_live": info.live, "first": info.first})

        def on_progress(reached: float, count: int) -> None:
            state["reached"], state["count"] = reached, count
            dur = state["info"].duration if state["info"] else 0
            run.step("chat").detail(f"{hms(reached)} of {hms(dur)} · {count:,} messages",
                                    reached / dur if dur else None)

        self.send_event("status", "Checking the video…")
        run.step("check").begin("asking YouTube…")
        pending: list[Window] = []
        last_sent = time.time()
        total = 0
        try:
            for t, author, text in load(vid, stop, on_info, on_progress):
                total += 1
                pending += det.add(t, author, text)
                if time.time() - last_sent > 0.7:  # send results in batches while downloading
                    self.flush_progress(det, pending, state, total, rules)
                    pending, last_sent = [], time.time()
            pending += det.flush()
            self.flush_progress(det, pending, state, total, rules)
            run.step("chat").set(messages=total)
            run.step("chat").done(f"{total:,} messages")
            spikes = group_spikes(det.windows, rules)
            top = max(spikes, key=lambda s: s.peak.score, default=None)
            run.step("detect").set(windows=len(det.windows), moments=len(spikes))
            run.step("detect").done(f"{len(spikes)} moments from {len(det.windows):,} windows"
                                    + (f" · biggest at {hms(top.peak.start)}" if top else ""))
            self.send_event("done", {"messages": total, "moments": ranked(spikes), "all_moments": len(spikes)})
            with run.step("save") as st:
                self.save(vid, state["info"], det.windows, spikes, rules)
                st.done(f"output/{vid}/")
            run.finish("done", f"{len(spikes)} moments · {total:,} messages")
        except ReplayError as e:
            for st in run.steps.values():
                if st.status == "running":
                    st.fail(str(e))
            run.finish("cancelled" if str(e) == "Cancelled." else "error", str(e))
            self.send_event("error", str(e))
        except (BrokenPipeError, ConnectionResetError):
            run.finish("cancelled", "page closed or Cancel pressed")
        finally:
            stop.set()
            run.finish("error", "stopped unexpectedly")  # no-op when already finished

    def flush_progress(self, det: Detector, pending: list[Window], state: dict, total: int, rules: Rules) -> None:
        if pending:
            self.send_event("windows", [window_row(w) for w in pending])
        info = state["info"]
        reached = max(state["reached"], det.windows[-1].start if det.windows else 0)
        self.send_event("progress", {"reached": reached, "duration": info.duration if info else 0,
                                     "messages": total})
        moments = group_spikes(det.windows, rules)
        self.send_event("moments", ranked(moments))
        self.run.step("detect").detail(f"{len(det.windows):,} windows checked · {len(moments)} moments so far")

    def save(self, vid: str, info: StreamInfo | None, windows, spikes, rules) -> None:
        save_outputs(library.results_dir(vid), vid, info.title if info else vid, windows, spikes, rules)

    # ---- The stream library (library.py): one folder per stream, grouped by channel ----
    def library_api(self, action: str, qs: dict) -> None:
        vid = (qs.get("v") or [""])[0].strip()
        if action == "list":
            return self.send_json({"root": library.rel(library.LIBRARY), "streams": library.streams()})
        if action == "reveal":
            ok = video_id_from_or_none(vid) == vid and library.reveal(vid)
            return self.send_json({"ok": bool(ok)})
        self.send_error(404)

    # ---- Live mode ----
    def live_api(self, action: str, qs: dict) -> None:
        g = lambda k: (qs.get(k) or [""])[0].strip()
        if action == "feed":
            return self.live_feed(g("v"))
        if action == "list":
            return self.send_json(live.watches())
        if action == "start":
            vid = video_id_from_or_none(g("url"))
            if not vid:
                return self.send_json({"error": "That doesn't look like a YouTube link."})
            return self.send_json(live.start(vid).summary())
        if action == "sync":
            w = live.get(g("v"))
            try:
                start = float(g("start"))
            except ValueError:
                return self.send_json({"ok": False, "why": "bad start"})
            return self.send_json(w.sync(start) if w else {"ok": False, "why": "not watching"})
        if action == "stop":
            w = live.get(g("v"))
            if w:
                w.stop()
            return self.send_json({"ok": bool(w)})
        self.send_error(404)

    def clip_api(self, action: str, qs: dict) -> None:
        """Reels of moments (clips.py): make one, list them, change the face-cam box, auto clips on/off."""
        from . import clips
        from .replay import cached

        g = lambda k, d="": (qs.get(k) or [d])[0].strip()
        vid = g("v")
        if video_id_from_or_none(vid) != vid:
            return self.send_json({"error": "bad video"})
        w = live.get(vid)
        info = cached(vid, include_running=True)
        channel = (w.info.channel if w and w.info else "") or (info.channel if info else "")
        title = (w.info.title if w and w.info else "") or (info.title if info else "")
        try:
            if action == "make":
                m = {k: float(g(k, "0")) for k in ("peak", "clip_start", "clip_end")}
                if m["clip_end"] - m["clip_start"] < 3 or m["clip_end"] - m["clip_start"] > 180:
                    return self.send_json({"error": "A clip must be 3 s to 3 min long."})
                clips.make(vid, m, channel, title, hook=qs["hook"][0] if "hook" in qs else None, new_hook=g("new_hook") == "1")
            elif action == "cancel":
                clips.cancel(vid, float(g("peak", "0")))
            elif action == "layout":
                box = lambda k: None if g(k, "none") == "none" else [float(x) for x in g(k).split(",")]
                old = clips.layout_for(channel, vid)
                face = box("face") if "face" in qs else old["face"]
                # No face cam (outdoor stream...): no game box either, unless one is given.
                clips.save_layout(channel, vid, face, box("game") if "game" in qs else old["game"] if face else None)
            elif action == "auto":
                if w:
                    w.auto_clip = g("on") == "1"
            elif action != "list":
                return self.send_error(404)
        except (ValueError, clips.ClipError) as e:
            return self.send_json({"error": str(e) or "bad value"})
        self.send_json({"clips": clips.jobs(vid), "channel": channel,
                        "folder": library.rel(library.clips_dir(vid)), "layout": clips.layout_for(channel, vid),
                        "auto": bool(w and w.auto_clip) if w else None})

    def send_clip(self, path: str) -> None:
        """/clips/<video>/<peak>.mp4|.jpg = the stream's clips/ folder, with Range support so the video can be scrubbed."""
        vid, _, name = path[len("/clips/"):].partition("/")
        if video_id_from_or_none(vid) != vid:
            return self.send_error(404)
        base = library.clips_dir(vid).resolve()
        f = (base / name).resolve()
        if base not in f.parents or f.suffix not in (".mp4", ".jpg") or not f.is_file():
            return self.send_error(404)
        size = f.stat().st_size
        ctype = "video/mp4" if f.suffix == ".mp4" else "image/jpeg"
        a, b = 0, size - 1
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            lo, _, hi = rng[6:].split(",")[0].partition("-")
            a = int(lo) if lo else max(0, size - int(hi))
            b = int(hi) if lo and hi else size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {a}-{b}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(b - a + 1))
        self.send_header("Cache-Control", "no-cache")
        if "download=1" in self.path:
            self.send_header("Content-Disposition", f'attachment; filename="{f.parent.name}-{f.name}"')
        self.end_headers()
        try:
            with f.open("rb") as fh:
                fh.seek(a)
                left = b - a + 1
                while left > 0 and (chunk := fh.read(min(1 << 16, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def send_json(self, data) -> None:
        self.send_bytes(json.dumps(data, ensure_ascii=False).encode(), "application/json")

    def live_feed(self, vid: str) -> None:
        """Live page: everything so far, then what changed, about once a second, until the watch ends.

        The watch runs on its own; closing or reloading the page doesn't stop it.
        """
        w = live.get(vid)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        if not w:
            self.send_event("gone", "This stream isn't being watched. Press Watch live to start.")
            return
        try:
            snap = w.snapshot()
            self.send_event("snapshot", snap)
            n_rows, chat_seq, mv, version = len(snap["windows"]), snap["chat_seq"], snap["moments_version"], -1
            while True:
                running = w.running
                version = w.wait(version, 1.0)
                if w.epoch != snap["epoch"]:  # all times changed (synced to the player): send everything again
                    snap = w.snapshot()
                    self.send_event("snapshot", snap)
                    n_rows, chat_seq, mv = len(snap["windows"]), snap["chat_seq"], snap["moments_version"]
                    continue
                d = w.delta(n_rows, chat_seq, mv)
                n_rows += len(d["windows"])
                chat_seq = d["chat_seq"]
                mv = d.get("moments_version", mv)
                self.send_event("update", d)
                if not running:
                    self.send_event("ended", d["stats"])
                    return
                time.sleep(0.5)  # at most ~2 updates a second
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def main() -> None:
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT
    activity.load_history()
    moved = library.migrate()
    if moved:
        print(f"Moved {moved} saved stream(s) into {library.rel(library.LIBRARY)}/ (one folder per stream, by channel).")
    # Closed by a signal (not Ctrl+C): still run the clean-up that ends yt-dlp of live watches (live.py, atexit).
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda *_: sys.exit(0))
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    url = f"http://localhost:{port}"
    print(f"Chat Spikes is running at {url}  (Ctrl+C to quit)")
    if "--no-browser" not in sys.argv:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        print("\nBye.")


if __name__ == "__main__":
    main()
