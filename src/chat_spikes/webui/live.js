// Live page: the app reads chat of a live stream on the server (live.py); this page shows it as it happens.
// The watch keeps running when the page is closed or reloaded; the page just reconnects to /api/live/feed.
import { $, clock, createPlayer, createSpikeView, esc, mountParts, setStatus, store, videoId } from "./common.js";

mountParts();
const form = $("form"), btn = $("btn");
const LIVE_CHAT_MAX = 300;
const SPANS = [[600, "10 min"], [1800, "30 min"], [7200, "2 h"], [0, "All"]];

let es = null, vid = null, shownVid = null, info = null;
let stats = null, statsAt = 0, running = false, finished = false;
let known = new Set();          // moments already seen (to spot new ones)
let chatTab = "live", unseen = 0;
let span = +(store.get("liveSpan") ?? 1800);
const alerts = { sound: store.get("liveSound") === "1", notify: store.get("liveNotify") === "1" };

const player = createPlayer($("player"), { onInfo: showPlayerPos });

// Stream time right now: the server's "live for" plus the time since it was sent.
const liveNow = () => !stats ? 0 : running ? stats.live_for + (Date.now() - statsAt) / 1000 : (stats.last ?? stats.live_for);
const lastWindowEnd = () => (view.windows.length ? view.windows[view.windows.length - 1][0] + (view.rules.window || 10) : null);

const view = createSpikeView({
  range: () => {
    const now = Math.max(liveNow(), lastWindowEnd() ?? 0);
    const first = view.windows.length ? view.windows[0][0] : now - 60;
    return [Math.floor(span ? Math.max(first, now - span) : first), now];
  },
  seek: (t) => seekStream(t),
  aiMode: "chat",
  noMoments: "No spikes yet. They show up here as they happen.",
  onSelect: () => setChatTab("moment"),
  // Right edge: chat that is still on its way (YouTube → yt-dlp → app takes ~10–15 s, plus the 10 s window).
  extraMarks: ({ x, t1, padT, H, padB }) => {
    const end = lastWindowEnd();
    if (!running || end === null || end >= t1) return "";
    return `<rect class="pending" x="${x(end)}" y="${padT}" width="${Math.max(0, x(t1) - x(end))}" height="${H - padT - padB}"/>
      <text class="axis" x="${x(t1) - 4}" y="${padT + 12}" text-anchor="end">chat on its way</text>`;
  },
});

// ---- Player: rewinding a live stream (DVR) ----
// The embed counts time from the video's 0:00. It also says the real clock time of the frame it shows
// (progressState.ingestionTime), so 0:00 = ingestionTime - currentTime. That is sent to the app once
// (/api/live/sync), which then uses it for all stream times, so moments match the video exactly.
// Rewinding only works inside YouTube's DVR window (about the last 12 hours).
let measuredStart = null, syncSentAt = 0;
function seekStream(t) {
  const shift = measuredStart && info ? info.started - measuredStart : 0;  // 0 once the app has synced
  player.seek(t + shift);
  $("playerBar").hidden = false;
}
function syncStart(p) {
  const ps = p.progressState;
  if (!ps || !ps.ingestionTime || !ps.current || !ps.isAtLiveHead || !info || !running) return;
  measuredStart = ps.ingestionTime - ps.current;
  const off = Math.abs(measuredStart - info.started);
  if ((off >= 2 || !stats.synced) && Date.now() - syncSentAt > 20000) {
    syncSentAt = Date.now();
    fetch(`/api/live/sync?v=${vid}&start=${measuredStart.toFixed(3)}`);
  }
}
function goLive() {
  // Far past the end = the live edge.
  const ps = player.info.progressState || {};
  player.seek(Math.max(liveNow(), ps.seekableEnd || 0) + 3600);
}
function showPlayerPos(p) {
  if (!vid) return;
  syncStart(p);
  $("playerBar").hidden = false;
  const ps = p.progressState || {};
  const atLive = ps.isAtLiveHead ?? true;
  const behind = Math.max(0, (ps.seekableEnd ?? p.currentTime ?? 0) - (p.currentTime || 0));
  $("playerPos").textContent = p.currentTime
    ? `Player at ${clock(p.currentTime)}${atLive ? " · live" : ` · ${clock(behind)} behind live`}`
    : "";
  $("goLive").classList.toggle("on", atLive);
}
$("goLive").addEventListener("click", goLive);

