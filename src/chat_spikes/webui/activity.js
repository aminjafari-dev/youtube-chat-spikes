// Activity panel: every job the app runs, as a flow of steps (done / running / next), plus every request.
(() => {
  const $ = (id) => document.getElementById(id);
  // Inside a tab of the app window (app.html): the window has the one Activity panel, so this page has none.
  // (Each panel keeps a connection open, and a browser only allows 6 to one app.)
  if (window.top !== window) {
    document.body.classList.add("embed");
    $("actToggle")?.remove();
    $("activity")?.remove();
    return;
  }
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const runs = new Map();     // id -> run
  let requests = [];
  let selRun = null, selStep = null, follow = true, tab = "runs", showFiles = false;
  let dirty = false;

  const KIND = { analyze: { icon: "📈", name: "Analyze stream" }, evidence: { icon: "💬", name: "Chat evidence" },
    ai: { icon: "🤖", name: "AI check" }, live: { icon: "🔴", name: "Live watch" }, clip: { icon: "🎬", name: "Reel" } };
  const STATUS = { waiting: "waiting", running: "running", done: "done", error: "failed", skipped: "skipped", cancelled: "cancelled", queued: "queued" };
  const ICON = { waiting: "○", running: "", done: "✓", error: "✕", skipped: "–", cancelled: "■", queued: "…" };

  const clockTime = (t) => new Date(t * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  const dur = (a, b) => {
    if (!a) return "";
    const s = Math.max(0, (b || Date.now() / 1000) - a);
    return s < 10 ? `${s.toFixed(1)} s` : s < 60 ? `${Math.round(s)} s` : `${Math.floor(s / 60)} m ${String(Math.round(s % 60)).padStart(2, "0")} s`;
  };
  const ago = (t) => {
    const s = Date.now() / 1000 - t;
    return s < 60 ? "just now" : s < 3600 ? `${Math.floor(s / 60)} min ago` : s < 86400 ? `${Math.floor(s / 3600)} h ago` : new Date(t * 1000).toLocaleDateString();
  };
  const sorted = () => [...runs.values()].sort((a, b) => b.created - a.created);
  const running = () => sorted().filter((r) => r.status === "running" || r.status === "queued");

  // ---- The panel's markup (shared by every page: they only have an empty <section id="activity">) ----
  const panel = $("activity"), toggle = $("actToggle");
  panel.innerHTML = `
    <div class="act-side">
      <div class="act-sidehead">
        <div class="seg act-tabs" role="group" aria-label="Show">
          <button type="button" data-tab="runs" class="on">Jobs</button>
          <button type="button" data-tab="requests">Requests</button>
        </div>
        <span class="muted small" id="actLive"></span>
      </div>
      <ol id="actList" class="act-list"></ol>
      <div class="act-opts small muted">
        <label><input type="checkbox" id="actFollow" checked> Follow what's running</label>
        <label><input type="checkbox" id="actFiles"> Show page files in requests</label>
      </div>
    </div>
    <div class="act-main">
      <div class="act-head" id="actHead"></div>
      <div class="flow" id="actFlow"></div>
      <div class="act-detail" id="actDetail"></div>
    </div>`;

  // ---- Open / close (remembered per browser) ----
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch {} } };
  function setOpen(open) {
    panel.hidden = !open;
    toggle.classList.toggle("on", open);
    toggle.setAttribute("aria-expanded", open);
    store.set("activityOpen", open ? "1" : "0");
    if (open) render();
  }
  toggle.addEventListener("click", () => setOpen(panel.hidden));
  $("actFollow").addEventListener("change", (e) => { follow = e.target.checked; if (follow) pickLive(); render(); });
  $("actFiles").addEventListener("change", (e) => { showFiles = e.target.checked; render(); });
  panel.querySelectorAll(".act-tabs button").forEach((b) => b.addEventListener("click", () => {
    tab = b.dataset.tab;
    panel.querySelectorAll(".act-tabs button").forEach((x) => x.classList.toggle("on", x === b));
    render();
  }));

  // ---- Live feed ----
  function connect() {
    const es = new EventSource("/api/activity");
    es.onmessage = (e) => {
      const { type, data } = JSON.parse(e.data);
      if (type === "snapshot") {
        runs.clear();
        data.runs.forEach((r) => runs.set(r.id, r));
        requests = data.requests;
        if (!selRun || !runs.has(selRun)) pickLive(true);
      } else if (type === "run") {
        const isNew = !runs.has(data.id);
        runs.set(data.id, data);
        if (follow && (isNew || data.status === "running")) { if (selRun !== data.id) selStep = null; selRun = data.id; }
      } else if (type === "request") {
        requests.push(data);
        if (requests.length > 300) requests.shift();
      }
      dirty = true;
    };
    es.onerror = () => { es.close(); $("actLive").textContent = "reconnecting…"; setTimeout(connect, 2000); };
    es.onopen = () => { $("actLive").textContent = ""; };
  }

  function pickLive(any = false) {
    const live = running()[0] || (any || follow ? sorted()[0] : null);
    if (live) { if (selRun !== live.id) selStep = null; selRun = live.id; }
  }

  // ---- Render ----
  function render() {
    dirty = false;
    const n = running().length;
    $("actCount").textContent = n ? n : "";
    $("actCount").hidden = !n;
    toggle.classList.toggle("busy", n > 0);
    if (panel.hidden) return;
    renderList();
    renderRun();
  }

  function stepDots(r) {
    return r.steps.map((s) => `<i class="d ${s.status}" title="${esc(s.label)}: ${STATUS[s.status]}"></i>`).join("");
  }

  function renderList() {
    const list = $("actList");
    if (tab === "requests") {
      const rows = requests.filter((q) => showFiles || q.path.startsWith("/api/")).slice().reverse();
      list.innerHTML = rows.map((q) => {
        const path = decodeURIComponent(q.path);
        const api = path.split("?")[0];
        return `<li class="req${q.status >= 400 ? " bad" : ""}">
          <div class="row"><span class="code c${String(q.status)[0]}">${q.status || "…"}</span><b>${esc(api)}</b><span class="muted small">${q.ms.toLocaleString()} ms</span></div>
          <div class="muted small ell" title="${esc(path)}">${clockTime(q.t)} · ${esc(path.slice(api.length + 1) || "")}</div></li>`;
      }).join("") || '<li class="muted small pad">No requests yet.</li>';
      return;
    }
    const all = sorted();
    list.innerHTML = all.map((r) => `
      <li class="run ${r.status}${r.id === selRun ? " sel" : ""}" data-id="${r.id}">
        <div class="row"><span class="kind">${KIND[r.kind]?.icon || "•"}</span><span class="rt ell">${esc(r.title)}</span>
          <span class="pill ${r.status}">${STATUS[r.status]}</span></div>
        <div class="row small muted"><span class="dots">${stepDots(r)}</span><span>${ago(r.created)} · ${dur(r.created, r.ended)}</span></div>
        ${r.result ? `<div class="small ell res">${esc(r.result)}</div>` : ""}
      </li>`).join("") || '<li class="muted small pad">Nothing has run yet. Analyze or watch a stream to see it here.</li>';
  }

  $("actList").addEventListener("click", (e) => {
    const li = e.target.closest(".run");
    if (!li) return;
    selRun = li.dataset.id; selStep = null;
    if (running()[0] && running()[0].id !== selRun) { follow = false; $("actFollow").checked = false; }
    render();
  });

  function renderRun() {
    const r = runs.get(selRun);
    if (!r) {
      $("actHead").innerHTML = '<h2>Nothing selected</h2>';
      $("actFlow").innerHTML = '<p class="muted pad">Every job the app does shows up here as a chain of steps: what is done, what is running now and what comes next.</p>';
      $("actDetail").innerHTML = "";
      return;
    }
    const t = r.target || {};
    const where = [t.title && t.title !== r.title ? t.title : "", t.video ? `video ${t.video}` : "",
      t.clip ? `clip ${hms(t.clip[0])} → ${hms(t.clip[1])}` : ""].filter(Boolean).join(" · ");
    $("actHead").innerHTML = `
      <div><h2>${KIND[r.kind]?.icon || ""} ${esc(r.title)} <span class="pill ${r.status}">${STATUS[r.status]}</span></h2>
      <div class="muted small">${esc(where)}${where ? " · " : ""}started ${clockTime(r.created)} · took ${dur(r.created, r.ended)}${r.result ? ` · <b>${esc(r.result)}</b>` : ""}</div></div>`;

    const nextKey = (r.status === "running" || r.status === "queued") ? (r.steps.find((s) => s.status === "waiting") || {}).key : null;
    const current = r.steps.find((s) => s.status === "running");
    const show = selStep && r.steps.some((s) => s.key === selStep) ? selStep : (current || r.steps.find((s) => s.status === "error") || r.steps[r.steps.length - 1]).key;

    $("actFlow").innerHTML = r.steps.map((s, i) => `
      <button type="button" class="node ${s.status}${s.key === show ? " sel" : ""}${s.key === nextKey ? " next" : ""}" data-key="${s.key}">
        <span class="nhead"><span class="num">${i + 1}</span><span class="st ${s.status}">${s.status === "running" ? '<span class="spin"></span>' : ICON[s.status]}</span></span>
        <span class="nlabel">${esc(s.label)}</span>
        <span class="nstate">${s.key === nextKey ? "up next" : STATUS[s.status]}${s.started ? ` · ${dur(s.started, s.ended)}` : ""}</span>
        ${s.progress != null && s.status === "running" ? `<span class="nbar"><i style="width:${Math.round(s.progress * 100)}%"></i></span>` : ""}
        <span class="ntext">${esc(s.text)}</span>
      </button>`).join('<span class="link"></span>');

    const s = r.steps.find((x) => x.key === show);
    const data = Object.entries(s.data || {});
    $("actDetail").innerHTML = `
      <div class="dhead"><b>${esc(s.label)}</b> <span class="pill ${s.status}">${STATUS[s.status]}</span>
        ${s.started ? `<span class="muted small">${clockTime(s.started)}${s.ended ? ` → ${clockTime(s.ended)}` : " → now"} · ${dur(s.started, s.ended)}</span>` : ""}</div>
      <p class="about">${esc(s.about)}</p>
      ${s.text ? `<p class="now"><span class="muted small">${s.status === "running" ? "Now" : "Result"}</span> ${esc(s.text)}</p>` : ""}
      ${data.length ? `<dl class="kv">${data.map(([k, v]) => `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("")}</dl>` : ""}
      ${s.logs && s.logs.length ? `<ol class="log">${s.logs.map(([t, m]) => `<li><span class="muted">${clockTime(t)}</span> ${esc(m)}</li>`).join("")}</ol>` : ""}`;
  }

  function hms(sec) {
    const s = Math.max(0, Math.floor(sec));
    return `${Math.floor(s / 3600)}:${String(Math.floor(s % 3600 / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;
  }

  $("actFlow").addEventListener("click", (e) => {
    const b = e.target.closest(".node");
    if (b) { selStep = b.dataset.key; renderRun(); }
  });

  // Redraw on changes, and every second while something runs (live durations).
  setInterval(() => { if (dirty || running().length) render(); }, 500);
  // Other parts of the page (e.g. the reel card) can open one job here.
  window.openActivityRun = (id) => {
    if (!runs.has(id)) return;
    selRun = id; selStep = null; follow = false; $("actFollow").checked = false; tab = "runs";
    panel.querySelectorAll(".act-tabs button").forEach((x) => x.classList.toggle("on", x.dataset.tab === "runs"));
    setOpen(true);
    panel.scrollIntoView({ behavior: "smooth", block: "start" });
  };
  setOpen(store.get("activityOpen") === "1");
  connect();
})();
