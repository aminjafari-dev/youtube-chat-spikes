// The app window: browser-style tabs, one stream per tab. Each tab is the normal live / finished page in a frame.
// - "+" opens a new tab (the start page: pick live or finished, or a saved stream).
// - Tabs are remembered (this browser only) and come back after a reload. A tab that has not been looked at
//   since then is loaded the first time you click it, like a browser restoring tabs.
// - Only the tab on screen keeps its live feed open (live.js); background live watches keep running on the
//   app, and this window checks them every few seconds to show a red dot and a count of new spikes.
import { esc, store } from "./common.js";

const KEY = "chatSpikes.tabs.v1";
const $ = (id) => document.getElementById(id);
const tabsEl = $("tabs"), framesEl = $("frames");

let tabs = [];          // {id, url, title, vid, kind, seen}
let activeId = null;
let watches = new Map(); // video id -> /api/live/list summary
let nextId = 1;

const kindOf = (url) => (url.startsWith("live.html") ? "live" : url.startsWith("finished.html") ? "finished" : "start");
const vidOf = (url) => new URLSearchParams(url.split("?")[1] || "").get("v");
const byId = (id) => tabs.find((t) => t.id === id);
const frameOf = (t) => framesEl.querySelector(`iframe[data-tab="${t.id}"]`);

function save() {
  store.set(KEY, JSON.stringify({ active: tabs.findIndex((t) => t.id === activeId), tabs: tabs.map(({ url, title, seen }) => ({ url, title, seen })) }));
}

function makeTab(url = "index.html", title = "", seen = 0) {
  return { id: nextId++, url, title, seen, vid: vidOf(url), kind: kindOf(url) };
}

// ---- Tab strip ----
function label(t) {
  const w = t.vid && watches.get(t.vid);
  if (t.kind === "start") return { name: "New tab", tip: "New tab" };
  if (!t.vid) return t.kind === "live" ? { name: "Live stream", tip: "Paste the link of a live stream" }
    : { name: "Finished stream", tip: "Paste the link of a finished stream" };
  const name = (w && w.channel) || t.title || (t.kind === "live" ? "Live stream" : "Finished stream");
  const tip = [w && w.title, t.kind === "live" ? (w && w.running ? "watching live" : "live (not running)") : "finished stream"]
    .filter(Boolean).join(" · ");
  return { name, tip };
}

function newSpikes(t) {
  const w = t.vid && watches.get(t.vid);
  return w && t.id !== activeId ? Math.max(0, (w.moments || 0) - (t.seen || 0)) : 0;
}

function render() {
  tabsEl.innerHTML = tabs.map((t) => {
    const { name, tip } = label(t);
    const w = t.vid && watches.get(t.vid);
    const icon = t.kind === "live" ? `<i class="live-dot${w && w.running ? "" : " off"}"></i>`
      : t.kind === "finished" ? '<span class="tab-ico">▶</span>' : '<span class="tab-ico">✦</span>';
    const n = newSpikes(t);
    return `<div class="tab${t.id === activeId ? " on" : ""}" role="tab" aria-selected="${t.id === activeId}" draggable="true"
        data-id="${t.id}" title="${esc(tip)}">
      ${icon}<span class="tab-name">${esc(name)}</span>
      ${n ? `<span class="tab-badge" title="${n} new spike${n === 1 ? "" : "s"}">${n}</span>` : ""}
      <button type="button" class="tab-x" data-x="${t.id}" aria-label="Close tab" title="Close tab">×</button>
    </div>`;
  }).join("");
  const total = tabs.reduce((a, t) => a + newSpikes(t), 0);
  document.title = total ? `(${total} new) Chat Spikes` : "Chat Spikes";
  $("appDot").classList.toggle("on", [...watches.values()].some((w) => w.running));
}

// ---- Frames ----
function ensureFrame(t) {
  let f = frameOf(t);
  if (f) return f;
  f = document.createElement("iframe");
  f.dataset.tab = t.id;
  f.title = "Stream";
  f.src = t.url;
  f.addEventListener("load", () => { readFrame(t); tell(t); });
  framesEl.appendChild(f);
  return f;
}

