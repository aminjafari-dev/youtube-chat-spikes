// Chat Spikes (finished streams): reads the analysis from the local Python server.
const $ = (id) => document.getElementById(id);
const form = $("form"), btn = $("btn");
const MAX_BARS = 480; // the chart groups windows so it never draws more bars than this

let es = null, vid = null, duration = 0, rules = { window: 10 };
let windows = [];   // [start, score, messages, people, normal, spike]
let moments = [];   // from the server, biggest first
let sortBy = "score", selected = null;
let why = {};            // peak -> {evidence, ai, ai_error, has_key, loading}
let autoQueue = [];      // moments Gemini explains automatically after the analysis
const AUTO_TOP = 3;      // free tier: keep it small

const clock = (sec) => {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const ytAt = (sec) => `https://www.youtube.com/watch?v=${vid}&t=${Math.max(0, Math.floor(sec))}s`;

function videoId(url) {
  const m = url.match(/(?:v=|youtu\.be\/|\/live\/|\/shorts\/|\/embed\/)([\w-]{11})/);
  return m ? m[1] : (/^[\w-]{11}$/.test(url) ? url : null);
}

function setStatus(text, error = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", error);
}

function setProgress(pct, text) {
  $("progressWrap").hidden = pct === null;
  if (pct !== null) {
    $("progressFill").style.width = `${Math.min(100, Math.max(0, pct))}%`;
    $("progressText").textContent = text;
  }
}

function reset() {
  windows = []; moments = []; selected = null; duration = 0; why = {}; autoQueue = [];
  $("whyCard").hidden = true;
  ["tLen", "tMsgs", "tBase", "tTop"].forEach((id) => ($(id).textContent = "–"));
  $("tChannel").innerHTML = $("tTopSub").innerHTML = "&nbsp;";
  $("moments").innerHTML = '<li class="muted">Nothing yet.</li>';
  $("momentCount").textContent = "";
  $("chat").innerHTML = '<li class="muted">Pick a moment to read what chat said.</li>';
  $("chatCount").textContent = "";
  $("chatTitle").textContent = "Chat in the selected moment";
  $("chartEmpty").hidden = false;
  setProgress(null);
  drawChart();
}

// ---- Player: YouTube embed, controlled with postMessage (enablejsapi=1) ----
function showPlayer(id) {
  $("player").innerHTML = `<iframe id="yt" src="https://www.youtube-nocookie.com/embed/${id}?enablejsapi=1&rel=0"
    allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen title="Stream recording"></iframe>`;
}
function seek(sec) {
  const f = $("yt");
  if (!f) return;
  const send = (func, args = []) => f.contentWindow.postMessage(JSON.stringify({ event: "command", func, args }), "*");
  send("seekTo", [Math.max(0, sec), true]);
  send("playVideo");
}

// ---- Run ----
function start() {
  const id = videoId($("url").value.trim());
  if (!id) { setStatus("Paste a YouTube link first.", true); return; }
  reset();
  vid = id;
  showPlayer(id);
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

function handle({ type, data }) {
  switch (type) {
    case "rules": rules = data; break;
    case "status": setStatus(data); break;
    case "info":
      duration = data.duration;
      $("tLen").textContent = clock(data.duration);
      $("tChannel").textContent = data.channel || " ";
      setStatus(data.cached ? `${data.title} (saved earlier, loading instantly)` : `${data.title}: downloading the chat replay…`);
      setProgress(0, "starting…");
      break;
    case "windows":
      windows.push(...data);
      $("chartEmpty").hidden = true;
      drawChart();
      break;
    case "progress": {
      const pct = data.duration ? (data.reached / data.duration) * 100 : 0;
      $("tMsgs").textContent = data.messages.toLocaleString();
      setProgress(pct, `${clock(data.reached)} of ${clock(data.duration)} · ${data.messages.toLocaleString()} messages`);
      updateBaseline();
      break;
    }
    case "moments": moments = data; renderMoments(); break;
    case "done":
      moments = data.moments;
      $("tMsgs").textContent = data.messages.toLocaleString();
      setStatus(`Done. ${data.all_moments} moments found. Click one to watch it.`);
      setProgress(null);
      finish();
      updateBaseline();
      renderMoments();
      if (moments.length) selectMoment(0, false);
      autoQueue = moments.slice(0, AUTO_TOP);
      runAutoQueue();
      break;
    case "error": setStatus(data, true); setProgress(null); finish(); break;
  }
}

function updateBaseline() {
  if (!windows.length) return;
  const scores = windows.map((w) => w[1]).sort((a, b) => a - b);
  $("tBase").textContent = Math.round(scores[Math.floor(scores.length / 2)]);
}

// ---- Moments ----
function sortedMoments() {
  return moments.slice().sort((a, b) => (sortBy === "score" ? b.score - a.score : b.ratio - a.ratio));
}

function renderMoments() {
  const list = sortedMoments();
  $("momentCount").textContent = list.length ? `(${list.length})` : "";
  const top = moments[0];
  if (top) {
    $("tTop").textContent = clock(top.peak);
    $("tTopSub").textContent = `${Math.round(top.score)} per 10 s · ${top.ratio}× normal`;
  }
  if (!list.length) { $("moments").innerHTML = '<li class="muted">No spikes yet.</li>'; return; }
  $("moments").innerHTML = list.map((s, i) => `
    <li class="moment${selected === s.peak ? " sel" : ""}" data-i="${i}">
      <div class="row"><span class="rank">#${i + 1}</span><span class="time">${clock(s.peak)}</span>${badge(s)}
        <span class="ratio">${Math.round(s.score)} msgs · ${s.ratio}× · ${s.people} people</span></div>
      <div class="clip">Clip ${clock(s.clip_start)} → ${clock(s.clip_end)} (${Math.round(s.clip_end - s.clip_start)} s)
        · <a href="${ytAt(s.clip_start)}" target="_blank" rel="noopener">YouTube ↗</a></div>
      <div class="said">${esc(s.samples.slice(0, 3).join(" · "))}</div>
    </li>`).join("");
  drawChart();
}

$("moments").addEventListener("click", (e) => {
  if (e.target.closest("a")) return;
  const li = e.target.closest(".moment");
  if (li) selectMoment(+li.dataset.i, true);
});

document.querySelectorAll(".seg button").forEach((b) => b.addEventListener("click", () => {
  sortBy = b.dataset.sort;
  document.querySelectorAll(".seg button").forEach((x) => x.classList.toggle("on", x === b));
  renderMoments();
}));

// ---- Why? Free chat evidence + Gemini ----
function badge(s) {
  const w = why[s.peak];
  if (w && w.loading) return '<span class="vbadge wait" title="Gemini is watching this clip">…</span>';
  if (w && w.ai) return `<span class="vbadge s${Math.min(3, Math.floor(w.ai.viral_score / 4))}" title="Viral score from Gemini">${w.ai.viral_score}/10</span>`;
  return "";
}

const whyQuery = (s, ai) => `/api/why?v=${vid}&peak=${s.peak}&clip_start=${s.clip_start}&clip_end=${s.clip_end}&score=${s.score}&ratio=${s.ratio}&ai=${ai ? 1 : 0}`;

async function loadWhy(s, ai) {
  const cur = why[s.peak] || {};
  why[s.peak] = { ...cur, loading: ai, ai_error: null };
  renderMoments(); if (selected === s.peak) renderWhy(s);
  try {
    const r = await (await fetch(whyQuery(s, ai))).json();
    why[s.peak] = { ...r, loading: false };
  } catch {
    why[s.peak] = { ...cur, loading: false, ai_error: "Couldn't reach the app." };
  }
  renderMoments(); if (selected === s.peak) renderWhy(s);
}

async function runAutoQueue() {
  while (autoQueue.length) {
    const s = autoQueue.shift();
    if (!why[s.peak] || !why[s.peak].evidence) await loadWhy(s, false);
    const w = why[s.peak];
    if (!w.has_key) { autoQueue = []; break; }      // no Gemini key: evidence only
    if (!w.ai) await loadWhy(s, true);
    if (why[s.peak].ai_error) { autoQueue = []; break; } // e.g. free limit reached: stop asking
  }
}

const TYPE_ICON = { funny: "😂", shock: "😱", skill_win: "🔥", fail: "💔", rage: "😡", payoff: "🎉",
  wholesome: "🥹", drama: "🍿", announcement: "📣", technical_issue: "🛠", other: "❔" };

function renderWhy(s) {
  const card = $("whyCard"), w = why[s.peak];
  card.hidden = false;
  $("whyTitle").textContent = `Why did chat explode at ${clock(s.peak)}?`;
  $("whyModel").textContent = w && w.ai ? `Gemini · ${w.ai.model}` : "";
  if (!w || !w.evidence) {
    $("whyEvidence").innerHTML = '<p class="muted">Reading the chat…</p>'; $("whyAi").innerHTML = ""; return;
  }
  const e = w.evidence;
  $("whyEvidence").innerHTML = `
    <div class="ev-row"><span class="ev-label">Reaction</span>${e.types.map((t) => `<span class="chip">${esc(t.label)} ${t.share}%</span>`).join("") || '<span class="muted small">unclear</span>'}</div>
    <div class="ev-row"><span class="ev-label">Chat kept saying</span>${e.keywords.map((k) => `<span class="chip kw" title="${k.count} people · ${k.times_more}× more than usual">${esc(k.text)}</span>`).join("") || '<span class="muted small">nothing stands out</span>'}</div>
    ${e.top_message ? `<div class="ev-row"><span class="ev-label">Most repeated</span><span class="quote">“${esc(e.top_message.text)}” ×${e.top_message.count}</span></div>` : ""}`;

  const box = $("whyAi");
  if (w.ai) {
    const a = w.ai;
    box.innerHTML = `
      <div class="ai-head"><span class="vscore s${Math.min(3, Math.floor(a.viral_score / 4))}">${a.viral_score}<small>/10</small></span>
        <div><div class="ai-title">${esc(a.title)}</div>
        <div class="muted small">${TYPE_ICON[a.moment_type] || ""} ${esc(a.moment_type.replace("_", " "))} · confidence ${esc(a.confidence)} · hook: “${esc(a.hook)}”</div></div></div>
      <dl class="ai-body">
        <dt>What happened</dt><dd>${esc(a.what_happened)}</dd>
        <dt>Why chat reacted</dt><dd>${esc(a.why_chat_reacted)}</dd>
        <dt>Why it could go viral</dt><dd>${esc(a.why_viral)}</dd>
        <dt>Best clip</dt><dd><button class="linkbtn" id="playBest">▶ ${clock(a.best_start)} → ${clock(a.best_end)}</button>
          (${Math.round(a.best_end - a.best_start)} s) · <a href="${ytAt(a.best_start)}" target="_blank" rel="noopener">YouTube ↗</a></dd>
      </dl>`;
    $("playBest").onclick = () => seek(a.best_start);
  } else if (w.loading) {
    box.innerHTML = '<p class="ai-wait"><span class="spin"></span>Gemini is watching the clip… (about 1 minute)</p>';
  } else if (!w.has_key) {
    box.innerHTML = '<p class="muted small">Add <code>GEMINI_API_KEY=…</code> to the project\'s <code>.env</code> file to get Gemini\'s explanation (free key: aistudio.google.com → Get API key).</p>';
  } else {
    box.innerHTML = `${w.ai_error ? `<p class="err small">${esc(w.ai_error)}</p>` : ""}
      <button class="askbtn" id="askGemini">Ask Gemini why this could go viral</button>`;
    $("askGemini").onclick = () => loadWhy(s, true);
  }
}

async function selectMoment(i, play) {
  const s = sortedMoments()[i];
  if (!s) return;
  selected = s.peak;
  renderMoments();
  if (play) seek(s.clip_start);
  renderWhy(s);
  if (!why[s.peak] || !why[s.peak].evidence) loadWhy(s, false);
  $("chatTitle").textContent = `Chat at ${clock(s.clip_start)} → ${clock(s.clip_end)}`;
  $("chat").innerHTML = '<li class="muted">Loading…</li>';
  try {
    const rows = await (await fetch(`/api/chat?v=${vid}&from=${s.clip_start}&to=${s.clip_end}`)).json();
    $("chatCount").textContent = `${rows.length.toLocaleString()} messages`;
    $("chat").innerHTML = rows.map((m) => `
      <li class="${m.counted ? "" : "skip"}${m.t >= s.peak && m.t < s.peak + rules.window ? " peak" : ""}">
        <button class="ts" data-t="${m.t}">${clock(m.t)}</button><span class="who">${esc(m.author)}</span>${esc(m.text)}</li>`).join("")
      || '<li class="muted">No messages.</li>';
  } catch {
    $("chat").innerHTML = '<li class="muted">Couldn\'t load the chat.</li>';
  }
}

$("chat").addEventListener("click", (e) => {
  const b = e.target.closest(".ts");
  if (b) seek(+b.dataset.t - 3);
});

// ---- Chart: whole stream; bars grouped so it stays readable ----
let buckets = [];
function drawChart() {
  const svg = $("svg");
  const W = svg.clientWidth || 600, H = svg.clientHeight || 240;
  const padL = 34, padB = 22, padT = 22;
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  if (!windows.length) { svg.innerHTML = ""; buckets = []; return; }

  const step = rules.window || 10;
  const total = Math.max(duration, windows[windows.length - 1][0] + step);
  const nBars = Math.min(MAX_BARS, Math.ceil(total / step));
  const span = total / nBars;
  buckets = Array.from({ length: nBars }, (_, i) => ({ t0: i * span, t1: (i + 1) * span, score: 0, msgs: 0, spike: false, has: false }));
  for (const w of windows) {
    const b = buckets[Math.min(nBars - 1, Math.floor(w[0] / span))];
    b.score = Math.max(b.score, w[1]); b.msgs += w[2]; b.spike ||= !!w[5]; b.has = true;
  }
  const top = Math.max(10, ...buckets.map((b) => b.score)) * 1.08;
  const slot = (W - padL) / nBars;
  const gap = slot > 4 ? 1 : 0;
  const bw = Math.max(1, slot - gap);
  const x = (t) => padL + (t / total) * (W - padL);
  const y = (v) => padT + (1 - v / top) * (H - padT - padB);

  let out = "";
  const tick = top > 300 ? 100 : top > 120 ? 50 : top > 60 ? 20 : 10;
  for (let v = 0; v <= top; v += tick) {
    out += `<line class="grid-line" x1="${padL}" x2="${W}" y1="${y(v)}" y2="${y(v)}"/>`;
    out += `<text class="axis" x="${padL - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  buckets.forEach((b, i) => {
    if (!b.has || b.score <= 0) return;
    const x0 = padL + i * slot, y0 = y(b.score), h = y(0) - y0, r = Math.min(3, bw / 2, h);
    out += `<path class="bar${b.spike ? " spike" : ""}" d="M${x0},${y(0)}V${y0 + r}Q${x0},${y0} ${x0 + r},${y0}H${x0 + bw - r}Q${x0 + bw},${y0} ${x0 + bw},${y0 + r}V${y(0)}Z"/>`;
  });
  // Numbered markers for the 5 biggest moments
  moments.slice(0, 5).forEach((s, i) => {
    const cx = x(s.peak + step / 2);
    out += `<g class="mark${selected === s.peak ? " sel" : ""}"><circle cx="${cx}" cy="${padT - 10}" r="8"/>
      <text x="${cx}" y="${padT - 6}" text-anchor="middle">${i + 1}</text></g>`;
  });
  if (selected !== null) {
    out += `<line class="cursor" x1="${x(selected + step / 2)}" x2="${x(selected + step / 2)}" y1="${padT}" y2="${y(0)}"/>`;
  }
  for (let i = 0; i <= 6; i++) {
    const t = (total * i) / 6;
    out += `<text class="axis" x="${Math.min(Math.max(x(t), padL + 20), W - 24)}" y="${H - 5}" text-anchor="middle">${clock(t)}</text>`;
  }
  out += `<rect class="hit" x="${padL}" y="0" width="${W - padL}" height="${H - padB}"/>`;
  svg.innerHTML = out;
}

function bucketAt(e) {
  const svg = $("svg"), r = svg.getBoundingClientRect();
  const padL = 34, frac = (e.clientX - r.left - padL) / (r.width - padL);
  if (frac < 0 || frac > 1 || !buckets.length) return null;
  return buckets[Math.min(buckets.length - 1, Math.floor(frac * buckets.length))];
}

$("svg").addEventListener("mousemove", (e) => {
  const b = bucketAt(e), tip = $("tip");
  if (!b) { tip.hidden = true; return; }
  const box = $("chart").getBoundingClientRect();
  tip.innerHTML = `<b>${clock(b.t0)} – ${clock(b.t1)}</b>${b.spike ? " · SPIKE" : ""}<br>
    ${b.has ? `peak ${Math.round(b.score)} per 10 s · ${b.msgs.toLocaleString()} messages` : "no chat yet"}<br>click to watch`;
  tip.style.left = `${Math.min(Math.max(e.clientX - box.left, 120), box.width - 120)}px`;
  tip.style.top = `${e.clientY - box.top}px`;
  tip.hidden = false;
});
$("svg").addEventListener("mouseleave", () => ($("tip").hidden = true));
$("svg").addEventListener("click", (e) => { const b = bucketAt(e); if (b) seek(b.t0); });
window.addEventListener("resize", drawChart);
reset();
