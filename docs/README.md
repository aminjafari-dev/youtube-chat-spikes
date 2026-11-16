# YouTube Chat Spikes (finished streams)

- **Started:** 2026-11-17
- **Branch:** `finished-streams`. The live-stream version (official API) stays on `main`.
- **Status:** Working. Paste a finished stream, get its biggest chat spikes as moments to clip.
- **Part of:** [twitch-chat-clipper](../../twitch-chat-clipper/docs/README.md) (Option A: rules only, no AI, $0)

## What it does

1. You paste the link of a YouTube stream that has **ended**.
2. **yt-dlp** downloads the stream's chat replay: every message with its exact position in the recording.
3. The chat is cut into 10-second windows and compared with the stream's normal level.
4. You get the moments where chat exploded, ranked biggest first, with clip times
   (20 s before the spike to 15 s after), a link to that point on YouTube, and what chat said.

No API key, no daily limit.

## Run it

```bash
cd /Volumes/Development/claude/projects/youtube-chat-spikes
uv run chat-spikes-web
```

Opens http://localhost:8765. Paste a link, press **Analyze**.

- **Progress bar** while the chat downloads. The chart and moments fill in as it goes.
- **Chart of the whole stream**: orange = spike, numbered dots = top 5. Click anywhere to jump the video there.
- **Moments to clip**: sort by *Most messages* (biggest) or *Most × normal* (most surprising).
  Click a moment: the video jumps there and the chat of that clip is shown (peak window highlighted).
  Click a time in the chat to jump to that message.
- **Cancel** stops the download; nothing half-done is kept.

Command line (same results, no page):

```bash
uv run chat-spikes "https://www.youtube.com/watch?v=VIDEO_ID" --top 10
```

Options: `--window 10 --threshold 2.5 --min-score 8 --lead 20 --tail 15 --refresh`

Results are saved in `output/<video-id>/` (`chart.html`, `spikes.csv`, `windows.csv`).
The chat is saved in `data/raw/<video-id>.jsonl`, so opening the same stream again is instant.

## How long the download takes

YouTube hands out chat replay a few seconds at a time, so yt-dlp reads it piece by piece.
Measured on 2026-11-17:

| Stream | Chat | Time |
|--------|------|------|
| Linus Tech Tips, 2.5 h (quiet chat) | 597 messages | 22 s |
| IShowSpeed, 2 h (one of YouTube's busiest chats) | 105,000 messages | ~11 min |
| Typical Gamer, 12 h | ~1,300 messages per 2.5 min of stream | roughly 1–2 h (estimate) |

The busier the chat, the longer it takes; stream length matters less.
Very quiet chats (a few messages a minute) may show no moments: nothing ever reaches the minimum of 8 per 10 s.

Second time: instant (saved copy).

## Rules (Option A, no AI)

- Ignore bots (`config/ignore_users.txt`) and `!commands` (giveaways, bot commands).
- One person counts at most 2 messages per window, so spam can't fake a spike.
- Reaction messages (LOL, no way, 😂, 💀, W, clip it …) add a small bonus.
- Normal level = median of the previous 5 minutes.
- A window is a spike when it has a score of at least 8 **and** either:
  - it's **2.5× the normal level** (works for small and medium streams), or
  - it's **far above the normal ups and downs** (normal + 4 × typical swing).
    Needed for huge streams: YouTube caps chat replay at roughly 15 messages per second,
    so on a mega-stream chat can never double. Found on IShowSpeed: always 120–175 per 10 s.
- Spikes within 30 s of each other become one moment.

## When it can't work

- The stream is still live → wait until it ends (or use the `main` branch).
- Chat replay turned off by the streamer, private or members-only stream → no chat to read.
- A normal video (not a past live stream) has no chat.

The page shows a clear message for each case.

## Code

| File | Job |
|------|-----|
| `src/chat_spikes/replay.py` | Downloads chat replay with yt-dlp, reads it while it downloads, saves a compact copy |
| `src/chat_spikes/detector.py` | Windows, rules, spike detection |
| `src/chat_spikes/report.py` | CSV files and the standalone chart |
| `src/chat_spikes/web.py` | Local web server for the page (`chat-spikes-web`) |
| `src/chat_spikes/webui/` | The page: `index.html`, `style.css`, `app.js` |
| `src/chat_spikes/cli.py` | Command line (`chat-spikes`) |

## Maintenance

If YouTube changes something and downloads start failing, update yt-dlp:

```bash
uv lock --upgrade-package yt-dlp && uv sync
```

## Existing tools on GitHub (checked 2026-11-17)

CHAD, chat-analyzer, stream-analyser, YBIGTA/24th-de-highlighter. All depend on the broken
`chat-downloader` library or are inactive, so this project has its own small detector.
