"""Write results: windows.csv, spikes.csv and a chart (chart.html, no extra libraries)."""

from __future__ import annotations

import csv
import html
from pathlib import Path

from .detector import Rules, Spike, Window


def hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def youtube_link(video_id: str, seconds: float) -> str:
    return f"https://youtu.be/{video_id}?t={int(seconds)}"


def write_windows(path: Path, windows: list[Window]) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start", "start_s", "messages_raw", "messages_counted", "people",
                    "reactions", "score", "normal_level", "times_normal", "spike"])
        for x in windows:
            w.writerow([hms(x.start), int(x.start), x.raw, x.counted, x.users, x.reactions,
                        round(x.score, 1), round(x.baseline, 1), round(x.ratio, 2), int(x.is_spike)])


def write_spikes(path: Path, spikes: list[Spike], video_id: str) -> None:
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["rank", "peak", "clip_start", "clip_end", "clip_start_s", "clip_end_s",
                    "peak_score", "times_normal", "people", "link", "sample_messages"])
        for rank, s in enumerate(sorted(spikes, key=lambda s: -s.peak.ratio), 1):
            w.writerow([rank, hms(s.peak.start), hms(s.clip_start), hms(s.clip_end),
                        int(s.clip_start), int(s.clip_end), round(s.peak.score, 1),
                        round(s.peak.ratio, 2), s.peak.users,
                        youtube_link(video_id, s.clip_start), " | ".join(s.peak.samples)])


def write_chart(path: Path, windows: list[Window], spikes: list[Spike], rules: Rules,
                title: str, video_id: str) -> None:
    if not windows:
        path.write_text("<p>No chat messages.</p>")
        return
    W, H, pad = 1200, 320, 40
    t0, t1 = windows[0].start, windows[-1].start + rules.window
    span = max(t1 - t0, 1)
    top = max(max(w.score for w in windows), 1)
    x = lambda t: pad + (t - t0) / span * (W - 2 * pad)
    y = lambda v: H - pad - v / top * (H - 2 * pad)

    bars = []
    bw = max((W - 2 * pad) / len(windows), 1)
    for w in windows:
        cls = "spike" if w.is_spike else "bar"
        bars.append(f'<rect class="{cls}" x="{x(w.start):.1f}" y="{y(w.score):.1f}" '
                    f'width="{bw:.2f}" height="{H - pad - y(w.score):.1f}">'
                    f'<title>{hms(w.start)} - score {w.score:.0f}, normal {w.baseline:.0f}</title></rect>')
    base = " ".join(f"{x(w.start):.1f},{y(w.baseline * rules.threshold):.1f}" for w in windows)
    ticks = "".join(
        f'<text x="{x(t):.1f}" y="{H - pad + 16}" text-anchor="middle">{hms(t)}</text>'
        for t in [t0 + span * i / 6 for i in range(7)])

    rows = "".join(
        f"<tr><td>{i}</td><td><a href='{youtube_link(video_id, s.clip_start)}' target='_blank'>"
        f"{hms(s.clip_start)} → {hms(s.clip_end)}</a></td><td>{s.peak.ratio:.1f}×</td>"
        f"<td>{s.peak.users}</td><td>{html.escape(' | '.join(s.peak.samples))}</td></tr>"
        for i, s in enumerate(sorted(spikes, key=lambda s: -s.peak.ratio), 1))

    path.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat Spikes</title><style>
:root{{--bg:#fff;--fg:#1a1a1a;--muted:#666;--bar:#9db4d6;--spike:#e4572e;--line:#333}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151515;--fg:#eee;--muted:#999;--bar:#4a6c99;--spike:#ff7a50;--line:#ccc}}}}
body{{background:var(--bg);color:var(--fg);font:14px system-ui;margin:24px 16px;max-width:1240px}}
svg{{width:100%;height:auto}} .bar{{fill:var(--bar)}} .spike{{fill:var(--spike)}}
text{{fill:var(--muted);font-size:11px}} polyline{{fill:none;stroke:var(--line);stroke-dasharray:4 3;stroke-width:1}}
table{{border-collapse:collapse;width:100%}} td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #8884;vertical-align:top}}
a{{color:var(--spike)}} p{{color:var(--muted)}}
</style></head><body>
<h1>{html.escape(title)}</h1>
<p>Chat score per {rules.window} s. Orange = spike (≥ {rules.threshold}× normal and ≥ {rules.min_score:g}).
Dashed line = spike threshold. Hover a bar for details.</p>
<svg viewBox="0 0 {W} {H}">{''.join(bars)}<polyline points="{base}"/>{ticks}</svg>
<h2>{len(spikes)} moments (best first)</h2>
<table><tr><th>#</th><th>Clip (opens YouTube)</th><th>× normal</th><th>People</th><th>What chat said</th></tr>{rows}</table>
</body></html>""")
