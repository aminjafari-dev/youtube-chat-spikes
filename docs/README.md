# YouTube Chat Spikes

- **Started:** 2026-11-17
- **Status:** Web page working in demo mode. YouTube live mode built on the official API, needs your API key to test.
- **Part of:** [twitch-chat-clipper](../../twitch-chat-clipper/docs/README.md) (Option A: rules only, no AI, $0)

## What it does

Give it a YouTube stream (live or finished). It counts chat messages every 10 seconds, compares each window with the stream's normal level, and lists the moments where chat exploded, with clip start/end times (20 s before the spike, 15 s after).

## Rules (Option A, no AI)

- Ignore bots (`config/ignore_users.txt`) and `!commands` (giveaways, bot commands).
- One person counts at most 2 messages per window, so spam can't fake a spike.
- Reaction messages (LOL, no way, 😂, 💀, W, clip it ...) add a small bonus.
- Normal level = median of the last 5 minutes. Spike = at least 2.5x normal and a score of at least 8.
- Spikes within 30 s of each other become one moment.

## The web page (easiest)

```bash
cd /Volumes/Development/claude/projects/youtube-chat-spikes
uv run chat-spikes-web
```

It opens http://localhost:8765. Paste a YouTube live link, press **Start**:
- the stream plays on the page (muted at first)
- tiles: chat per 10 s, normal level, viewers, moments found
- live bar chart of the last 10 minutes (orange = spike, dashed line = spike level, hover for details)
- live chat feed (bot and `!command` messages are crossed out: not counted)
- "Moments to clip" list with clip start/end times and what chat said

**Demo (fake chat)** mode works without any key, so you can try it right away. Add a YouTube link in demo mode to watch a real stream next to the fake chat.

**YouTube live** mode uses the official YouTube Data API. It needs a free key:
copy `.env.example` to `.env` and paste the key. Limits: live streams only, and the free daily quota is about 10,000 units
(each chat check costs a few units), roughly a few hours of watching per day.

When you press Stop, results are saved to `output/demo/` or `output/<video-id>-live/`.

## Command line (finished streams)

```bash
cd /Volumes/Development/claude/projects/youtube-chat-spikes
uv run chat-spikes "https://www.youtube.com/watch?v=VIDEO_ID"
```

Options: `--window 10 --threshold 2.5 --min-score 8 --lead 20 --tail 15 --refresh`

Results go to `output/<video-id>/`:
- `chart.html`: chat activity chart; spikes in orange; table with links that open YouTube at the clip start
- `spikes.csv`: moments, best first, with clip times and sample messages
- `windows.csv`: every 10-second window

Downloaded chat is saved in `data/raw/` so re-running with different settings is instant.

## Code

| File | Job |
|------|-----|
| `src/chat_spikes/detector.py` | Windows, rules, spike detection (works the same live or after) |
| `src/chat_spikes/report.py` | CSV files and chart |
| `src/chat_spikes/youtube.py` | Reads YouTube chat (**unfinished, see below**) |
| `src/chat_spikes/cli.py` | The `chat-spikes` command |
| `src/chat_spikes/web.py` | Local web server for the page (`chat-spikes-web`) |
| `src/chat_spikes/sources.py` | Live chat sources: official YouTube API, demo |
| `src/chat_spikes/webui/` | The page: `index.html`, `style.css`, `app.js` |

## Open issue: where the YouTube chat comes from

1. `chat-downloader` (the library most GitHub tools use) is broken: unmaintained since 2023, fails on today's YouTube.
2. `youtube.py` reads YouTube's internal chat endpoint (what the youtube.com chat box uses). It got data back in testing, but it is unofficial and could break at any time.
3. Choices:
   - **Official YouTube Data API**: free API key, live streams only, daily quota.
   - **yt-dlp**: well maintained; downloads chat of finished streams.
   - **Keep the internal endpoint**: works for both, may break.

## Existing tools on GitHub (checked 2026-11-17)

- CHAD (david-ca6/chad): chat activity dashboard for YouTube/Twitch replays (uses chat-downloader)
- chat-analyzer (PyPI): YouTube/Twitch highlight analysis (uses chat-downloader)
- stream-analyser (emso-c): highlight detection from chat, inactive since 2022
- YBIGTA/24th-de-highlighter: real-time YouTube highlight detection, student project

None was a clean fit: they depend on the broken library or are inactive, so this project has its own small detector.