// ---- Extra controls in the shared cards ----
$("chartTools").innerHTML = `<div class="seg" role="group" aria-label="Chart range">${SPANS.map(([s, l]) =>
  `<button type="button" data-span="${s}" class="${s === span ? "on" : ""}">${l}</button>`).join("")}</div>`;
$("chartTools").addEventListener("click", (e) => {
  const b = e.target.closest("[data-span]");
  if (!b) return;
  span = +b.dataset.span; store.set("liveSpan", span);
  $("chartTools").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
  view.drawChart();
});

$("momentTools").innerHTML = `<div class="alerts small">
  <label><input type="checkbox" id="aSound" ${alerts.sound ? "checked" : ""}> 🔔 Sound on a new moment</label>
  <label><input type="checkbox" id="aNotify" ${alerts.notify ? "checked" : ""}> Notification (when this tab is hidden)</label></div>`;
$("aSound").addEventListener("change", (e) => { alerts.sound = e.target.checked; store.set("liveSound", alerts.sound ? "1" : "0"); if (alerts.sound) beep(); });
$("aNotify").addEventListener("change", async (e) => {
  alerts.notify = e.target.checked;
  if (alerts.notify && "Notification" in window && Notification.permission === "default") await Notification.requestPermission();
  if (alerts.notify && (!("Notification" in window) || Notification.permission !== "granted")) {
    alerts.notify = e.target.checked = false;
    setStatus("The browser doesn't allow notifications for this page.", true);
  }
  store.set("liveNotify", alerts.notify ? "1" : "0");
});

$("chatTools").innerHTML = `<div class="seg chat-tabs" role="group" aria-label="Chat">
  <button type="button" data-tab="live" class="on">Live chat <span id="unseen" class="unseen" hidden></span></button>
  <button type="button" data-tab="moment">Selected moment</button></div>`;
$("chat").insertAdjacentHTML("afterend", '<ul id="liveChat" class="chat live-chat"><li class="muted">Live chat shows here.</li></ul>');
$("chatTools").addEventListener("click", (e) => { const b = e.target.closest("[data-tab]"); if (b) setChatTab(b.dataset.tab); });
$("liveChat").addEventListener("click", (e) => { const b = e.target.closest(".ts"); if (b) seekStream(+b.dataset.t - 3); });

function setChatTab(tab) {
  chatTab = tab;
  $("chatTools").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x.dataset.tab === tab));
  $("chat").hidden = tab !== "moment";
  $("liveChat").hidden = tab !== "live";
  if (tab === "live") {
    $("chatTitle").textContent = "Live chat";
    $("chatCount").textContent = "newest at the bottom";
    unseen = 0; $("unseen").hidden = true;
    const ul = $("liveChat"); ul.scrollTop = ul.scrollHeight;
  } else if (view.selected === null) {
    $("chatTitle").textContent = "Chat in the selected moment"; $("chatCount").textContent = "";
  }
}

function addLiveChat(rows, initial = false) {
  if (!rows.length) return;
  const ul = $("liveChat");
  const atBottom = ul.scrollHeight - ul.scrollTop - ul.clientHeight < 40;
  if (ul.querySelector(".muted")) ul.innerHTML = "";
  ul.insertAdjacentHTML("beforeend", rows.slice(-LIVE_CHAT_MAX).map(([t, a, x]) =>
    `<li><button class="ts" data-t="${t}">${clock(t)}</button><span class="who">${esc(a)}</span>${esc(x)}</li>`).join(""));
  while (ul.children.length > LIVE_CHAT_MAX) ul.firstElementChild.remove();
  if (atBottom || initial) ul.scrollTop = ul.scrollHeight;
  if (chatTab !== "live" && !initial) {
    unseen += rows.length;
    $("unseen").textContent = unseen > 999 ? "999+" : unseen;
    $("unseen").hidden = false;
  }
}

