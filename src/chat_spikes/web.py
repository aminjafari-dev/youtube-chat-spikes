"""Local web page: paste a YouTube URL, press Start, watch the chat spikes live.

    uv run chat-spikes-web          # then open http://localhost:8765
"""

from __future__ import annotations

import json
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .cli import load_ignore_users, video_id_from_or_none
from .detector import Detector, Rules, Window, group_spikes, keep_message
from .report import write_chart, write_spikes, write_windows
from .sources import ROOT, SOURCES

WEB = Path(__file__).parent / "webui"
PORT = 8765
GRACE = 4.0  # seconds to wait for late chat before closing a window


def window_json(w: Window, rules: Rules) -> dict:
    return {"start": w.start, "score": round(w.score, 1), "raw": w.raw, "counted": w.counted,
            "people": w.users, "baseline": round(w.baseline, 1),
            "limit": round(max(rules.threshold * max(w.baseline, 1.0), rules.min_score), 1),
            "ratio": round(w.ratio, 2), "spike": w.is_spike, "samples": w.samples}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet
        pass

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/api/run":
            return self.run_stream(parse_qs(u.query))
        name = "index.html" if u.path in ("/", "") else u.path.lstrip("/")
        f = (WEB / name).resolve()
        if WEB.resolve() not in f.parents or not f.is_file():
            self.send_error(404)
            return
        ctype = {"html": "text/html", "css": "text/css", "js": "application/javascript"}.get(f.suffix[1:], "text/plain")
        body = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_event(self, kind: str, data) -> None:
        self.wfile.write(f"data: {json.dumps({'type': kind, 'data': data}, ensure_ascii=False)}\n\n".encode())
        self.wfile.flush()

    def run_stream(self, qs: dict) -> None:
        url = (qs.get("url") or [""])[0].strip()
        mode = (qs.get("source") or ["youtube"])[0]
        vid = video_id_from_or_none(url) if url else None

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        if mode not in SOURCES:
            self.send_event("error", "Unknown source.")
            return
        if mode == "youtube" and not vid:
            self.send_event("error", "That doesn't look like a YouTube link.")
            return

        rules = Rules(ignore_users=load_ignore_users())
        det = Detector(rules)
        q: queue.Queue = queue.Queue()
        stop = threading.Event()
        start = time.time()
        threading.Thread(target=SOURCES[mode], args=(vid or "", q, stop, start), daemon=True).start()
        self.send_event("rules", {"window": rules.window, "threshold": rules.threshold,
                                  "min_score": rules.min_score, "lead": rules.lead, "tail": rules.tail})
        title = "demo"
        spikes_sent = 0
        base: float | None = None  # when the stream started (epoch seconds); all times are stream time
        try:
            last_ping = time.time()
            while True:
                try:
                    kind, data = q.get(timeout=0.5)
                except queue.Empty:
                    kind, data = None, None
                closed: list[Window] = []
                if kind == "message":
                    t, author, text = data
                    closed += det.add(t, author, text)
                    self.send_event("message", {"t": t, "author": author, "text": text,
                                                "counted": keep_message(author, text, rules)})
                elif kind == "info":
                    title = data["title"]
                    base = data.get("stream_start") or start
                    data["stream_time_now"] = time.time() - base
                    self.send_event("info", data)
                elif kind in ("viewers", "error"):
                    self.send_event(kind, data)
                elif kind == "end":
                    self.send_event("end", None)
                    break
                if base is not None:
                    closed += det.advance(time.time() - base - GRACE)
                for w in closed:
                    self.send_event("window", window_json(w, rules))
                spikes = group_spikes(det.windows, rules)
                if len(spikes) != spikes_sent or (closed and spikes and closed[-1].is_spike):
                    spikes_sent = len(spikes)
                    self.send_event("spikes", [{
                        "peak": s.peak.start, "clip_start": s.clip_start, "clip_end": s.clip_end,
                        "ratio": round(s.peak.ratio, 1), "people": s.peak.users,
                        "samples": s.peak.samples} for s in spikes])
                if time.time() - last_ping > 15:  # keep the connection open
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page pressed Stop or was closed
        finally:
            stop.set()
            if det.windows:
                out = ROOT / "output" / (f"{vid}-live" if mode == "youtube" else "demo")
                out.mkdir(parents=True, exist_ok=True)
                spikes = group_spikes(det.windows, rules)
                write_windows(out / "windows.csv", det.windows)
                write_spikes(out / "spikes.csv", spikes, vid or "")
                write_chart(out / "chart.html", det.windows, spikes, rules, title, vid or "")


def main() -> None:
    port = int(sys.argv[sys.argv.index("--port") + 1]) if "--port" in sys.argv else PORT
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
