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

## Why did chat explode? (free evidence + optional AI check)

Click a moment and the **"Why did chat explode?"** card shows **free chat evidence**
(no AI, counted on your Mac, `evidence.py`):

- reaction mix (😂 Funny / 😱 Shock / 🔥 Win / 💔 Fail / 🎉 Hype, from word lists)
- words that stand out: said by at least 3 different people and 3× more than in the rest of the stream
- the message repeated most word for word

### "Check with AI and find the exact clip" (button, optional)

Nothing goes to AI unless you press the button on a moment. Then Gemini gets **text only, no video**:

1. **What the streamer says** from 1 minute before to 1 minute after the spike: YouTube's captions
   (`transcript.py`), as numbered lines with their stream time. Downloaded once per stream in about 10 s,
   saved in `data/raw/<id>.words.json` with a time for every word.
2. **Every chat message of the spike** with its time (bots and `!commands` left out), plus chat messages per 10 s
   over the 2 minutes.

Gemini answers: what happened, why chat reacted, why it could go viral, a **viral score 1–10**, type, title,
hook line, and **where to cut**: the line and the exact words the clip starts with and ends with.

**How the cut times are made exact:** Gemini doesn't give times (it could make them up). It picks the start
and end *lines* and copies the words. The app finds those words in YouTube's captions and takes their time,
to about half a second, plus a small margin (0.3 s before, 0.7 s after). The card shows
"Start when they say …" / "End after …" with a ▶ button that plays the cut, and the transcript that was sent
(the chosen lines highlighted).

Limits: if the moment is mostly visual (a stunt, something on screen), the transcript can't show it.
Gemini then says so and gives low confidence. Streams without captions can't be cut this way.

Answers are saved in `data/processed/why/`, so the same moment never uses quota twice.
About 9,000 tokens per check on a very busy stream.

### Setup

Free key: https://aistudio.google.com → **Get API key**. Add it to `.env` in the project folder:

```
GEMINI_API_KEY=your-key
# optional: force one model instead of the automatic choice
# GEMINI_MODEL=gemini-3.8-flash
```

### Which model (free tier)

From Google's pricing and models pages (Nov 2026), all on the free tier. Tried in this order:

1. `gemini-4-hydrogen` (newest generation)
2. `gemini-3.8-flash`
3. `gemini-3.6-flash`
4. `gemini-3.5-flash-lite`

The free models are often busy ("503 high demand"). The app tries each model at most twice and waits at most
60 s for an answer, then moves to the next. If a model's daily free quota is used up, it skips that model
for the rest of the run. Each step is printed in the terminal (`[gemini …]`).
Free-tier requests may be used by Google to improve its products (public stream content here).

Tested 2026-11-19 on IShowSpeed: `gemini-4-hydrogen` answered after about 4 min of busy retries;
another moment fell back to `gemini-3.5-flash-lite`, which answered in 3 s.

## Activity panel (see everything the app does)

Press **Activity** at the top of the page. It shows every job the app runs, live, as a chain of steps
(like n8n): green ✓ = done, blue spinner = running now (with a progress bar where it can), dashed
"up next" = what runs after it, red ✕ = failed (with the reason), faded = skipped.
The number on the button = jobs running right now.

- **Jobs** (left): every job, newest first, with its step dots, how long it took and the result.
  Jobs: *Analyze stream* (check video → get chat → find spikes → save),
  *Chat evidence* (each moment you click), *AI check* (evidence → captions → build question →
  wait for turn → ask Gemini → exact cut → save).
- Click a job to see its flow, click a step to see what it does, its timing, its numbers and its log
  (for Gemini: every model tried, every error, every retry).
- **Requests**: every request the page made to the app, with status and time.
- **Follow what's running** jumps to the job that is running now.
- Finished jobs are kept in `data/processed/activity.jsonl`, so the history is still there after a restart.
  Reloading the page reconnects to jobs that are still running.

Code: `activity.py` (runs, steps, live feed at `/api/activity`), `webui/activity.js`.

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
| `src/chat_spikes/evidence.py` | Free chat evidence per moment: standout words, reaction type, top message |
| `src/chat_spikes/transcript.py` | Captions of the stream with a time per word (yt-dlp), cut into numbered lines |
| `src/chat_spikes/gemini.py` | Text-only Gemini check: what happened + exact cut from the captions, model fallback, saved answers |
| `src/chat_spikes/report.py` | CSV files and the standalone chart |
| `src/chat_spikes/activity.py` | Records every job, its steps and every request; live feed for the Activity panel |
| `src/chat_spikes/web.py` | Local web server for the page (`chat-spikes-web`) |
| `src/chat_spikes/webui/` | The page: `index.html`, `style.css`, `app.js`, `activity.js` |
| `src/chat_spikes/cli.py` | Command line (`chat-spikes`) |

## Maintenance

If YouTube changes something and downloads start failing, update yt-dlp:

```bash
uv lock --upgrade-package yt-dlp && uv sync
```

## Existing tools on GitHub (checked 2026-11-17)

CHAD, chat-analyzer, stream-analyser, YBIGTA/24th-de-highlighter. All depend on the broken
`chat-downloader` library or are inactive, so this project has its own small detector.