// ---- New moment: make it easy to notice ----
let audio = null;
function beep() {
  try {
    audio = audio || new AudioContext();
    const o = audio.createOscillator(), g = audio.createGain();
    o.frequency.value = 880; o.connect(g); g.connect(audio.destination);
    g.gain.setValueAtTime(0.0001, audio.currentTime);
    g.gain.exponentialRampToValueAtTime(0.25, audio.currentTime + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, audio.currentTime + 0.35);
    o.start(); o.stop(audio.currentTime + 0.4);
  } catch {}
}
let titleBase = document.title, newCount = 0;
function announce(list) {
  newCount += list.length;
  document.title = `(${newCount} new) ${titleBase}`;
  const s = list[0];
  if (alerts.sound) beep();
  if (alerts.notify && document.hidden && "Notification" in window && Notification.permission === "granted") {
    const n = new Notification(`Chat spike at ${clock(s.peak)}`, { body: `${Math.round(s.score)} msgs in 10 s · ${s.ratio}× normal\n${s.samples.slice(0, 2).join(" · ")}`, tag: `spike-${vid}` });
    n.onclick = () => { window.focus(); view.selectMoment(s, true); };
  }
}
const clearTitle = () => { newCount = 0; document.title = titleBase; };
window.addEventListener("focus", clearTitle);
document.addEventListener("click", clearTitle);

function takeMoments(list, alert) {
  const fresh = list.filter((m) => !known.has(m.peak));
  list.forEach((m) => known.add(m.peak));
  view.moments = list;
  // Only moments from the last 10 minutes are news (not old ones loaded from an earlier recording).
  const news = fresh.filter((m) => m.peak >= liveNow() - 600);
  if (alert && news.length) {
    news.forEach((m) => view.fresh.add(m.peak));
    announce(news.sort((a, b) => b.score - a.score));
  }
  view.renderMoments();
}

// ---- Tiles and status ----
function updateTiles() {
  if (!stats) return;
  $("tLive").textContent = clock(liveNow());
  $("tMsgs").textContent = stats.messages.toLocaleString();
  $("tMsgsSub").textContent = stats.first != null ? `since ${clock(stats.first)}` : "read so far";
  $("tRate").textContent = running ? stats.per_min.toLocaleString() : "–";
  $("tRateSub").textContent = running ? `last minute · ${stats.per_min_5}/min over 5 min` : "not reading";
  $("tBase").textContent = stats.normal != null ? Math.round(stats.normal) : "–";
  const top = view.top();
  if (top) {
    $("tTop").textContent = clock(top.peak);
    $("tTopSub").textContent = `${Math.round(top.score)} per 10 s · ${top.ratio}× normal`;
  }
}

function showState() {
  if (!stats) return;
  const ago = stats.last_msg_ago != null && running ? stats.last_msg_ago + (Date.now() - statsAt) / 1000 : null;
  $("livePill").hidden = !(running && stats.state === "reading");
  $("busyDot").classList.toggle("on", running);
  const name = info && info.title ? `${info.channel || info.title}: ` : "";
  switch (stats.state) {
    case "starting": setStatus(`${name}checking the stream…`); break;
    case "connecting": setStatus(`${name}connecting to chat (yt-dlp)…`); break;
    case "reading": setStatus(`${name}reading chat · last message ${ago != null ? Math.round(ago) : "–"} s ago · ` +
      "chat arrives about 10–15 s late" + (stats.synced ? "" : " · times from YouTube's start time until the player matches them") +
      (stats.note ? ` · ${stats.note}` : "")); break;
    case "reconnecting": setStatus(`${name}${stats.note || "reconnecting…"}`, true); break;
    case "error": setStatus(stats.note || "Something went wrong. See the Activity panel.", true); break;
    default: break;
  }
}

