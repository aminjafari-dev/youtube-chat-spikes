// Finished-stream page: downloads (or reads the saved) chat of a stream that has ended, then shows its spikes.
import { $, clock, createPlayer, createSpikeView, mountParts, setStatus, videoId } from "./common.js";

mountParts();
const form = $("form"), btn = $("btn");
const player = createPlayer($("player"));
let es = null, duration = 0;

const view = createSpikeView({
  range: () => {
    const last = view.windows.length ? view.windows[view.windows.length - 1][0] + (view.rules.window || 10) : 0;
    return [0, Math.max(duration, last)];
  },
  seek: (t) => player.seek(t),
  aiMode: "full",
});

function setProgress(pct, text) {
  $("progressWrap").hidden = pct === null;
  if (pct !== null) {
    $("progressFill").style.width = `${Math.min(100, Math.max(0, pct))}%`;
    $("progressText").textContent = text;
  }
}

function reset() {
  duration = 0;
  ["tLen", "tMsgs", "tBase", "tTop"].forEach((id) => ($(id).textContent = "–"));
  $("tChannel").innerHTML = $("tTopSub").innerHTML = "&nbsp;";
  $("tMsgsSub").textContent = "in the replay";
  setProgress(null);
  view.reset();
}

// ---- Run ----
function start() {
  const id = videoId($("url").value.trim());
  if (!id) { setStatus("Paste a YouTube link first.", true); return; }
  reset();
  view.vid = id;
  history.replaceState(null, "", `?v=${id}`);
  player.show(id, "Stream recording");
  es = new EventSource(`/api/analyze?url=${encodeURIComponent(id)}`);
  es.onmessage = (e) => handle(JSON.parse(e.data));
  es.onerror = () => { if (es) { setStatus("Lost connection to the app. Is it still running in the terminal?", true); finish(); } };
  btn.textContent = "Cancel"; btn.classList.add("stop");
  $("busyDot").classList.add("on");
}

function finish() {
  if (es) { es.close(); es = null; }
  btn.textContent = "Analyze"; btn.classList.remove("stop");
  $("busyDot").classList.remove("on");
}

form.addEventListener("submit", (e) => {
  e.preventDefault();
  if (es) { finish(); setStatus("Cancelled. Nothing was saved for this stream."); setProgress(null); }
  else start();
});

function updateTiles() {
  const n = view.normalLevel();
  if (n !== null) $("tBase").textContent = Math.round(n);
  const top = view.top();
  if (top) {
    $("tTop").textContent = clock(top.peak);
    $("tTopSub").textContent = `${Math.round(top.score)} per 10 s · ${top.ratio}× normal`;
  }
}

function handle({ type, data }) {
  switch (type) {
    case "rules": view.rules = data; break;
    case "status": setStatus(data); break;
    case "info":
      duration = data.duration;
      $("tLen").textContent = clock(data.duration);
      $("tChannel").textContent = data.channel || " ";
      document.title = `${data.channel || data.title} · Chat Spikes`;
      if (data.recorded_live) $("tMsgsSub").textContent = `recorded live from ${clock(data.first)}`;
      setStatus(data.recorded_live
        ? `${data.title} (chat recorded in live mode from ${clock(data.first)}, loading instantly)`
        : data.cached ? `${data.title} (saved earlier, loading instantly)` : `${data.title}: downloading the chat replay…`);
      setProgress(0, "starting…");
      break;
    case "windows":
      view.addWindows(data);
      view.drawChart();
      break;
    case "progress": {
      const pct = data.duration ? (data.reached / data.duration) * 100 : 0;
      $("tMsgs").textContent = data.messages.toLocaleString();
      setProgress(pct, `${clock(data.reached)} of ${clock(data.duration)} · ${data.messages.toLocaleString()} messages`);
      updateTiles();
      break;
    }
    case "moments": view.moments = data; view.renderMoments(); view.drawChart(); updateTiles(); break;
    case "done":
      view.moments = data.moments;
      $("tMsgs").textContent = data.messages.toLocaleString();
      setStatus(`Done. ${data.all_moments} moments found. Click one to watch it.`);
      setProgress(null);
      finish();
      updateTiles();
      view.renderMoments();
      view.drawChart();
      view.loadClips();
      if (view.moments.length) view.selectMoment(view.moments[0], false);
      break;
    case "error": setStatus(data, true); setProgress(null); finish(); break;
  }
}

reset();
// Opened with ?v=VIDEO_ID (e.g. from the live page after a stream ended): analyze it right away.
const fromUrl = new URLSearchParams(location.search).get("v");
if (fromUrl) { $("url").value = fromUrl; start(); }
