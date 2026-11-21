# YouTube Chat Spikes (finished and live streams)

- **Started:** 2026-11-17
- **Branches:** `finished-streams` (finished streams only), `live-mode` (adds live mode, 2026-11-19).
  The old live version on `main` uses the official YouTube Data API and is not used any more.
- **Status:** Working. One window with browser-style tabs, one stream per tab (2026-11-19). Two modes:
  - **Finished stream (video):** paste a stream that has ended, get its biggest chat spikes as moments to clip.
  - **Live stream:** watch a stream while it is live; spikes show up in real time.
- **Part of:** [twitch-chat-clipper](../../twitch-chat-clipper/docs/README.md) (Option A: rules only, no AI, $0)

## What it does (finished stream)

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

Opens http://localhost:8765: a window with tabs, like a browser. A new tab shows the start page: choose
**Live stream** or **Finished stream (video)**, or open one of the **Saved streams**.
On the finished page, paste a link and press **Analyze** (or open `finished.html?v=VIDEO_ID`).

### Tabs: several streams at once

- **+** opens a new tab. Watch a different stream in each one (5, 10…); each live watch runs on the app by itself.
- Click a tab to show it, **×** (or middle-click) to close it, drag to reorder. Tabs come back after a reload
  (remembered in this browser).
- Tab icons: red dot = live watch running, grey dot = live watch stopped, ▶ = finished stream.
  An orange number on a tab = new spikes since you last looked at it (the window title shows the total).
- **Only the tab on screen keeps its live connection.** A browser allows just 6 connections to one app, so
  background tabs disconnect (and pause their player); the watches keep recording on the app. Switching back
  reconnects and redraws everything. So "Sound/Notification on a new moment" only fires for the tab on screen;
  the tab badges cover the others.
- Closing the tab of a running live watch asks: **Close tab, keep watching** (it keeps recording; reopen it from
  "Being watched now" in a new tab) or **Stop watching and close**.
- Opening a stream that is already open in another tab switches to that tab.
- `index.html`, `live.html` and `finished.html` still work on their own, without the tabs.

### Where everything is saved: one folder per stream, grouped by channel

```
streams/
  <Channel>/
    <YYYY-MM-DD> <Title> [<video id>]/
      stream.json      title, channel, link, start, live and/or finished
      chat/            replay.jsonl (finished), live.jsonl (live mode), *.meta.json, words.json (captions)
      results/         chart.html, spikes.csv, windows.csv
      ai/              saved AI answers and hooks
      clips/           reels (.mp4) + frame (.jpg) + info (.json)
```

