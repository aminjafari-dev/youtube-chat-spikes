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

from .cli import load_ignore_users, video_id_from_or_none
from .detector import Detector, Rules, Spike, Window, group_spikes, keep_message
from .replay import RAW, ROOT, ReplayError, StreamInfo, load
from .report import write_chart, write_spikes, write_windows

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

    def do_GET(self):
        u = urlparse(self.path)
        qs = parse_qs(u.query)
        if u.path == "/api/analyze":
            return self.analyze(qs)
        if u.path == "/api/chat":
            return self.chat_between(qs)
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
        self.send_event("rules", {"window": rules.window, "threshold": rules.threshold,
                                  "min_score": rules.min_score, "lead": rules.lead, "tail": rules.tail})

        def on_info(info: StreamInfo, from_cache: bool) -> None:
            state["info"] = info
            self.send_event("info", {"video_id": info.video_id, "title": info.title, "channel": info.channel,
                                     "duration": info.duration, "cached": from_cache})

        def on_progress(reached: float, count: int) -> None:
            state["reached"], state["count"] = reached, count

        self.send_event("status", "Checking the video…")
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
            spikes = group_spikes(det.windows, rules)
            self.send_event("done", {"messages": total, "moments": ranked(spikes), "all_moments": len(spikes)})
            self.save(vid, state["info"], det.windows, spikes, rules)
        except ReplayError as e:
            self.send_event("error", str(e))
        except (BrokenPipeError, ConnectionResetError):
            pass  # the page was closed or Cancel was pressed
        finally:
            stop.set()

    def flush_progress(self, det: Detector, pending: list[Window], state: dict, total: int, rules: Rules) -> None:
        if pending:
            self.send_event("windows", [window_row(w) for w in pending])
        info = state["info"]
        reached = max(state["reached"], det.windows[-1].start if det.windows else 0)
        self.send_event("progress", {"reached": reached, "duration": info.duration if info else 0,
                                     "messages": total})
        self.send_event("moments", ranked(group_spikes(det.windows, rules)))

    def save(self, vid: str, info: StreamInfo | None, windows, spikes, rules) -> None:
        out = ROOT / "output" / vid
        out.mkdir(parents=True, exist_ok=True)
        write_windows(out / "windows.csv", windows)
        write_spikes(out / "spikes.csv", spikes, vid)
        write_chart(out / "chart.html", windows, spikes, rules, info.title if info else vid, vid)


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