function setButton() {
  btn.textContent = running ? "Stop" : "Watch live";
  btn.classList.toggle("stop", running);
}

// ---- Feed from the app ----
function connect(id) {
  if (es) es.close();
  vid = id; view.vid = id; finished = false;
  history.replaceState(null, "", `?v=${id}`);
  es = new EventSource(`/api/live/feed?v=${id}`);
  es.onmessage = (e) => handle(JSON.parse(e.data));
  es.onerror = () => {
    // EventSource retries on its own; the watch keeps running on the app meanwhile.
    if (!finished) setStatus("Lost connection to the app. Retrying… (is it still running in the terminal?)", true);
  };
}

function takeStats(s, newInfo) {
  stats = s; statsAt = Date.now();
  if (newInfo) {
    info = newInfo;
    titleBase = `${info.channel || info.title || "Live"} · Chat Spikes Live`;
    if (!newCount) document.title = titleBase;
    $("tChannel").textContent = info.channel || " ";
    if (info.title && shownVid !== vid) { player.show(vid, info.title, { autoplay: true }); shownVid = vid; }
  }
  running = s.running;
  setButton();
}

function handle({ type, data }) {
  switch (type) {
    case "snapshot": {
      // First load, reconnect, or all times changed after a sync: draw everything again.
      const keepSel = view.selected;
      view.reset();
      known = new Set();
      $("liveChat").innerHTML = "";
      view.rules = data.rules;
      takeStats(data.stats, data.info.title ? data.info : null);
      view.addWindows(data.windows);
      takeMoments(data.moments, false);
      addLiveChat(data.chat, true);
      setChatTab(keepSel === null ? "live" : chatTab);
      break;
    }
    case "update":
      takeStats(data.stats, data.info);
      view.addWindows(data.windows);
      if (data.moments) takeMoments(data.moments, running);
      addLiveChat(data.chat);
      break;
    case "ended": {
      finished = true; running = false;
      es.close(); es = null;
      takeStats(data, null);
      setButton();
      const n = `${data.messages.toLocaleString()} messages, ${view.moments.length} moment${view.moments.length === 1 ? "" : "s"}`;
      if (data.state === "ended") {
        $("status").innerHTML = `The stream ended. ${n} saved. <a href="finished.html?v=${vid}">Open it in Finished stream mode</a> (instant, from the saved chat).`;
        $("status").classList.remove("error");
      } else if (data.state === "error") {
        setStatus(`Stopped: ${data.note}`, true);
      } else {
        setStatus(`Stopped. ${n} saved in data/raw/${vid}.live.jsonl. Press Watch live to continue.`);
      }
      $("livePill").hidden = true;
      $("busyDot").classList.remove("on");
      break;
    }
    case "gone":
      finished = true; es.close(); es = null; running = false; setButton();
      $("url").value = vid;
      setStatus(data);
      break;
  }
  updateTiles();
  showState();
  view.drawChart();
}

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  if (running) {
    setStatus("Stopping…");
    await fetch(`/api/live/stop?v=${vid}`);
    return;
  }
  const id = videoId($("url").value.trim()) || (!$("url").value.trim() && vid);
  if (!id) { setStatus("Paste a YouTube link first.", true); return; }
  setStatus("Starting…");
  const r = await (await fetch(`/api/live/start?url=${encodeURIComponent(id)}`)).json();
  if (r.error) { setStatus(r.error, true); return; }
  if (id !== shownVid) { shownVid = null; player.clear("The live stream will show here"); }
  connect(r.video_id);
});

// Keep the clock, tiles and chart moving between updates.
setInterval(() => { if (running) { updateTiles(); showState(); view.drawChart(); } }, 1000);

// Reload / come back: reconnect to the watch from the address (?v=), or to whatever is being watched now.
const fromUrl = new URLSearchParams(location.search).get("v");
setChatTab("live");
if (fromUrl) connect(fromUrl);
else {
  const list = await (await fetch("/api/live/list")).json();
  const w = list.find((x) => x.running);
  if (w) connect(w.video_id);
}