// Each page says nothing about tabs; the window reads its address and title (same origin).
function readFrame(t) {
  const f = frameOf(t);
  let loc, title;
  try { loc = f.contentWindow.location; title = f.contentDocument.title; } catch { return; }
  if (!loc || loc.href === "about:blank") return;
  const url = loc.pathname.replace(/^\//, "") + loc.search || "index.html";
  const clean = (title || "").replace(/^\(\d+ new\) /, "").replace(/ · Chat Spikes.*$/, "").replace(/^Chat Spikes$/, "");
  if (url !== t.url || clean !== t.title) {
    const vid = vidOf(url);
    if (vid !== t.vid) t.seen = (watches.get(vid) || {}).moments || 0;
    Object.assign(t, { url, title: clean === "New tab" ? "" : clean, vid, kind: kindOf(url) });
    save();
    render();
  }
}

function tell(t) {
  const f = frameOf(t);
  if (f && f.contentWindow) f.contentWindow.postMessage({ tab: "active", on: t.id === activeId }, location.origin);
}

function activate(id) {
  const t = byId(id);
  if (!t) return;
  activeId = id;
  ensureFrame(t);
  for (const x of tabs) {
    const f = frameOf(x);
    if (f) f.classList.toggle("on", x.id === id);
    tell(x);
  }
  const w = t.vid && watches.get(t.vid);
  if (w) t.seen = w.moments || 0;
  save();
  render();
  tabsEl.querySelector(".tab.on")?.scrollIntoView({ block: "nearest", inline: "nearest" });
}

function newTab(url = "index.html") {
  const t = makeTab(url);
  tabs.push(t);
  activate(t.id);
  return t;
}

function removeTab(id) {
  const i = tabs.findIndex((t) => t.id === id);
  if (i < 0) return;
  frameOf(tabs[i])?.remove();
  tabs.splice(i, 1);
  if (!tabs.length) { newTab(); return; }
  if (activeId === id) activate(tabs[Math.min(i, tabs.length - 1)].id);
  else { save(); render(); }
}

// Closing a tab of a running live watch: ask whether to stop the watch too.
let closing = null;
function closeTab(id) {
  const t = byId(id);
  const w = t && t.kind === "live" && t.vid && watches.get(t.vid);
  const elsewhere = t && tabs.some((x) => x.id !== id && x.vid === t.vid && x.kind === "live");
  if (!w || !w.running || elsewhere) return removeTab(id);
  closing = id;
  $("closeName").textContent = w.channel || w.title || t.vid;
  $("closeAsk").hidden = false;
  $("closeAsk").querySelector("[data-close=keep]").focus();
}
$("closeAsk").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-close]");
  if (!b) return;
  const id = closing, t = byId(id);
  closing = null;
  $("closeAsk").hidden = true;
  if (b.dataset.close === "cancel" || !t) return;
  if (b.dataset.close === "stop") await fetch(`/api/live/stop?v=${t.vid}`);
  removeTab(id);
  poll();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("closeAsk").hidden) { closing = null; $("closeAsk").hidden = true; }
});

// ---- Clicks, middle-click, drag to reorder ----
tabsEl.addEventListener("click", (e) => {
  const x = e.target.closest("[data-x]");
  if (x) { e.stopPropagation(); closeTab(+x.dataset.x); return; }
  const t = e.target.closest(".tab");
  if (t) activate(+t.dataset.id);
});
tabsEl.addEventListener("auxclick", (e) => {
  const t = e.target.closest(".tab");
  if (t && e.button === 1) { e.preventDefault(); closeTab(+t.dataset.id); }
});
let dragId = null;
tabsEl.addEventListener("dragstart", (e) => {
  const t = e.target.closest(".tab");
  if (!t) return;
  dragId = +t.dataset.id;
  e.dataTransfer.effectAllowed = "move";
  t.classList.add("dragging");
});
tabsEl.addEventListener("dragover", (e) => {
  const over = e.target.closest(".tab");
  if (dragId === null || !over) return;
  e.preventDefault();
  const to = +over.dataset.id;
  if (to === dragId) return;
  const from = tabs.findIndex((t) => t.id === dragId);
  const [moved] = tabs.splice(from, 1);
  const r = over.getBoundingClientRect();
  let i = tabs.findIndex((t) => t.id === to);
  if (e.clientX > r.left + r.width / 2) i += 1;
  tabs.splice(i, 0, moved);
  render();
  tabsEl.querySelector(`.tab[data-id="${dragId}"]`)?.classList.add("dragging");
});
tabsEl.addEventListener("dragend", () => { dragId = null; save(); render(); });
$("newTab").addEventListener("click", () => newTab());

// A page in a tab asks to open a stream (start page links): show the tab that already has it, if any.
window.addEventListener("message", (e) => {
  if (e.origin !== location.origin || !e.data || e.data.tab !== "open") return;
  const src = tabs.find((t) => frameOf(t)?.contentWindow === e.source);
  const url = String(e.data.url || "");
  const vid = vidOf(url), kind = kindOf(url);
  const open = vid && tabs.find((t) => t !== src && t.vid === vid && t.kind === kind);
  if (open) {
    activate(open.id);
    if (src && src.kind === "start") removeTab(src.id);
  } else if (src) {
    frameOf(src).src = url;
  } else newTab(url);
});

// ---- Live watches: red dots and new-spike counts for the tabs ----
async function poll() {
  try {
    const list = await (await fetch("/api/live/list")).json();
    watches = new Map(list.map((w) => [w.video_id, w]));
  } catch { return; }
  const a = byId(activeId);
  const w = a && a.vid && watches.get(a.vid);
  if (w) a.seen = w.moments || 0;
  for (const t of tabs) if (frameOf(t)) readFrame(t);
  render();
}

// ---- Start: the saved tabs, or a tab per running live watch ----
let saved = null;
try { saved = JSON.parse(store.get(KEY) || "null"); } catch {}
await poll();
if (saved && saved.tabs && saved.tabs.length) {
  tabs = saved.tabs.map((s) => makeTab(s.url, s.title, s.seen));
  activate((tabs[saved.active] || tabs[0]).id);
} else {
  const running = [...watches.values()].filter((w) => w.running);
  tabs = running.map((w) => makeTab(`live.html?v=${w.video_id}`, w.channel, w.moments));
  if (!tabs.length) tabs.push(makeTab());
  activate(tabs[0].id);
}
setInterval(poll, 4000);
setInterval(() => { const t = byId(activeId); if (t) readFrame(t); }, 1000);
