"""Activity: everything the app does, as runs made of steps (nodes), shown live on the page.

A run is one job, e.g. "Analyze stream" or "AI check of moment 0:31:40". It has a fixed list of
steps, so the page can show what is done, what is running now and what comes next.
Every HTTP request is logged too. Finished runs are kept in data/processed/activity.jsonl,
so the history survives a restart.

    run = activity.start("ai", "AI check · 0:31:40", target={...}, steps=[("captions", "Captions", "what it does")])
    with run.step("captions") as s:
        s.detail("downloaded, 12,057 words")
        s.log("used en-orig")
        s.set(words=12057)
    run.finish()
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from collections import deque

from .replay import ROOT

HISTORY = ROOT / "data" / "processed" / "activity.jsonl"
KEEP_RUNS = 150
KEEP_REQUESTS = 300
MAX_LOGS = 200

_cond = threading.Condition()
_seq = 0
_changes: deque = deque(maxlen=3000)  # (seq, event) for the live page
_runs: dict[str, "Run"] = {}
_old_runs: deque = deque(maxlen=KEEP_RUNS)  # finished runs from earlier app starts (dicts)
_requests: deque = deque(maxlen=KEEP_REQUESTS)
_ids = itertools.count(1)


def _emit(kind: str, data) -> None:
    global _seq
    with _cond:
        _seq += 1
        _changes.append((_seq, {"type": kind, "data": data}))
        _cond.notify_all()


class Step:
    def __init__(self, run: "Run", key: str, label: str, about: str):
        self.run, self.key, self.label, self.about = run, key, label, about
        self.status = "waiting"  # waiting, running, done, error, skipped, cancelled
        self.started = self.ended = None
        self.text = ""
        self.progress: float | None = None
        self.data: dict = {}
        self.logs: list = []

    # --- updates (each one is pushed to the page) ---
    def begin(self, text: str = "") -> "Step":
        self.status, self.started, self.text = "running", time.time(), text or self.text
        if self.run.status == "queued":
            self.run.status = "running"
        self.run.changed(force=True)
        return self

    def detail(self, text: str, progress: float | None = None) -> None:
        self.text = text
        if progress is not None:
            self.progress = max(0.0, min(1.0, progress))
        self.run.changed()

    def log(self, msg: str) -> None:
        self.logs.append([time.time(), msg])
        del self.logs[:-MAX_LOGS]
        self.run.changed(force=True)

    def set(self, **kv) -> None:
        self.data.update(kv)
        self.run.changed()

    def done(self, text: str | None = None) -> None:
        if text is not None:
            self.text = text
        self.status, self.ended = "done", time.time()
        if self.progress is not None:
            self.progress = 1.0
        self.run.changed(force=True)

    def fail(self, msg: str) -> None:
        self.status, self.ended, self.text = "error", time.time(), msg
        self.log(f"error: {msg}")

    def skip(self, text: str = "") -> None:
        self.status, self.text = "skipped", text or self.text
        self.run.changed(force=True)

    def __enter__(self) -> "Step":
        if self.status != "running":
            self.begin()
        return self

    def __exit__(self, et, e, tb) -> bool:
        if et is None:
            if self.status == "running":
                self.done()
        elif self.status == "running":
            self.fail(str(e) or et.__name__)
        return False

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "about": self.about, "status": self.status,
                "started": self.started, "ended": self.ended, "text": self.text, "progress": self.progress,
                "data": self.data, "logs": self.logs}


class Run:
    def __init__(self, kind: str, title: str, target: dict, steps: list[tuple[str, str, str]]):
        self.id = f"{int(time.time())}-{next(_ids)}"
        self.kind, self.title, self.target = kind, title, target
        self.status = "queued"  # queued, running, done, error, cancelled
        self.created, self.ended = time.time(), None
        self.result = ""
        self.steps = {k: Step(self, k, label, about) for k, label, about in steps}
        self._last_push = 0.0
        self._pending = False

    def step(self, key: str) -> Step:
        return self.steps[key]

    def changed(self, force: bool = False) -> None:
        # Progress can change many times a second: push at most ~4 times a second (always on status changes).
        now = time.time()
        if force or now - self._last_push > 0.25 or self.status in ("done", "error", "cancelled"):
            self._last_push = now
            _emit("run", self.as_dict())

    def finish(self, status: str = "done", result: str = "") -> None:
        if self.ended:
            return
        self.status, self.ended, self.result = status, time.time(), result or self.result
        for s in self.steps.values():
            if s.status == "running":
                s.status, s.ended = ("cancelled" if status == "cancelled" else "error" if status == "error" else "done"), self.ended
            elif s.status == "waiting":
                s.status = "skipped"
        self.changed(force=True)
        try:
            HISTORY.parent.mkdir(parents=True, exist_ok=True)
            with HISTORY.open("a") as f:
                f.write(json.dumps(self.as_dict(), ensure_ascii=False) + "\n")
        except OSError:
            pass

    def as_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "title": self.title, "target": self.target,
                "status": self.status, "created": self.created, "ended": self.ended, "result": self.result,
                "steps": [s.as_dict() for s in self.steps.values()]}


def start(kind: str, title: str, target: dict | None = None, steps: list[tuple[str, str, str]] = ()) -> Run:
    run = Run(kind, title, target or {}, list(steps))
    with _cond:
        _runs[run.id] = run
        if len(_runs) > KEEP_RUNS:  # forget the oldest finished ones
            for rid in [r.id for r in sorted(_runs.values(), key=lambda r: r.created) if r.ended][:len(_runs) - KEEP_RUNS]:
                del _runs[rid]
    run.changed(force=True)
    return run


class _Null:
    """Stand-in when no run is tracked (e.g. command line use)."""
    def __getattr__(self, _):
        return lambda *a, **k: self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


NULL = _Null()


def request(method: str, path: str, status: int, seconds: float, started: float) -> None:
    r = {"t": started, "method": method, "path": path, "status": status, "ms": round(seconds * 1000)}
    _requests.append(r)
    _emit("request", r)


def load_history() -> None:
    if not HISTORY.exists():
        return
    lines = HISTORY.read_text().splitlines()[-KEEP_RUNS:]
    for line in lines:
        try:
            _old_runs.append(json.loads(line))
        except ValueError:
            pass
    if len(HISTORY.read_text().splitlines()) > KEEP_RUNS * 3:  # keep the file small
        HISTORY.write_text("\n".join(lines) + "\n")


def snapshot() -> dict:
    with _cond:
        live = {r.id: r.as_dict() for r in _runs.values()}
        runs = [r for r in _old_runs if r["id"] not in live] + list(live.values())
        return {"seq": _seq, "runs": sorted(runs, key=lambda r: r["created"])[-KEEP_RUNS:],
                "requests": list(_requests)}


def wait_changes(after: int, timeout: float = 15.0) -> tuple[int, list | None]:
    """Events after `after`. None means the page fell too far behind and should reload the snapshot."""
    with _cond:
        if _seq == after:
            _cond.wait(timeout)
        if _changes and _changes[0][0] > after + 1:
            return _seq, None
        return _seq, [e for s, e in _changes if s > after]
