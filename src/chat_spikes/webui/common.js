// Shared by both pages (finished stream and live): player, chart, moments, "why" card, chat of a moment.
// Each page puts empty placeholders like <div data-part="chart"></div>; mountParts() fills them.

export const $ = (id) => document.getElementById(id);
export const clock = (sec) => {
  const s = Math.max(0, Math.floor(sec));
  return `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
};
export const clockMs = (sec) => `${clock(sec)}.${Math.floor((Math.max(0, sec) % 1) * 10)}`;
export const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
export const store = {
  get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} },
};

export function videoId(url) {
  const m = url.match(/(?:v=|youtu\.be\/|\/live\/|\/shorts\/|\/embed\/)([\w-]{11})/);
  return m ? m[1] : (/^[\w-]{11}$/.test(url) ? url : null);
}

export function setStatus(text, error = false) {
  $("status").textContent = text;
  $("status").classList.toggle("error", error);
}

// ---- Shared page parts ----
const PARTS = {
  why: () => `
    <div class="card why" id="whyCard" hidden>
      <div class="card-head">
        <h2 id="whyTitle">Why did chat explode?</h2>
        <span class="muted small" id="whyModel"></span>
      </div>
      <div id="whyEvidence" class="evidence"></div>
      <div id="whyAi" class="ai"></div>
    </div>`,
  chart: (el) => `
    <div class="card">
      <div class="card-head">
        <h2 id="chartTitle">${esc(el.dataset.title || "Chat activity")}</h2>
        <div class="chart-tools">
          <div class="key">
            <span><i class="sw normal"></i>normal</span>
            <span><i class="sw spike"></i>spike</span>
            <span><i class="sw mark">1</i>top moments</span>
          </div>
          <span id="chartTools"></span>
        </div>
      </div>
      <div class="chart" id="chart">
        <svg id="svg"></svg>
        <div class="tip" id="tip" hidden></div>
        <div class="empty" id="chartEmpty">${esc(el.dataset.empty || "The chart fills in as chat is read.")}</div>
      </div>
      <p class="hint small muted">${esc(el.dataset.hint || "Click anywhere on the chart to jump the video to that time.")}</p>
    </div>`,
  moments: () => `
    <div class="card moments">
      <div class="card-head">
        <h2>Moments to clip <span class="muted small" id="momentCount"></span></h2>
        <div class="seg" role="group" aria-label="Sort moments">
          <button type="button" data-sort="score" class="on">Most messages</button>
          <button type="button" data-sort="ratio">Most × normal</button>
        </div>
      </div>
      <div id="momentTools"></div>
      <ol id="moments" class="moment-list"><li class="muted">Nothing yet.</li></ol>
    </div>`,
  chat: () => `
    <div class="card chatcard">
      <div class="card-head">
        <h2 id="chatTitle">Chat in the selected moment</h2>
        <span class="muted small" id="chatCount"></span>
      </div>
      <div id="chatTools"></div>
      <ul id="chat" class="chat"><li class="muted">Pick a moment to read what chat said.</li></ul>
    </div>`,
};

export function mountParts() {
  document.querySelectorAll("[data-part]").forEach((el) => {
    const make = PARTS[el.dataset.part];
    if (make) el.outerHTML = make(el);
  });
}

// ---- Player: YouTube embed, controlled with postMessage (enablejsapi=1) ----
// The embed also reports its state (infoDelivery): currentTime, duration, playerState.
export function createPlayer(el, { onInfo } = {}) {
  let frame = null;
  const info = { currentTime: 0, duration: 0, playerState: -1 };
  window.addEventListener("message", (e) => {
    if (!frame || e.source !== frame.contentWindow || typeof e.data !== "string") return;
    let d;
    try { d = JSON.parse(e.data); } catch { return; }
    info.at = info.at || Date.now();  // it answered: no need to keep asking
    if ((d.event === "infoDelivery" || d.event === "initialDelivery") && d.info) {
      Object.assign(info, d.info);
      info.at = Date.now();
      if (onInfo) onInfo(info);
    }
  });
  const send = (func, args = []) => frame && frame.contentWindow.postMessage(JSON.stringify({ event: "command", func, args }), "*");
  return {
    info,
    show(id, title = "Stream", { autoplay = false } = {}) {
      // autoplay only works muted (browser rule); the live page uses it so the player reports its time at once.
      el.innerHTML = `<iframe id="yt" src="https://www.youtube-nocookie.com/embed/${id}?enablejsapi=1&rel=0${autoplay ? "&autoplay=1&mute=1" : ""}&origin=${encodeURIComponent(location.origin)}"
        allow="autoplay; encrypted-media; picture-in-picture" allowfullscreen title="${esc(title)}"></iframe>`;
      frame = el.querySelector("iframe");
      // Ask the player to report its state (currentTime, duration) to this page. It only listens once
      // its own script is ready, so ask every half second until it answers (at most 30 s).
      const me = frame;
      info.at = 0;
      let tries = 0;
      const ask = setInterval(() => {
        if (frame !== me || info.at || ++tries > 60) { clearInterval(ask); return; }
        me.contentWindow.postMessage(JSON.stringify({ event: "listening", id: 1, channel: "widget" }), "*");
      }, 500);
    },
    clear(text) { frame = null; el.innerHTML = `<div class="placeholder">${esc(text)}</div>`; },
    seek(sec) { send("seekTo", [Math.max(0, sec), true]); send("playVideo"); },
    send,
  };
}

// ---- The spike view: chart + moments + why card + chat of a moment ----
// opts.range()        -> [from, to] seconds shown on the chart
// opts.seek(t)        -> jump the player to stream time t
// opts.aiMode         -> "full" (captions + exact cut) or "chat" (live: chat only)
// opts.onSelect(s)    -> called when a moment is picked
const MAX_BARS = 480; // the chart groups windows so it never draws more bars than this
const TYPE_ICON = { funny: "😂", shock: "😱", skill_win: "🔥", fail: "💔", rage: "😡", payoff: "🎉",
  wholesome: "🥹", drama: "🍿", announcement: "📣", technical_issue: "🛠", other: "❔" };

export function createSpikeView(opts) {
  const v = {
    vid: null, rules: { window: 10 }, windows: [], moments: [], sortBy: "score", selected: null,
    why: {}, fresh: new Set(),   // fresh = moments that arrived live and haven't been opened yet
  };
  const ytAt = (sec) => `https://www.youtube.com/watch?v=${v.vid}&t=${Math.max(0, Math.floor(sec))}s`;

  v.reset = () => {
    v.windows = []; v.moments = []; v.selected = null; v.why = {}; v.fresh = new Set();
    $("whyCard").hidden = true;
    $("moments").innerHTML = '<li class="muted">Nothing yet.</li>';
    $("momentCount").textContent = "";
    $("chat").innerHTML = '<li class="muted">Pick a moment to read what chat said.</li>';
    $("chatCount").textContent = "";
    $("chatTitle").textContent = "Chat in the selected moment";
    $("chartEmpty").hidden = false;
    v.drawChart();
  };

  v.addWindows = (rows) => {
    if (!rows.length) return;
    v.windows.push(...rows);
    $("chartEmpty").hidden = true;
  };

  v.normalLevel = () => {
    if (!v.windows.length) return null;
    const scores = v.windows.map((w) => w[1]).sort((a, b) => a - b);
    return scores[Math.floor(scores.length / 2)];
  };

  v.top = () => v.moments.reduce((a, b) => (!a || b.score > a.score ? b : a), null);

  // ---- Moments ----
  const sorted = () => v.moments.slice().sort((a, b) => (v.sortBy === "score" ? b.score - a.score : b.ratio - a.ratio));

  v.renderMoments = () => {
    const list = sorted();
    $("momentCount").textContent = list.length ? `(${list.length})` : "";
    if (!list.length) { $("moments").innerHTML = `<li class="muted">${esc(opts.noMoments || "No spikes yet.")}</li>`; return; }
    $("moments").innerHTML = list.map((s, i) => `
      <li class="moment${v.selected === s.peak ? " sel" : ""}${v.fresh.has(s.peak) ? " fresh" : ""}" data-i="${i}">
        <div class="row"><span class="rank">#${i + 1}</span><span class="time">${clock(s.peak)}</span>
          ${v.fresh.has(s.peak) ? '<span class="newtag">NEW</span>' : ""}${badge(s)}
          <span class="ratio">${Math.round(s.score)} msgs · ${s.ratio}× · ${s.people} people</span></div>
        <div class="clip">Clip ${clock(s.clip_start)} → ${clock(s.clip_end)} (${Math.round(s.clip_end - s.clip_start)} s)
          · <a href="${ytAt(s.clip_start)}" target="_blank" rel="noopener">YouTube ↗</a></div>
        <div class="said">${esc(s.samples.slice(0, 3).join(" · "))}</div>
      </li>`).join("");
  };

  $("moments").addEventListener("click", (e) => {
    if (e.target.closest("a")) return;
    const li = e.target.closest(".moment");
    if (li) v.selectMoment(sorted()[+li.dataset.i], true);
  });
  document.querySelectorAll("[data-sort]").forEach((b) => b.addEventListener("click", () => {
    v.sortBy = b.dataset.sort;
    document.querySelectorAll("[data-sort]").forEach((x) => x.classList.toggle("on", x === b));
    v.renderMoments();
  }));

  v.selectMoment = async (s, play) => {
    if (!s) return;
    v.selected = s.peak;
    v.fresh.delete(s.peak);
    v.renderMoments();
    v.drawChart();
    if (play) opts.seek(s.clip_start);
    renderWhy(s);
    if (!v.why[s.peak] || !v.why[s.peak].evidence) loadWhy(s, false);
    if (opts.onSelect) opts.onSelect(s);
    $("chatTitle").textContent = `Chat at ${clock(s.clip_start)} → ${clock(s.clip_end)}`;
    $("chat").innerHTML = '<li class="muted">Loading…</li>';
    try {
      const rows = await (await fetch(`/api/chat?v=${v.vid}&from=${s.clip_start}&to=${s.clip_end}`)).json();
      $("chatCount").textContent = `${rows.length.toLocaleString()} messages`;
      $("chat").innerHTML = rows.map((m) => `
        <li class="${m.counted ? "" : "skip"}${m.t >= s.peak && m.t < s.peak + v.rules.window ? " peak" : ""}">
          <button class="ts" data-t="${m.t}">${clock(m.t)}</button><span class="who">${esc(m.author)}</span>${esc(m.text)}</li>`).join("")
        || '<li class="muted">No messages.</li>';
    } catch {
      $("chat").innerHTML = '<li class="muted">Couldn\'t load the chat.</li>';
    }
  };

  $("chat").addEventListener("click", (e) => {
    const b = e.target.closest(".ts");
    if (b) opts.seek(+b.dataset.t - 3);
  });

  // ---- Why? Free chat evidence + Gemini ----
  function badge(s) {
    const w = v.why[s.peak];
    if (w && w.loading) return '<span class="vbadge wait" title="Gemini is checking this moment">…</span>';
    if (w && w.ai) return `<span class="vbadge s${Math.min(3, Math.floor(w.ai.viral_score / 4))}" title="Viral score from Gemini">${w.ai.viral_score}/10</span>`;
    return "";
  }

  const whyQuery = (s, ai) => `/api/why?v=${v.vid}&peak=${s.peak}&clip_start=${s.clip_start}&clip_end=${s.clip_end}` +
    `&score=${s.score}&ratio=${s.ratio}&ai=${ai ? 1 : 0}&mode=${opts.aiMode || "full"}`;

  async function loadWhy(s, ai) {
    const cur = v.why[s.peak] || {};
    v.why[s.peak] = { ...cur, loading: ai, ai_error: null };
    v.renderMoments(); if (v.selected === s.peak) renderWhy(s);
    try {
      const r = await (await fetch(whyQuery(s, ai))).json();
      v.why[s.peak] = { ...r, loading: false };
    } catch {
      v.why[s.peak] = { ...cur, loading: false, ai_error: "Couldn't reach the app." };
    }
    v.renderMoments(); if (v.selected === s.peak) renderWhy(s);
  }

  function renderWhy(s) {
    const w = v.why[s.peak];
    $("whyCard").hidden = false;
    $("whyTitle").textContent = `Why did chat explode at ${clock(s.peak)}?`;
    $("whyModel").textContent = w && w.ai ? `Gemini · ${w.ai.model}${w.ai.chat_only ? " · chat only" : ""}` : "";
    if (!w || !w.evidence) {
      $("whyEvidence").innerHTML = '<p class="muted">Reading the chat…</p>'; $("whyAi").innerHTML = ""; return;
    }
    if (w.error) { $("whyEvidence").innerHTML = `<p class="err small">${esc(w.error)}</p>`; return; }
    const e = w.evidence;
    $("whyEvidence").innerHTML = `
      <div class="ev-row"><span class="ev-label">Reaction</span>${e.types.map((t) => `<span class="chip">${esc(t.label)} ${t.share}%</span>`).join("") || '<span class="muted small">unclear</span>'}</div>
      <div class="ev-row"><span class="ev-label">Chat kept saying</span>${e.keywords.map((k) => `<span class="chip kw" title="${k.count} people · ${k.times_more}× more than usual">${esc(k.text)}</span>`).join("") || '<span class="muted small">nothing stands out</span>'}</div>
      ${e.top_message ? `<div class="ev-row"><span class="ev-label">Most repeated</span><span class="quote">“${esc(e.top_message.text)}” ×${e.top_message.count}</span></div>` : ""}`;

    const box = $("whyAi");
    const chatMode = opts.aiMode === "chat";
    const askHtml = (label, note) => `${w.ai_error ? `<p class="err small">${esc(w.ai_error)}</p>` : ""}
      <button class="askbtn" id="askGemini">${label}</button><p class="muted small">${note}</p>`;
    const FULL_NOTE = "Sends only text to Gemini (free tier): what the streamer says 1 minute before and after, plus the chat of this spike. No video.";
    const CHAT_NOTE = "Live: YouTube makes captions only after the stream is processed, so this sends <b>chat only</b> " +
      "(no exact cut). After the stream ends, open it in <b>Finished stream</b> mode for the full check with captions and the exact cut.";
    if (w.ai) {
      const a = w.ai;
      const cut = a.chat_only ? "" : `
        <div class="cut">
          <div class="cut-head"><span class="cut-label">Clip it here</span>
            <button class="playcut" id="playBest">▶ ${clockMs(a.best_start)} → ${clockMs(a.best_end)}</button>
            <span class="muted small">${(a.best_end - a.best_start).toFixed(1)} s · <a href="${ytAt(a.best_start)}" target="_blank" rel="noopener">YouTube ↗</a></span></div>
          <div class="cut-row"><span class="ev-label">Start when they say</span><span class="quote">“${esc(a.start_words)}” <span class="muted small">(line ${a.start_line})</span></span></div>
          <div class="cut-row"><span class="ev-label">End after</span><span class="quote">“${esc(a.end_words)}” <span class="muted small">(line ${a.end_line})</span></span></div>
          <p class="small muted cut-why">${esc(a.cut_reason)}${a.words_found ? "" : " · exact words not found in the captions, times are from the whole line"}</p>
        </div>`;
      box.innerHTML = `
        <div class="ai-head"><span class="vscore s${Math.min(3, Math.floor(a.viral_score / 4))}">${a.viral_score}<small>/10</small></span>
          <div><div class="ai-title">${esc(a.title)}</div>
          <div class="muted small">${TYPE_ICON[a.moment_type] || ""} ${esc(a.moment_type.replace("_", " "))} · confidence ${esc(a.confidence)} · hook: “${esc(a.hook)}”</div></div></div>
        ${cut}
        <dl class="ai-body">
          <dt>What happened</dt><dd>${esc(a.what_happened)}</dd>
          <dt>Why chat reacted</dt><dd>${esc(a.why_chat_reacted)}</dd>
          <dt>Why it could go viral</dt><dd>${esc(a.why_viral)}</dd>
        </dl>
        ${a.chat_only ? (chatMode
          ? `<p class="muted small">Gemini only saw chat (${a.chat_sent} messages): no captions exist yet. After the stream ends, the Finished stream page can run the full check with the exact cut.</p>`
          : askHtml("Full check with captions and exact cut", `This answer was made live from chat only. ${FULL_NOTE}`)) : ""}
        ${a.transcript ? `<details class="tx"><summary>Transcript sent to Gemini (${a.transcript.length} lines, ${a.chat_sent} chat messages)</summary>
          <ol>${a.transcript.map((l) => `<li class="${l.n >= Math.min(a.start_line, a.end_line) && l.n <= Math.max(a.start_line, a.end_line) ? "in" : ""}">
            <button class="ts" data-t="${l.start}">${clock(l.start)}</button><span class="muted small">L${l.n}</span> ${esc(l.text)}</li>`).join("")}</ol>
        </details>` : ""}`;
      if ($("playBest")) $("playBest").onclick = () => opts.seek(a.best_start);
      const tx = box.querySelector(".tx ol");
      if (tx) tx.onclick = (ev) => { const b = ev.target.closest(".ts"); if (b) opts.seek(+b.dataset.t); };
    } else if (w.loading) {
      box.innerHTML = `<p class="ai-wait"><span class="spin"></span>Gemini is reading ${chatMode ? "what chat wrote" : "what was said and what chat wrote"}… (usually under a minute, up to a few minutes when the free tier is busy)</p>`;
    } else if (!w.has_key) {
      box.innerHTML = '<p class="muted small">Add <code>GEMINI_API_KEY=…</code> to the project\'s <code>.env</code> file to get Gemini\'s explanation (free key: aistudio.google.com → Get API key).</p>';
    } else {
      box.innerHTML = chatMode ? askHtml("Check with AI (chat only)", CHAT_NOTE) : askHtml("Check with AI and find the exact clip", FULL_NOTE);
    }
    const ask = $("askGemini");
    if (ask) ask.onclick = () => loadWhy(s, true);
  }

  // ---- Chart: bars grouped so it stays readable ----
  let buckets = [];
  const PAD_L = 34;
  v.drawChart = () => {
    const svg = $("svg");
    const W = svg.clientWidth || 600, H = svg.clientHeight || 240;
    const padB = 22, padT = 22;
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
    if (!v.windows.length) { svg.innerHTML = ""; buckets = []; return; }

    const step = v.rules.window || 10;
    const [t0, t1] = opts.range();
    const total = Math.max(step, t1 - t0);
    const nBars = Math.max(1, Math.min(MAX_BARS, Math.ceil(total / step)));
    const span = total / nBars;
    buckets = Array.from({ length: nBars }, (_, i) => ({ t0: t0 + i * span, t1: t0 + (i + 1) * span, score: 0, msgs: 0, spike: false, has: false }));
    for (const w of v.windows) {
      if (w[0] < t0 || w[0] >= t0 + total) continue;
      const b = buckets[Math.min(nBars - 1, Math.floor((w[0] - t0) / span))];
      b.score = Math.max(b.score, w[1]); b.msgs += w[2]; b.spike ||= !!w[5]; b.has = true;
    }
    const top = Math.max(10, ...buckets.map((b) => b.score)) * 1.08;
    const slot = (W - PAD_L) / nBars;
    const gap = slot > 4 ? 1 : 0;
    const bw = Math.max(1, slot - gap);
    const x = (t) => PAD_L + ((t - t0) / total) * (W - PAD_L);
    const y = (val) => padT + (1 - val / top) * (H - padT - padB);
    const inRange = (t) => t >= t0 && t <= t0 + total;

    let out = "";
    const tick = top > 300 ? 100 : top > 120 ? 50 : top > 60 ? 20 : 10;
    for (let val = 0; val <= top; val += tick) {
      out += `<line class="grid-line" x1="${PAD_L}" x2="${W}" y1="${y(val)}" y2="${y(val)}"/>`;
      out += `<text class="axis" x="${PAD_L - 6}" y="${y(val) + 4}" text-anchor="end">${val}</text>`;
    }
    buckets.forEach((b, i) => {
      if (!b.has || b.score <= 0) return;
      const x0 = PAD_L + i * slot, y0 = y(b.score), h = y(0) - y0, r = Math.min(3, bw / 2, h);
      out += `<path class="bar${b.spike ? " spike" : ""}" d="M${x0},${y(0)}V${y0 + r}Q${x0},${y0} ${x0 + r},${y0}H${x0 + bw - r}Q${x0 + bw},${y0} ${x0 + bw},${y0 + r}V${y(0)}Z"/>`;
    });
    // Numbered markers for the 5 biggest moments (when they are on screen)
    v.moments.slice().sort((a, b) => b.score - a.score).slice(0, 5).forEach((s, i) => {
      if (!inRange(s.peak)) return;
      const cx = x(s.peak + step / 2);
      out += `<g class="mark${v.selected === s.peak ? " sel" : ""}"><circle cx="${cx}" cy="${padT - 10}" r="8"/>
        <text x="${cx}" y="${padT - 6}" text-anchor="middle">${i + 1}</text></g>`;
    });
    if (v.selected !== null && inRange(v.selected)) {
      out += `<line class="cursor" x1="${x(v.selected + step / 2)}" x2="${x(v.selected + step / 2)}" y1="${padT}" y2="${y(0)}"/>`;
    }
    if (opts.extraMarks) out += opts.extraMarks({ x, y, t0, t1: t0 + total, padT, H, padB });
    for (let i = 0; i <= 6; i++) {
      const t = t0 + (total * i) / 6;
      out += `<text class="axis" x="${Math.min(Math.max(x(t), PAD_L + 20), W - 24)}" y="${H - 5}" text-anchor="middle">${clock(t)}</text>`;
    }
    out += `<rect class="hit" x="${PAD_L}" y="0" width="${W - PAD_L}" height="${H - padB}"/>`;
    svg.innerHTML = out;
  };

  function bucketAt(e) {
    const r = $("svg").getBoundingClientRect();
    const frac = (e.clientX - r.left - PAD_L) / (r.width - PAD_L);
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
  $("svg").addEventListener("click", (e) => { const b = bucketAt(e); if (b) opts.seek(b.t0); });
  window.addEventListener("resize", () => v.drawChart());
  return v;
}
