"""Local web page: paste a finished YouTube stream, press Analyze, see its biggest chat spikes.

    uv run chat-spikes-web          # then open http://localhost:8765
"""

from __future__ import annotations

import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import activity
from .cli import load_ignore_users, video_id_from_or_none
from .detector import Detector, Rules, Spike, Window, group_spikes, keep_message
from .replay import RAW, ROOT, ReplayError, StreamInfo, load
from .report import hms, write_chart, write_spikes, write_windows

WEB = Path(__file__).parent / "webui"
PORT = 8765
TOP_MOMENTS = 200


def window_row(w: Window) -> list:
    # Compact: [start, score, messages, people, normal_level, spike]
    return [int(w.start), round(w.score, 1), w.raw, w.users, round(w.baseline, 1), int(w.is_spike)]


def spike_json(s: Spike) -> dict:
    return {"peak": s.peak.start, "clip_start": s.clip_start, "clip_end": s.clip_end,
            "score": round(s.peak.score, 1), "ratio": round(s.peak.ratio, 1),
            "people": s.peak.users, "samples": s.peak.samples}


def ranked(spikes: list[Spike]) -> list[dict]:
    return [spike_json(s) for s in sorted(spikes, key=lambda s: -s.peak.score)[:TOP_MOMENTS]]


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
        name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
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
        for path in (RAW / f"{vid}.jsonl", RAW / f"{vid}.jsonl.part"):
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
        """Why did chat explode? Free chat evidence, plus Gemini's answer when ai=1 (or saved earlier)."""
        from .evidence import evidence
        from .gemini import AI_STEPS, GeminiError, api_key, explain, saved_answer
        from .replay import cached

        g = lambda k, d="0": (qs.get(k) or [d])[0]
        vid = g("v", "")
        if video_id_from_or_none(vid) != vid or not (RAW / f"{vid}.jsonl").exists():
            return self.send_bytes(b'{"error": "Analyze the stream first."}', "application/json")
        moment = {"peak": float(g("peak")), "clip_start": float(g("clip_start")), "clip_end": float(g("clip_end")),
                  "score": float(g("score")), "ratio": float(g("ratio"))}
        info = cached(vid)
        name = f"{info.channel or info.title} · " if info else ""
        target = {"video": vid, "title": info.title if info else "", "peak": moment["peak"],
                  "clip": [moment["clip_start"], moment["clip_end"]]}
        saved = saved_answer(vid, moment["peak"])
        ask_ai = g("ai") == "1" and not saved
        if ask_ai:
            run = activity.start("ai", f"AI check · {name}moment {hms(moment['peak'])}", target, AI_STEPS)
        else:
            run = activity.start("evidence", f"Chat evidence · {name}moment {hms(moment['peak'])}", target, [
                ("evidence", "Chat evidence", "Counts the chat of this moment on your Mac: reaction mix, words that "
                 "stand out, most repeated message. Free, no AI."),
                ("saved", "Saved AI answer?", "Looks in data/processed/why/ for an AI answer from before. "
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
                out["ai"] = explain(vid, {"title": info.title if info else "", "channel": info.channel if info else ""},
                                    moment, ev, run)
                a = out["ai"]
                run.finish("done", f"cut {hms(a['best_start'])} → {hms(a['best_end'])} · {a['viral_score']}/10")
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
                                     "duration": info.duration, "cached": from_cache})

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
        out = ROOT / "output" / vid
        out.mkdir(parents=True, exist_ok=True)
        write_windows(out / "windows.csv", windows)
        write_spikes(out / "spikes.csv", spikes, vid)
        write_chart(out / "chart.html", windows, spikes, rules, info.title if info else vid, vid)


def main() -> None:
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT
    activity.load_history()
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
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    main()