- The **Saved streams** list on the start page shows this library by channel, with a filter and **Show folder**
  (opens the stream's folder in Finder).
- A stream is filed as soon as the app knows its title and channel. The folder never moves after that, even if
  the streamer changes the title. The date is the stream's start (live) or release date (finished).
- Not per stream: `data/clip_layouts.json` (face-cam boxes, per channel), `data/processed/activity.jsonl`
  (Activity history), and `data/raw/` (temp folders of running yt-dlp jobs only, deleted when they end).
- **Moving from the old layout:** on start, the app moves anything still in `data/raw/`, `data/clips/`,
  `data/processed/why/` and `output/<id>/` into `streams/` (code: `library.py`, `migrate()`). Folders in
  `output/` that aren't a video id (tests, demos) stay where they are.

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

Results are saved in the stream's folder, `results/` (`chart.html`, `spikes.csv`, `windows.csv`).
The chat is saved in `chat/replay.jsonl` there, so opening the same stream again is instant.

## Live mode

Press **Live stream** on the start page, paste the link of a stream that is live right now, press **Watch live**.

### How it works

- **yt-dlp only.** No API key, no browser extension, and the app's own code never calls YouTube's chat
  endpoints. yt-dlp runs in its live chat mode
  (`--skip-download --write-subs --sub-langs live_chat`) and asks YouTube for new chat about every 10 s,
  like a chat window in a browser. The app reads the file yt-dlp writes, as it grows (`live.py`).
- **Stream time, not your clock.** Every message has YouTube's own time (`timestampUsec`).
  Stream time = that time − the stream's 0:00.
  - First the 0:00 comes from yt-dlp (`release_timestamp`).
  - That can be a bit early: on IShowSpeed (2026-11-19) it was **149 s** before the video's real 0:00.
    So the live page's player (muted autoplay) tells the app the real 0:00
    (YouTube's player reports the real clock time of the frame it shows). The app then shifts the times
    saved so far, once, and uses the new 0:00. After that, moment times match the video, the player,
    YouTube `?t=` links and finished-stream mode. Activity → Live watch → Check stream shows the shift.
  - If no live page is open, the app keeps `release_timestamp` (the page says so in the status line).
- **Same detector** as finished streams (`detector.py`, 10-second windows, same rules), fed message by message.
  A window closes when newer chat arrives (or 45 s after it ends, if chat is silent).
  After a hole in the recording (app was closed, reconnect) the detector learns the normal level again,
  so the hole doesn't look like a spike.
- **Runs on the app, not in the page.** Closing or reloading the page doesn't stop it; the page reconnects
  (`live.html?v=VIDEO_ID`, or the start page's "Being watched now" list).
- **Saved while it runs:** the stream's folder, `chat/live.jsonl` (same format as the replay copy) and
  `chat/live.meta.json`. Press **Watch live** again later and it continues the same file.
  When it stops: `results/` gets `windows.csv`, `spikes.csv`, `chart.html`.
- **Finished-stream mode reads the live recording instantly** when there is no full replay copy
  (status says "chat recorded in live mode from …"). It only has the chat from when you started watching.
- **Stops** when you press **Stop**, or by itself when the stream ends. Then the page links to the finished-stream page.
  Stop and closing the app (Ctrl+C or a kill signal) end yt-dlp and delete its temp folder (`data/raw/_live_<id>/`).

### The live page

- Live player. Click a moment, a chat time or the chart: the player **rewinds** there (YouTube's DVR,
  about the last 12 hours). **Back to live** returns to the live edge.
- Tiles: live for, messages, messages per minute, normal level, biggest spike.
- Live chart (last 10 min / 30 min / 2 h / all). The grey band on the right = "chat on its way":
  chat reaches the app about 10–15 s late, plus the 10-second window.
- **Moments to clip** update in real time. A new one is highlighted (**NEW**, glowing border), the tab title
  shows "(1 new)", and you can turn on a 🔔 sound and a notification (when the tab is hidden).
- Chat panel: **Live chat** (newest at the bottom) or **Selected moment**.
- **Why did chat explode?** Free chat evidence works the same as for finished streams.

### AI check in live mode: chat only

YouTube makes captions only after the stream is processed, so in live mode **"Check with AI (chat only)"**
sends Gemini only the chat of the spike and chat per 10 s (no transcript, no exact cut). Saved as
the stream's `ai/<time>.chat.json`. After the stream ends, open it in Finished stream mode:
the same moment shows the chat-only answer plus a **Full check with captions and exact cut** button.

### Activity: "Live watch" job

Check stream (live? chat on? real start) → Connect to chat (yt-dlp) → Reading chat (messages/min, last
message time) → Find spikes (new moments are logged) → Save.

- No new message for 60 s on a busy chat (≥ 10/min), or yt-dlp stops: the step turns red with the reason
  (yt-dlp's last error, e.g. a block from YouTube), the app asks yt-dlp if the stream is still live, and
  reconnects (waits 10, 20, 40, 60 s). After 5 tries in a row without new chat it gives up.
- Quiet chat: every 5 minutes of silence the app asks if the stream is still live.

### Limits (tested 2026-11-19)

- **yt-dlp reads "Top chat", not "Live chat" (all messages).** YouTube filters Top chat (removes spam,
  repeats). On IShowSpeed: yt-dlp ~250–320/min, the browser's Top chat ~300/min, Live chat ~500/min.
  Spikes still show (they are measured against the normal level), but counts are lower than
  in a finished stream's replay. yt-dlp has no option for "Live chat". The other way would be a browser
  that the app controls (Playwright), reading the "Live chat" view. Not built: needs a big new dependency.
- Moments show up about 20–30 s after they happen (YouTube → yt-dlp ~10–15 s, plus the 10-second window).
- Rewinding only works inside YouTube's DVR window (~12 h) and if the streamer allows DVR.
- yt-dlp's first chunk of chat can take 1–2 minutes on very busy streams (it waits up to 3 minutes).
- Chat only from when you start watching. Members-only or subscribers-only chat: you see what YouTube shows
  a logged-out viewer.
- The temp file yt-dlp writes grows by ~1.5 KB per message (deleted on stop); on a 10-hour mega stream
  that can be ~150 MB in `data/raw/_live_<id>/` while it runs.

### When YouTube changes something

Live mode depends only on yt-dlp. If live chat stops arriving (Activity → Live watch → Reading chat or
Connect to chat turns red), update yt-dlp first:

```bash
uv lock --upgrade-package yt-dlp && uv sync
```

If the stream time looks wrong, open the live page once while the stream is live (the player sets the
exact 0:00). Live lines are read in `live.py` → `parse_live_line()`: if YouTube changes the message format,
that is the one place to fix.

## Reels: a vertical clip of a moment (9:16, with a hook)

Each moment can become a reel for Shorts / Reels / TikTok:

```
┌──────────────┐
│  face cam    │  top 35%: the streamer's face cam, zoomed to the full width
├─── HOOK ─────┤  a caption with emoji on the line between them, first 5 s only (Gemini)
│              │
│  middle of   │  bottom 65%: the middle of the stream screen (the game)
│  the screen  │
│              │
└──────────────┘
```

- **Finished stream page**: pick a moment → **🎬 Make a reel of this moment**.
- **Live page**: "🎬 Make a reel of every new moment" is on by default. When a new spike is found, its reel is
  made on its own. You can also make one by hand from any moment.
- The moment list shows 🎬 when its reel is ready (🎬… while it is being made). The reel card plays it and has
  **Download**, **Make again** and the face-cam box.
- Saved in the stream's `clips/<peak>.mp4` (plus a `.jpg` frame and a `.json` with the times and the layout).
- Each reel is a **Reel** job with 7 connected nodes: In line → Check the stream → Wait for the video (live only) →
  Download the piece → Write the hook → Make the reel → Save. Download and Make the reel show a progress bar. The same nodes show
  inside the reel card, with **Open this job in Activity** and **Cancel**. One reel is made at a time.
- **The hook**: Gemini reads what is said in the clip (finished streams: YouTube's captions) and the chat, and
  writes a caption of at most 10 words plus 1-2 emoji (e.g. "Lacy shows off how BIG the GTA VI Map is 😳🔥")
  that tells a stranger what the clip is and why to keep watching (based on what really happens). It is drawn
  as bold near-black San Francisco text with Apple colour emoji on a white rounded box with a soft shadow (made
  with Pillow, since this ffmpeg has no text filter), shows for the first 5 s and fades out (`HOOK_SECONDS` in
  `clips.py`). Saved in the stream's `ai/<peak>.hook.json`, so remakes reuse it. On the reel card you can
  type your own hook (**Use this hook**) or ask for **✨ New hook**. If Gemini fails, the reel is made without one.
- **Best quality**: yt-dlp picks the highest resolution, then frame rate, then bitrate (`-S res,fps,br`), with no
  size limit. The reel is never scaled down: 1080×1920 from 1080p, 1440×2560 from 1440p, 2160×3840 from 4K (the
  game part is always at least as tall as the source). It keeps the source frame rate (60 fps), is encoded with
  x264 `-preset slow -crf 10` (visually lossless), and the sound is copied as is when it is AAC.
- **Why live reels are 1080p even when YouTube's player shows 1440p**: while a stream is live, YouTube gives
  1440p only to its own web player (a streaming protocol and tokens yt-dlp can't use). Every client yt-dlp can use
  gets at most 1080p60. After the stream ends, the recording usually has 1440p: **Make again** then downloads it
  from the recording in full quality. The Download node shows the quality it got.
- **Audio sync (live)**: YouTube's live video and audio lists have different lengths, so the downloaded video and
  audio can start a few seconds apart (seen: 6 s). The app reads each one's start time from ffmpeg's log and cuts
  each one at the same stream moment.
- Speed depends on your internet: with a slow connection, live reels can wait in line until their moment is
  older than the ~15 minutes the live feed keeps, and then fail.
- Needs **ffmpeg** (`brew install ffmpeg`). yt-dlp does all the downloading; ffmpeg only works on the
  downloaded file on your Mac.

### The face-cam box

Where the face cam is depends on the streamer, so the box is saved in `data/clip_layouts.json`:
for the stream (`video:<id>`) and, when there is a face cam, for the channel (default for its next streams).
IShowSpeed's GTA 6 layout is built in (`clips.py` → `DEFAULT_LAYOUTS`).

**Found by Gemini (2026-11-21):** the first reel of a stream with no saved box first takes 6 screenshots at random
times (yt-dlp downloads 1 s at each; finished: spread over the recording, live: over the last ~15 min) and sends
them to Gemini in one request (`facecam.py`). Gemini says for each frame if there is a webcam overlay of the real
streamer and its box (game characters don't count). The cam counts as found if it is in at least half of the
frames; the box is the median of those boxes. Saved like a drawn box; the frames and the answer are kept in the
stream's `ai/` folder (`facecam.json`, `facecam/*.jpg`). About 1–4 minutes, one request per stream. Also on the page:
**✨ Find the face cam with AI**, or by hand: `uv run python -m chat_spikes.facecam <video id or link> [--save]`.
If Gemini can't answer, the reel is made as before.

To change it: on a reel, press **Draw / Change the face-cam box**, drag a box on the frame, **Save and make again**.
**No face cam** (e.g. an outdoor IRL stream) makes the whole reel from the middle of the screen, for that stream only.
An optional `game` box (only in the JSON file) cuts off black bars around the game.

### How the piece is downloaded

- **Finished stream**: `yt-dlp --download-sections "*START-END"` downloads only those seconds (45 s took ~40 s).
- **Live stream**: yt-dlp can't cut a time range of a stream that is still live, so the app reads YouTube's live
  HLS feed, which keeps the last ~15 minutes. yt-dlp starts N two-second pieces back (`-live_start_index -N`).
  The pieces are numbered from the start of the stream (`.../sq/4249/...`), so piece number × 2 s is the stream
  time: the app knows exactly where the clip starts in the file. It waits until the end of the clip is on the feed.
  Downloading from the live feed is slow: about 2× the clip length (a 45 s reel took ~2–4 minutes in all).
- Right after a stream ends, YouTube processes the recording for a while ("post_live") and refuses downloads
  (403). The reel then fails with a clear message: try again later from the Finished stream page.
- Live moments older than ~14 minutes can't be cut from the live feed: make them after the stream ends.

## Posting reels to six platforms (chat-publish)

`chat-publish` posts every finished reel to Instagram, TikTok, X, Facebook, Pinterest and YouTube through each
platform's official API: the file goes straight from this Mac, in full quality, with no browser.

- One reel at a time, in that platform order, then a random 5–10 min pause; when nothing is left, a 1 h rest.
- Connect each account once: `uv run chat-publish setup youtube|meta|tiktok|x|pinterest` (Meta covers Instagram and
  Facebook). Keys and tokens are kept in `.secrets/publish.json` (git-ignored). Platforms not connected are skipped.
- `status`, `once [--dry-run]`, `run`, `retry <platform>`, `install` / `uninstall` (a launchd service that keeps the
  loop running in the background and starts it at login). Log: `output/publish.log`.
- Progress per reel and platform is in `upload_log.json`. Anything that needs you goes under `needs_human`.
- A platform that fails 3 times in a row, or loses its sign-in, is paused and the others continue. A post that was
  interrupted mid-upload is never retried on its own, so nothing is posted twice.
- Platform limits: YouTube keeps API uploads private until the Google project passes YouTube's audit, TikTok posts
  privately until its app audit, X needs a paid API plan for video and allows at most 140 s, and Pinterest needs
  Standard access (Trial apps write to the sandbox).
- Step-by-step setup (Persian): [publish-setup-fa.md](publish-setup-fa.md).

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
   saved in the stream's `chat/words.json` with a time for every word.
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

Answers are saved in the stream's `ai/` folder, so the same moment never uses quota twice.
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
  wait for turn → ask Gemini → exact cut → save), *Live watch* (see Live mode).
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

- The stream is still live → use **Live stream** mode (finished mode opens it after it ends).
- Chat replay turned off by the streamer, private or members-only stream → no chat to read.
- A normal video (not a past live stream) has no chat.

The page shows a clear message for each case.

## Code

| File | Job |
|------|-----|
| `src/chat_spikes/replay.py` | Downloads chat replay with yt-dlp, reads it while it downloads, saves a compact copy |
| `src/chat_spikes/clips.py` | Reels: download the piece (yt-dlp), face cam on top + middle of the screen below (ffmpeg), queue, layouts |
| `src/chat_spikes/facecam.py` | Finds the face cam: random screenshots of the stream → Gemini → the box (any side or corner) |
| `src/chat_spikes/live.py` | Live mode: yt-dlp live chat reader, stream time, real-time detection, reconnects, saving |
| `src/chat_spikes/detector.py` | Windows, rules, spike detection |
| `src/chat_spikes/evidence.py` | Free chat evidence per moment: standout words, reaction type, top message |
| `src/chat_spikes/transcript.py` | Captions of the stream with a time per word (yt-dlp), cut into numbered lines |
| `src/chat_spikes/gemini.py` | Text-only Gemini check: what happened + exact cut from the captions, model fallback, saved answers |
| `src/chat_spikes/report.py` | CSV files and the standalone chart |
| `src/chat_spikes/activity.py` | Records every job, its steps and every request; live feed for the Activity panel |
| `src/chat_spikes/library.py` | The stream library: one folder per stream under `streams/<channel>/`, paths inside it, moving the old layout |
| `src/chat_spikes/web.py` | Local web server for the pages (`chat-spikes-web`), incl. `/api/live/*`, `/api/library/*` |
| `src/chat_spikes/webui/` | `app.html` + `app.js` (the window with tabs), `index.html` (start page / new tab, saved streams), `finished.html` + `finished.js`, `live.html` + `live.js`, shared `common.js` (player, chart, moments, why card, chat), `activity.js`, `style.css` |
| `src/chat_spikes/cli.py` | Command line (`chat-spikes`) |
| `src/chat_spikes/publish/` | `chat-publish`: queue + `upload_log.json` (`queue.py`), one uploader per platform (`platforms.py`), HTTP/OAuth/secrets (`net.py`), loop and launchd service (`cli.py`) |

## Maintenance

If YouTube changes something and downloads start failing, update yt-dlp:

```bash
uv lock --upgrade-package yt-dlp && uv sync
```

## Existing tools on GitHub (checked 2026-11-17)

CHAD, chat-analyzer, stream-analyser, YBIGTA/24th-de-highlighter. All depend on the broken
`chat-downloader` library or are inactive, so this project has its own small detector.
