// Chat Spikes page: talks to the local Python server over Server-Sent Events.
const $ = (id) => document.getElementById(id);
const form = $("form"), btn = $("btn"), status = $("status");
const MAX_BARS = 60;   // 60 x 10 s = last 10 minutes on the chart
const MAX_CHAT = 150;

let es = null, windows = [], msgCount = 0, rules = { window: 10 }, currentVid = null;

// Stream time: how far into the stream (h:mm:ss), the same time you see on YouTube's player.
const clock = (sec) => {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
const ytAt = (sec) => `https://www.youtube.com/watch?v=${currentVid}&t=${Math.max(0, Math.floor(sec))}s`;
const esc = (s) => s.replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function videoId(url) {
  const m = url.match(/(?:v=|youtu\.be\/|\/live\/|\/shorts\/|\/embed\/)([\w-]{11})/);
  return m ? m[1] : (/^[\w-]{11}$/.test(url) ? url : null);
}

function setStatus(text, error = false) {
  status.textContent = text;
  status.classList.toggle("error", error);
}

function reset() {
  windows = []; msgCount = 0;
  $("chat").innerHTML = "";
  $("moments").innerHTML = '<li class="muted">Nothing yet. Spikes appear here.</li>';
  ["tNow", "tBase", "tViewers"].forEach((id) => ($(id).textContent = "–"));
  $("tSpikes").textContent = "0";
  $("msgCount").textContent = "0 messages";
  $("chartEmpty").hidden = false;
  drawChart();
}

function showPlayer(id) {
  const p = $("player");
  p.innerHTML = id
    ? `<iframe src="https://www.youtube-nocookie.com/embed/${id}?autoplay=1&mute=1" allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen title="Stream"></iframe>`
    : '<div class="placeholder">No video for demo mode. Add a YouTube link to watch one while the fake chat runs.</div>';
}

function start() {
  const url = $("url").value.trim();
  const source = $("source").value;
  if (source === "youtube" && !videoId(url)) {
    setStatus("Paste a YouTube link first (or choose Demo).", true);
    return;
  }
  reset();
  currentVid = videoId(url);
  showPlayer(currentVid);
  es = new EventSource(`/api/run?source=${source}&url=${encodeURIComponent(url)}`);
  es.onmessage = (e) => handle(JSON.parse(e.data));
  es.onerror = () => { if (es) { setStatus("Lost connection to the app. Is it still running in the terminal?", true); stop(false); } };
  btn.textContent = "Stop"; btn.classList.add("stop");
  $("liveDot").classList.add("on");
  setStatus("Connecting…");
}

function stop(clearStatus = true) {
  if (es) { es.close(); es = null; }
  btn.textContent = "Start"; btn.classList.remove("stop");
  $("liveDot").classList.remove("on");
  if (clearStatus) setStatus("Stopped. Results were saved in the project's output folder.");
}

form.addEventListener("submit", (e) => { e.preventDefault(); es ? stop() : start(); });

function handle({ type, data }) {
  switch (type) {
    case "rules": rules = data; break;
    case "info":
      setStatus(`Watching: ${data.title}${data.stream_time_now ? ` · stream is at ${clock(data.stream_time_now)}` : ""}`);
      break;
    case "viewers": $("tViewers").textContent = Number(data).toLocaleString(); break;
    case "message": addChat(data); break;
    case "window": addWindow(data); break;
    case "spikes": showMoments(data); break;
    case "error": setStatus(data, true); stop(false); break;
    case "end": setStatus("The stream or chat ended."); stop(false); break;
  }
}

function addChat(m) {
  const ul = $("chat");
  const atBottom = ul.scrollHeight - ul.scrollTop - ul.clientHeight < 40;
  const li = document.createElement("li");
  if (!m.counted) li.className = "skip";
  li.innerHTML = `<span class="who">${esc(m.author)}</span>${esc(m.text)}`;
  ul.appendChild(li);
  while (ul.children.length > MAX_CHAT) ul.firstChild.remove();
  if (atBottom) ul.scrollTop = ul.scrollHeight;
  $("msgCount").textContent = `${(++msgCount).toLocaleString()} messages`;
}

function addWindow(w) {
  windows.push(w);
  if (windows.length > MAX_BARS) windows.shift();
  $("chartEmpty").hidden = true;
  $("tNow").textContent = Math.round(w.score);
  $("tBase").textContent = w.baseline ? Math.round(w.baseline) : "…";
  $("tNow").parentElement.classList.toggle("hot", w.spike);
  drawChart();
}

function showMoments(list) {
  $("tSpikes").textContent = list.length;
  const ol = $("moments");
  if (!list.length) return;
  const prev = ol.querySelectorAll(".moment").length;
  ol.innerHTML = list.slice().reverse().map((s, i) => `
    <li class="moment${i === 0 && list.length > prev ? " new" : ""}">
      <div class="row"><span class="time">${clock(s.peak)}</span><span class="ratio">${s.ratio}× normal · ${s.people} people</span></div>
      <div class="clip">Clip ${clock(s.clip_start)} → ${clock(s.clip_end)} (${Math.round(s.clip_end - s.clip_start)} s)
        ${currentVid ? `· <a href="${ytAt(s.clip_start)}" target="_blank" rel="noopener">open on YouTube ↗</a>` : ""}</div>
      <div class="said" title="${esc(s.samples.join(" | "))}">${esc(s.samples.slice(0, 3).join(" · "))}</div>
    </li>`).join("");
}

// ---- Chart: one bar per 10-second window, dashed spike line, hover tooltip ----
function drawChart() {
  const svg = $("svg");
  const W = svg.clientWidth || 600, H = svg.clientHeight || 240;
  const padL = 30, padB = 22, padT = 8;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!windows.length) { svg.innerHTML = ""; return; }

  const top = Math.max(10, ...windows.map((w) => Math.max(w.score, w.limit))) * 1.1;
  const slot = (W - padL) / MAX_BARS;
  const bw = Math.max(2, slot - 2);                 // 2px gap between bars
  const x = (i) => padL + (MAX_BARS - windows.length + i) * slot + 1;
  const y = (v) => padT + (1 - v / top) * (H - padT - padB);

  let out = "";
  const step = top > 60 ? 20 : top > 30 ? 10 : 5;
  for (let v = 0; v <= top; v += step) {
    out += `<line class="grid-line" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/>`;
    out += `<text class="axis" x="${padL - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  windows.forEach((w, i) => {
    const h = Math.max(0, y(0) - y(w.score));
    const r = Math.min(4, bw / 2, h);
    // Bar rounded on top only, flat on the baseline.
    const x0 = x(i), y0 = y(w.score), x1 = x0 + bw, yb = y(0);
    const d = h <= 0 ? "" : `M${x0},${yb}V${y0 + r}Q${x0},${y0} ${x0 + r},${y0}H${x1 - r}Q${x1},${y0} ${x1},${y0 + r}V${yb}Z`;
    out += `<rect class="hit" data-i="${i}" x="${x0 - 1}" y="${padT}" width="${slot}" height="${H - padT - padB}"/>`;
    out += `<path class="bar${w.spike ? " spike" : ""}" d="${d}"/>`;
  });
  const pts = windows.map((w, i) => `${x(i) + bw / 2},${y(w.limit)}`).join(" ");
  out += `<polyline class="limit" points="${pts}"/>`;
  // Time labels: first, middle, last bar.
  [0, Math.floor(windows.length / 2), windows.length - 1].forEach((i) => {
    if (i < 0 || (i > 0 && i === windows.length - 1 && windows.length < 3)) return;
    out += `<text class="axis" x="${x(i) + bw / 2}" y="${H - 6}" text-anchor="middle">${clock(windows[i].start)}</text>`;
  });
  svg.innerHTML = out;
}

$("svg").addEventListener("mousemove", (e) => {
  const t = e.target.closest(".hit");
  const tip = $("tip");
  if (!t) { tip.hidden = true; return; }
  const w = windows[+t.dataset.i];
  const box = $("chart").getBoundingClientRect();
  tip.innerHTML = `<b>${clock(w.start)}</b> · score ${Math.round(w.score)}${w.spike ? " · SPIKE" : ""}<br>
    ${w.raw} messages, ${w.counted} counted, ${w.people} people<br>
    normal ${w.baseline || "…"} · spike line ${w.limit}`;
  tip.style.left = `${Math.min(Math.max(e.clientX - box.left, 110), box.width - 110)}px`;
  tip.style.top = `${e.clientY - box.top}px`;
  tip.hidden = false;
});
$("svg").addEventListener("mouseleave", () => ($("tip").hidden = true));
window.addEventListener("resize", drawChart);
reset();
