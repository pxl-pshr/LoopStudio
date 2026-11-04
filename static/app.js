// Loop Studio front end: plain JS, talks to server.py.
const $ = (sel, el = document) => el.querySelector(sel);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const MIN_PER_GIF = { stock: 15.0, fast: 4.5, pika: 2.5 };
const ASPECT = { vertical: "9:16", wide: "16:9" };  // render formats besides the default square

const S = {
  subjects: [], name: null, data: null, tab: "review", filter: "all", search: "",
  concept: null, draft: null, checked: new Set(), runs: [], openRuns: new Set(), job: {}, seen: {},
  agent: "the agent",  // from config.json, via /api/state
};

async function api(method, url, body, isForm) {
  const opt = { method, headers: {} };
  if (body !== undefined) {
    if (isForm) opt.body = body;
    else { opt.body = JSON.stringify(body); opt.headers["Content-Type"] = "application/json"; }
  }
  const r = await fetch(url, opt);
  if (!r.ok) throw new Error(await r.text() || r.statusText);
  return r.json();
}

function toast(msg, err) {
  const t = document.createElement("div");
  t.className = "toast" + (err ? " err" : "");
  t.textContent = msg;
  $("#toasts").append(t);
  setTimeout(() => t.remove(), err ? 7000 : 3500);
}

const fail = e => toast(e.message || String(e), true);

// ---------- sidebar ----------

async function loadSubjects() {
  S.subjects = await api("GET", "/api/subjects");
  // renders that landed in a subject since it was last open, and which subject the GPU is on now
  for (const s of S.subjects) if (s.name === S.name || !(s.name in S.seen)) S.seen[s.name] = s.renders;
  const rendering = new Set(S.runs.filter(r => r.status === "running").map(r => r.subject));
  $("#subjects").innerHTML = S.subjects.map(s => `
    <button class="subj ${s.name === S.name ? "active" : ""}" data-name="${esc(s.name)}">
      ${s.has_reference ? `<img src="/ref/${esc(s.name)}" alt="">` : `<span class="noimg">◎</span>`}
      <span><div class="t">${esc(s.name)}${rendering.has(s.name) ? `<span class="live-dot" title="rendering now"></span>` : ""}</div>
        <div class="s">${s.error ? `<span class="bad" title="${esc(s.error)}">can't read its files</span>` : `${s.renders} renders`}${s.renders > S.seen[s.name] ? ` · <b class="new">+${s.renders - S.seen[s.name]} new</b>` : ""}</div></span>
    </button>`).join("");
  $("#subjects").querySelectorAll(".subj").forEach(b => b.onclick = () => openSubject(b.dataset.name).catch(fail));
}

async function openSubject(name, keepTab) {
  // nothing changes until it has loaded: if it can't, the subject on screen stays the one that edits save to
  S.opening = name;
  const data = await api("GET", `/api/subjects/${name}`);
  if (S.opening !== name) return;  // another subject was opened while this one loaded
  if (name !== S.name) { S.concept = null; S.draft = null; S.checked.clear(); S.hover = null; }
  S.name = name;
  if (!keepTab && !S.tab) S.tab = "review";
  localStorage.setItem("ms-subject", name);
  S.data = data;
  S.seen[name] = S.data.renders.length;
  loadSubjects().catch(fail);
  renderMain();
}

// false when another subject was opened meanwhile (its data stays)
async function reloadData() {
  const name = S.name;
  if (!name) return false;
  const data = await api("GET", `/api/subjects/${name}`);
  if (S.name !== name) return false;
  S.data = data;
  return true;
}

// ---------- main ----------

function renderMain() {
  const { subject, concepts, renders } = S.data;
  const tags = (subject.base_tags || []).map(t => `<span class="chip">${esc(t)}</span>`).join("");
  $("#main").innerHTML = `
    <div class="subj-head">
      ${subject.reference ? `<img src="/ref/${esc(S.name)}" alt="">` : `<div class="noimg">◎</div>`}
      <div style="min-width:0;flex:1">
        <h1>${esc(S.name)}</h1>
        <div class="desc" title="${esc(subject.subject)}">${esc(subject.subject || "no description")}</div>
        <div class="chips">${tags}</div>
      </div>
      <button class="btn ghost small" id="open-subject" title="${esc(S.data.folder)}">Open folder</button>
      <button class="btn ghost small" id="edit-subject">Edit</button>
    </div>
    <div class="tabs">
      <button class="tab" data-tab="review">Review<span class="n">${renders.length}</span></button>
      <button class="tab" data-tab="concepts">Concepts<span class="n">${concepts.length}</span></button>
      <button class="tab" data-tab="runs">Runs<span class="n" id="runs-n"></span></button>
    </div>
    <div class="pane" id="pane"></div>`;
  $("#edit-subject").onclick = editSubject;
  $("#open-subject").onclick = () => api("POST", `/api/subjects/${S.name}/show`, {}).catch(fail);
  document.querySelectorAll(".tab").forEach(b => b.onclick = () => { S.tab = b.dataset.tab; renderTab(); });
  renderTab();
}

function renderTab() {
  document.querySelectorAll(".tab").forEach(b => b.classList.toggle("active", b.dataset.tab === S.tab));
  if (S.tab === "review") renderReview();
  else if (S.tab === "concepts") renderConcepts();
  else renderRuns();
}

// tell the server what's open, so the agent in the terminal knows what "this one" means (studio.py state)
let stateTimer;
function pushState() {
  clearTimeout(stateTimer);
  stateTimer = setTimeout(() => api("POST", "/api/ui-state", {
    subject: S.name, tab: S.tab, concept: S.concept === "__new" ? null : S.concept, selected: [...S.checked], filter: S.filter,
  }).catch(() => {}), 300);
}

// the subject's files changed on disk (a run, a mark, or the agent editing them from the terminal)
async function subjectChanged() {
  const before = JSON.stringify(S.data.subject);
  if (!(await reloadData())) return;
  if (JSON.stringify(S.data.subject) !== before) return renderMain();
  const n = $(".tab[data-tab=review] .n"); if (n) n.textContent = S.data.renders.length;
  const c = $(".tab[data-tab=concepts] .n"); if (c) c.textContent = S.data.concepts.length;
  if (S.tab === "review") fillGrid();
  else if (S.tab === "concepts") {
    if (!draftDirty()) { S.draft = null; return renderConcepts(); }
    // the user is mid-edit: keep their edits, but say the file moved on under them
    const saved = S.data.concepts.find(c => c.name === S.concept);
    if (S.concept !== "__new" && JSON.stringify(saved) !== JSON.stringify(S.base) && !S.warned) {
      S.warned = true;
      toast(saved ? `${S.concept} changed on disk; Save or Render will replace that with your edits` : `${S.concept} was deleted on disk; Save puts it back`, true);
    }
  }
}

// edited since it was opened (S.base is the concept as it was then)
function draftDirty() {
  if (!S.draft) return false;
  if (S.concept === "__new") return true;
  return JSON.stringify(S.draft) !== JSON.stringify(S.base);
}

// ---------- review ----------

function mark(stem) { return S.data.results[stem] || {}; }

function conceptOf(stem) {
  // what stands before the format, so wink rendered wide isn't taken for a concept called wink_wide
  const m = stem.match(/^(.+?)_(?:square|vertical|wide)_\d+f(?:_|$)/);
  if (m) return m[1];
  return S.data.concepts.map(c => c.name).filter(n => stem.startsWith(n + "_")).sort((a, b) => b.length - a.length)[0] || stem;
}

function visible(r) {
  const m = mark(r.stem);
  if (S.search && !r.stem.includes(S.search.toLowerCase())) return false;
  return S.filter === "all" || (S.filter === "todo" && !m.v) || (S.filter === "win" && m.v === "win")
    || (S.filter === "lose" && m.v === "lose");
}

const filterCount = f => S.data.renders.filter(r => { const s = S.filter; S.filter = f; const v = visible(r); S.filter = s; return v; }).length;

function renderReview() {
  const rs = S.data.renders, n = filterCount;
  $("#pane").innerHTML = `
    <div class="toolbar">
      <div class="seg" id="filters">
        ${[["all", "All"], ["todo", "To review"], ["win", "Wins"], ["lose", "Losses"]]
          .map(([f, l]) => `<button data-f="${f}" class="${S.filter === f ? "active" : ""}">${l} <span class="muted">${n(f)}</span></button>`).join("")}
      </div>
      <input type="search" id="search" placeholder="Filter by name" value="${esc(S.search)}">
      <span class="spacer"></span>
      <button class="btn ghost small" id="refresh" title="Rebuild from ComfyUI history, and try again to make any GIF that failed">Refresh</button>
      <button class="btn ghost small danger" id="recycle" title="Send every Loss's files to the Recycle Bin">Recycle losses</button>
    </div>
    <div class="grid" id="grid"></div>
    <div class="empty" id="no-renders" style="padding:60px" ${rs.length ? "hidden" : ""}>No renders yet. Write a concept and render it.</div>`;
  $("#filters").querySelectorAll("button").forEach(b => b.onclick = () => { S.filter = b.dataset.f; renderReview(); });
  $("#search").oninput = e => { S.search = e.target.value; fillGrid(); };
  $("#refresh").onclick = refresh;
  $("#recycle").onclick = recycleLosses;
  fillGrid();
  pushState();
}

function fillGrid() {
  const grid = $("#grid");
  if (!grid) return;
  const existing = new Map([...grid.children].map(el => [el.dataset.stem, el]));
  const rows = S.data.renders.filter(visible);
  grid.replaceChildren(...rows.map(r => {
    const el = existing.get(r.stem) && existing.get(r.stem).dataset.media === mediaKey(r) ? existing.get(r.stem) : cardEl(r);
    updateCard(el, r);
    return el;
  }));
  document.querySelectorAll("#filters button").forEach(b => $(".muted", b).textContent = filterCount(b.dataset.f));
  const none = $("#no-renders"); if (none) none.hidden = S.data.renders.length > 0;
}

const mediaKey = r => r.gif || r.webp || r.mp4 || r.status || "";

function cardEl(r) {
  const el = document.createElement("div");
  el.className = "card";
  el.dataset.stem = r.stem;
  el.dataset.media = mediaKey(r);
  const media = r.gif || r.webp ? `<img src="/media/${esc(S.name)}/${esc(r.gif || r.webp)}" loading="lazy" alt="">`
    : r.mp4 ? `<video src="/media/${esc(S.name)}/${esc(r.mp4)}" autoplay loop muted playsinline></video>`
    : `<div class="wait">${esc(r.status || "queued")}…</div>`;
  const seed = (r.stem.match(/_s(\d+)_/) || [, r.seed])[1];
  const fast = r.mode ? r.mode.startsWith("fast") : /_\d+f_fast(_|$)/.test(r.stem);
  el.innerHTML = `
    <div class="media">${media}
      <div class="badges">
        ${r.caption ? `<span class="badge cap">${esc(r.caption)}</span>` : ""}
        ${r.example ? `<span class="badge example" title="Bundled with Loop Studio, not rendered here">EXAMPLE</span>` : ""}
        ${fast ? `<span class="badge fast">FAST</span>` : ""}
        ${r.width && r.height && r.width !== r.height ? `<span class="badge fast">${r.width > r.height ? "16:9" : "9:16"}</span>` : ""}
      </div>
    </div>
    <div class="card-body">
      <div class="card-title"><b title="${esc(r.stem)}">${esc(conceptOf(r.stem))}</b><span>seed ${esc(seed ?? "")}${r.date ? ` · <span title="${esc(new Date(r.date * 1000).toLocaleString())}">${esc(shortDate(r.date))}</span>` : ""}${r.minutes ? ` · ${esc(r.minutes)}m` : ""}${r.gif_mb ? ` · ${esc(r.gif_mb)}MB` : ""}</span></div>
      <div class="marks"><button class="mark win">✓ Win</button><button class="mark lose">✕ Lose</button></div>
      <input class="note" placeholder="Note">
      <details><summary>tags & prompt</summary>
        <input class="tags-in" title="Tags, comma separated" style="margin-top:6px">
        <pre>${esc(r.prompt || "")}</pre>
        ${r.example ? "" : `<button class="btn ghost small show-file" title="Open its folder in Explorer, with the file selected">Show in folder</button>`}
      </details>
    </div>`;
  const subject = S.name;
  const save = patch => saveMark(r, patch);
  $(".media", el).onclick = e => { if (!e.target.closest("a")) openViewer(r.stem); };
  $(".mark.win", el).onclick = () => toggleMark(r, "win");
  $(".mark.lose", el).onclick = () => toggleMark(r, "lose");
  $(".note", el).onchange = e => save({ note: e.target.value });
  $(".tags-in", el).onchange = e => save({ tags: e.target.value });
  const show = $(".show-file", el);
  if (show) show.onclick = () => api("POST", `/api/subjects/${subject}/show`, { file: r.gif || r.webp || r.mp4 }).catch(fail);
  el.onmouseenter = () => (S.hover = el);
  el.onmouseleave = () => { if (S.hover === el) S.hover = null; };
  return el;
}

function saveMark(r, patch) {
  const subject = S.name;
  return api("POST", `/api/subjects/${subject}/marks`, { stem: r.stem, ...patch })
    .then(res => {
      if (S.name !== subject) return;
      if (Object.keys(res).length) S.data.results[r.stem] = res; else delete S.data.results[r.stem];
      const el = cardOf(r.stem);
      if (el) updateCard(el, r);
      if (viewing()) viewerMarks();
      document.querySelectorAll("#filters button").forEach(b => $(".muted", b).textContent = filterCount(b.dataset.f));
    }).catch(fail);
}
const toggleMark = (r, v) => saveMark(r, { v: mark(r.stem).v === v ? "" : v });
const cardOf = stem => [...($("#grid")?.children || [])].find(el => el.dataset.stem === stem);

// "Oct 26", with the year when it isn't this one
function shortDate(t) {
  const d = new Date(t * 1000);
  return d.toLocaleDateString([], { month: "short", day: "numeric", ...(d.getFullYear() !== new Date().getFullYear() && { year: "numeric" }) });
}

function updateCard(el, r) {
  const m = mark(r.stem);
  el.classList.toggle("win", m.v === "win");
  el.classList.toggle("lose", m.v === "lose");
  $(".mark.win", el).classList.toggle("on", m.v === "win");
  $(".mark.lose", el).classList.toggle("on", m.v === "lose");
  if (document.activeElement !== $(".note", el)) $(".note", el).value = m.note || "";
  if (document.activeElement !== $(".tags-in", el)) $(".tags-in", el).value = m.tags || (r.tags || []).join(", ");
}

// W / L / U on the card under the mouse
document.addEventListener("keydown", e => {
  if (S.tab !== "review" || !S.hover || !S.hover.isConnected || !$("#modal").hidden || viewing() || e.target.matches("input, textarea, button, select")
    || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase();
  if (k === "w") $(".mark.win", S.hover).click();
  if (k === "l") $(".mark.lose", S.hover).click();
});

// ---------- viewer: click a card's GIF to see it large; <- -> step through, W / L mark, M switches to the MP4 master ----------
const V = { stems: [], i: 0, mp4: false };
const viewing = () => !$("#viewer").hidden;
const viewed = () => S.data && S.data.renders.find(r => r.stem === V.stems[V.i]);

function openViewer(stem) {
  V.stems = S.data.renders.filter(visible).map(r => r.stem);  // the grid's order, as filtered when opened
  V.i = Math.max(0, V.stems.indexOf(stem));
  $("#viewer").hidden = false;
  showViewer();
}
function closeViewer() { $("#viewer").hidden = true; $("#viewer").innerHTML = ""; }
function stepViewer(d) { const i = V.i + d; if (i >= 0 && i < V.stems.length) { V.i = i; showViewer(); } }

function showViewer() {
  const r = viewed();
  if (!r) return closeViewer();
  const src = f => `/media/${esc(S.name)}/${esc(f)}`;
  const still = r.gif || r.webp, mp4 = r.mp4 && (V.mp4 || !still);
  const seed = (r.stem.match(/_s(\d+)_/) || [, r.seed])[1];
  $("#viewer").innerHTML = `
    <button class="viewer-nav" data-d="-1" title="Previous (←)" ${V.i ? "" : "disabled"}>‹</button>
    <figure>
      ${mp4 ? `<video src="${src(r.mp4)}" autoplay loop muted playsinline></video>`
        : still ? `<img src="${src(still)}" alt="">` : `<div class="wait">${esc(r.status || "queued")}…</div>`}
      <figcaption>
        <b>${esc(conceptOf(r.stem))}</b>
        <span class="muted small">seed ${esc(seed ?? "")} · <span class="v-res"></span>${V.i + 1} / ${V.stems.length}</span>
        <span class="spacer"></span>
        ${still && r.mp4 ? `<div class="seg" title="M"><button data-m="" class="${mp4 ? "" : "active"}">${r.gif ? "GIF" : "WebP"}</button><button data-m="1" class="${mp4 ? "active" : ""}">MP4 master</button></div>` : ""}
        <button class="mark win" title="W">✓ Win</button><button class="mark lose" title="L">✕ Lose</button>
        <button class="icon-btn" data-close title="Close (Esc)">✕</button>
      </figcaption>
    </figure>
    <button class="viewer-nav" data-d="1" title="Next (→)" ${V.i < V.stems.length - 1 ? "" : "disabled"}>›</button>`;
  const v = $("#viewer");
  v.querySelectorAll("[data-d]").forEach(b => b.onclick = () => stepViewer(+b.dataset.d));
  v.querySelectorAll("[data-m]").forEach(b => b.onclick = () => { V.mp4 = !!b.dataset.m; showViewer(); });
  $(".mark.win", v).onclick = () => toggleMark(r, "win");
  $(".mark.lose", v).onclick = () => toggleMark(r, "lose");
  $("[data-close]", v).onclick = closeViewer;
  const media = $("figure img, figure video", v), res = $(".v-res", v);
  if (media) media[mp4 ? "onloadedmetadata" : "onload"] = () => {
    const w = media.naturalWidth || media.videoWidth, h = media.naturalHeight || media.videoHeight;
    res.textContent = `${w}×${h}${media.clientWidth < w ? ` (shown at ${media.clientWidth})` : ""} · `;
  };
  viewerMarks();
}

function viewerMarks() {
  const r = viewed(), v = $("#viewer");
  if (!r) return;
  const m = mark(r.stem);
  $(".mark.win", v).classList.toggle("on", m.v === "win");
  $(".mark.lose", v).classList.toggle("on", m.v === "lose");
}

$("#viewer").onclick = e => { if (e.target.id === "viewer") closeViewer(); };
document.addEventListener("keydown", e => {
  if (!viewing() || e.target.matches("input, textarea, select") || e.ctrlKey || e.metaKey || e.altKey) return;
  const k = e.key.toLowerCase(), r = viewed();
  if (k === "escape") closeViewer();
  else if (k === "arrowleft") stepViewer(-1);
  else if (k === "arrowright") stepViewer(1);
  else if (k === "w" && r) toggleMark(r, "win");
  else if (k === "l" && r) toggleMark(r, "lose");
  else if (k === "m" && r && r.mp4 && (r.gif || r.webp)) { V.mp4 = !V.mp4; showViewer(); }
  else return;
  e.preventDefault();
});

const lastLine = s => s.trim().split("\n").pop();

async function refresh() {
  const b = $("#refresh"); b.disabled = true; b.textContent = "Refreshing…";
  try {
    const r = await api("POST", `/api/subjects/${S.name}/refresh`);
    if (await reloadData()) renderReview();
    const stuck = r.output.split("\n").find(l => l.startsWith("couldn't finish"));
    if (!r.ok) toast("The review failed: " + lastLine(r.output), true);
    else if (stuck) toast(stuck, true);
    else toast("Review rebuilt");
  } catch (e) { fail(e); } finally { if (b.isConnected) { b.disabled = false; b.textContent = "Refresh"; } }
}

async function recycleLosses() {
  const examples = new Set(S.data.renders.filter(r => r.example).map(r => r.stem));
  const n = Object.entries(S.data.results).filter(([stem, r]) => r.v === "lose" && !examples.has(stem)).length;
  if (!n) return toast("No losses to recycle; bundled examples stay");
  if (!confirm(`Send the files of ${n} losses to the Recycle Bin?`)) return;
  try {
    const r = await api("POST", `/api/subjects/${S.name}/recycle-losers`);
    if (r.ok) toast(r.output.trim().split("\n")[0] || "Done"); else toast("Recycling failed: " + lastLine(r.output), true);
    if (await reloadData()) renderReview();
  }
  catch (e) { fail(e); }
}

// ---------- concepts ----------

function renderConcepts() {
  const cs = S.data.concepts;
  for (const n of [...S.checked]) if (!cs.some(c => c.name === n)) S.checked.delete(n);  // gone from the file meanwhile
  if (S.concept && !S.draft) {
    S.draft = structuredClone(cs.find(c => c.name === S.concept) || { name: "", template: "studio" });
    S.base = structuredClone(S.draft); S.warned = false;
  }
  const keep = { redo: $("#redo")?.checked, seeds: $("#seeds")?.value, scroll: $(".clist") && $("#pane").scrollTop };
  const count = n => S.data.renders.filter(r => conceptOf(r.stem) === n).length;
  $("#pane").innerHTML = `
    <div class="split">
      <div class="clist">
        <div class="toolbar"><button class="btn small" id="new-concept">+ New concept</button><span class="spacer"></span>
          <span class="muted small">${S.checked.size ? `${S.checked.size} selected` : ""}</span></div>
        ${cs.map(c => `
          <div class="citem ${c.name === S.concept ? "active" : ""}" data-name="${esc(c.name)}">
            <input type="checkbox" ${S.checked.has(c.name) ? "checked" : ""} title="Select for rendering">
            <div class="nm"><b>${esc(c.name)}</b><span>${esc(c.template || "custom")}${c.caption ? ` · “${esc(c.caption)}”` : ""} · ${count(c.name)} renders</span></div>
          </div>`).join("")}
      </div>
      <div>
        <div class="editor" id="editor">${S.draft ? editorHtml() : `<div class="empty" style="padding:60px 0">Pick a concept, or make a new one.</div>`}</div>
      </div>
    </div>
    <div class="renderbar">
      <span id="render-what" class="muted"></span><span class="spacer"></span>
      <label class="muted small">Seeds</label><input type="text" id="seeds" value="${esc(keep.seeds ?? (localStorage.getItem("ms-seeds-v2") || ""))}" placeholder="new" title="Comma separated; empty: a seed each concept hasn't had yet">
      <select id="format" title="Aspect of the render: 992x992, 704x1280 or 1280x704">${[["square", "1:1"], ["vertical", "9:16"], ["wide", "16:9"]]
        .map(([f, l]) => `<option value="${f}" ${(localStorage.getItem("ms-format") || "square") === f ? "selected" : ""}>${l}</option>`).join("")}</select>
      <label class="toggle" ${S.data.subject.model ? `title="This subject's subject.json sets &quot;model&quot;: &quot;${esc(S.data.subject.model)}&quot;, which decides the render mode"` : ""}><input type="checkbox" id="fast" ${(S.data.subject.model ? S.data.subject.model === "fast" : localStorage.getItem("ms-fast") === "1") ? "checked" : ""} ${S.data.subject.model ? "disabled" : ""}> FastH3</label>
      <label class="toggle" title="Render again even if this concept and seed already exist"><input type="checkbox" id="redo" ${keep.redo ? "checked" : ""}> Redo</label>
      <button class="btn primary" id="render">Render</button>
    </div>`;
  $("#new-concept").onclick = () => { S.concept = "__new"; S.draft = { name: "", caption: null, template: "studio", action: "", sound: "", tags: [] }; renderConcepts(); };
  document.querySelectorAll(".citem").forEach(el => {
    $("input", el).onclick = e => { e.stopPropagation(); e.target.checked ? S.checked.add(el.dataset.name) : S.checked.delete(el.dataset.name); renderConcepts(); };
    el.onclick = () => { S.concept = el.dataset.name; S.draft = null; renderConcepts(); };
  });
  if (keep.scroll) $("#pane").scrollTop = keep.scroll;
  if (S.draft) wireEditor();
  ["seeds", "fast", "format", "redo"].forEach(id => $("#" + id).onchange = updateRenderWhat);
  $("#render").onclick = startRender;
  updateRenderWhat();
  pushState();
}

function editorHtml() {
  const d = S.draft, t = d.visual !== undefined ? "custom" : d.template || "studio";
  const ta = (k, label, hint, rows = 3) => `<div class="field"><label>${label}</label><textarea data-k="${k}" rows="${rows}">${esc(d[k] || "")}</textarea>${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  return `
    <div class="row">
      <div class="field"><label>Name</label><input data-k="name" value="${esc(d.name)}" placeholder="thumbs_up"></div>
      <div class="field"><label>Caption</label><input data-k="caption" value="${esc(d.caption || "")}" placeholder="none, or e.g. YES!"></div>
    </div>
    <div class="field"><label>Template</label>
      <div class="seg" id="tpl">${[["studio", "Studio"], ["scene", "Scene"], ["custom", "Custom"]].map(([v, l]) => `<button data-t="${v}" class="${t === v ? "active" : ""}">${l}</button>`).join("")}</div>
      <div class="hint">${{ studio: "White studio, face centred, chest-up. For gestures and expressions.", scene: "A real place. Write the setting with {Subject} in it.", custom: "Your own full visual prompt. {subject}, {Subject} and {keep} are filled in." }[t]}</div>
    </div>
    ${t === "studio" ? ta("action", "Action", "One continuous or cyclic action, big and cartoony. Hold any pose “from the very first frame”.", 4)
      + `<div class="field"><label>Framing</label><input data-k="framing" value="${esc(d.framing || "")}" placeholder="from the chest up"></div>` : ""}
    ${t === "scene" ? ta("setting", "Setting", "e.g. A cozy living room. {Subject} sits on a sofa with…", 3) + ta("action", "Action", "", 3) : ""}
    ${t === "custom" ? ta("visual", "Visual", "", 8) : ""}
    ${ta("sound", "Sound", "Silent in the GIF, but loud sound cues make motion bigger.", 2)}
    <div class="field"><label>Tags</label><input data-k="tags" value="${esc((d.tags || []).join(", "))}" placeholder="thumbs up, approve, nice"></div>
    <div style="display:flex;gap:8px">
      <button class="btn primary" id="save">Save</button>
      <button class="btn" id="preview">Preview prompt</button>
      <span class="spacer"></span>
      ${S.concept !== "__new" ? `<button class="btn ghost danger" id="del">Delete</button>` : ""}
    </div>
    <pre class="prompt-preview" id="prompt" hidden></pre>`;
}

function wireEditor() {
  const ed = $("#editor");
  ed.querySelectorAll("[data-k]").forEach(i => i.oninput = () => {
    const k = i.dataset.k;
    S.draft[k] = k === "tags" ? i.value.split(",").map(s => s.trim()).filter(Boolean) : k === "caption" ? (i.value.trim() || null) : i.value;
  });
  $("#tpl").querySelectorAll("button").forEach(b => b.onclick = () => {
    const d = S.draft;
    if (b.dataset.t === "custom") { if (d.visual === undefined) d.visual = ""; delete d.template; }
    else { delete d.visual; d.template = b.dataset.t; }
    $("#editor").innerHTML = editorHtml(); wireEditor();
  });
  $("#save").onclick = saveConcept;
  $("#preview").onclick = async () => {
    try {
      const r = await api("POST", `/api/subjects/${S.name}/preview`, clean(S.draft)), p = $("#prompt");
      p.hidden = !r.ok;
      if (r.ok) p.textContent = r.prompt; else toast(lastLine(r.prompt), true);
    }
    catch (e) { fail(e); }
  };
  if ($("#del")) $("#del").onclick = async () => {
    if (!confirm(`Delete the concept ${S.concept}? Its renders stay.`)) return;
    try { await persist(S.data.concepts.filter(c => c.name !== S.concept)); } catch (e) { return fail(e); }
    S.checked.delete(S.concept); S.concept = null; S.draft = null; renderConcepts();
  };
}

// the draft as it's saved: fields the editor doesn't show (upscale, pixel, vhs, reference, ...) stay, in their order
function clean(d) {
  const c = structuredClone(d);
  c.name = (d.name || "").trim(); c.caption = d.caption || null;
  if (d.visual !== undefined) for (const k of ["template", "setting", "action", "framing"]) delete c[k];
  else { delete c.visual; for (const k of ["setting", "action", "framing"]) if (!d[k]) delete c[k]; }
  c.sound = d.sound || ""; c.tags = d.tags || [];
  return c;
}

async function persist(list) {
  await api("PUT", `/api/subjects/${S.name}/concepts`, list);
  S.data.concepts = list;
  $(".tab[data-tab=concepts] .n").textContent = list.length;
}

async function saveConcept() {
  const c = clean(S.draft);
  if (!/^[a-z0-9_]+$/.test(c.name)) { toast("Name: lowercase letters, digits and _ only", true); return false; }
  const list = S.data.concepts.slice();
  const i = list.findIndex(x => x.name === (S.concept === "__new" ? c.name : S.concept));
  if ((S.concept === "__new" && i >= 0) || (S.concept !== "__new" && c.name !== S.concept && list.some(x => x.name === c.name))) {
    toast(`A concept called ${c.name} exists`, true); return false;
  }
  if (i >= 0) list[i] = c; else list.push(c);
  try { await persist(list); if (S.checked.delete(S.concept)) S.checked.add(c.name); S.concept = c.name; S.draft = null; toast("Saved"); renderConcepts(); return true; } catch (e) { fail(e); return false; }
}

function renderTargets() {
  return S.checked.size ? [...S.checked] : S.concept && S.concept !== "__new" ? [S.concept] : [];
}

function updateRenderWhat() {
  const t = renderTargets(), seeds = parseSeeds(), fast = $("#fast").checked, mode = S.data.subject.model || (fast ? "fast" : "stock");
  localStorage.setItem("ms-seeds-v2", $("#seeds").value); localStorage.setItem("ms-format", $("#format").value);
  if (!$("#fast").disabled) localStorage.setItem("ms-fast", fast ? "1" : "0");
  // no seeds typed: one new seed per concept
  const n = t.length * Math.max(seeds.length, 1), min = n * (MIN_PER_GIF[mode] ?? MIN_PER_GIF.stock);
  $("#render-what").innerHTML = t.length ? `<b>${n}</b> GIF${n === 1 ? "" : "s"} · ${t.length === 1 ? esc(t[0]) : `${t.length} concepts`}${seeds.length ? "" : " · new seed"} · ≈ ${min < 60 ? Math.round(min) + " min" : (min / 60).toFixed(1) + " h"}` : "Select concepts to render";
  $("#render").disabled = !t.length;
}

const parseSeeds = () => ($("#seeds")?.value || "").split(/[ ,]+/).filter(Boolean).map(Number).filter(n => Number.isInteger(n) && n >= 0);

async function startRender() {
  // the open concept's unsaved edits are what the user means to render
  if (S.concept !== "__new" && draftDirty() && !(await saveConcept())) return;
  try {
    const run = await api("POST", `/api/subjects/${S.name}/render`, { concepts: renderTargets(), seeds: parseSeeds(), fast: $("#fast").checked, format: $("#format").value, redo: $("#redo").checked });
    toast(`Queued ${run.concepts.length * Math.max(run.seeds.length, 1)} render(s)`);
    S.checked.clear(); S.tab = "runs"; S.openRuns.add(run.id); renderTab();
  } catch (e) { fail(e); }
}

// ---------- runs ----------

async function loadRuns() {
  S.runs = await api("GET", "/api/runs");
  const active = S.runs.filter(r => r.status === "running" || r.status === "queued").length;
  if ($("#runs-n")) $("#runs-n").textContent = active || "";
}

async function renderRuns() {
  pushState();
  await loadRuns();
  if (S.tab !== "runs" || !$("#pane")) return;
  const j = S.job;
  $("#pane").innerHTML = `
    <div class="toolbar"><b>Runs</b><span class="muted small">one at a time; ComfyUI is cleared between jobs</span></div>
    <div class="runs">
      <div class="live"><div style="min-width:220px">${j.stage && j.stage !== "idle" ? `<b>${esc(j.label || "rendering")}</b><div class="muted small">${esc(j.stage)}${j.max ? ` · ${j.value}/${j.max}` : ""}</div>` : `<span class="muted">GPU idle</span>`}</div>
        <div class="bar"><div id="live-bar" style="width:${j.max ? (100 * j.value / j.max) : 0}%"></div></div></div>
      ${S.runs.slice().reverse().map(r => `
        <div class="run" data-id="${r.id}">
          <div class="run-head"><span class="state ${r.status}">${r.status}</span>
            <b>${esc(r.subject)}</b><span class="muted">${esc(r.concepts.join(", "))}</span>
            <span class="muted small">${r.seeds.length ? `seeds ${r.seeds.join(", ")}` : "new seed"}${r.fast ? " · FastH3" : ""}${ASPECT[r.format] ? " · " + ASPECT[r.format] : ""}${r.redo ? " · redo" : ""}${runCounts(r)}</span>
            <span class="spacer"></span><span class="muted small">${when(r)}</span>
            ${r.status === "running" || r.status === "queued" ? `<button class="btn ghost small danger" data-stop="${r.id}">Stop</button>` : ""}</div>
          <pre ${S.openRuns.has(r.id) ? "" : "hidden"}>${esc(r.log_tail.join("\n")) || "waiting…"}</pre>
        </div>`).join("") || `<div class="empty" style="padding:40px">No runs yet this session.</div>`}
    </div>`;
  document.querySelectorAll(".run-head").forEach(h => h.onclick = e => {
    if (e.target.dataset.stop) return;
    const id = h.parentElement.dataset.id, pre = $("pre", h.parentElement);
    pre.hidden = !pre.hidden; pre.hidden ? S.openRuns.delete(id) : S.openRuns.add(id);
  });
  document.querySelectorAll("[data-stop]").forEach(b => b.onclick = async () => {
    if (!confirm("Stop this run? The GIF being rendered now is cancelled.")) return;
    try { await api("POST", `/api/runs/${b.dataset.stop}/stop`); renderRuns(); } catch (e) { fail(e); }
  });
}

// what an ended run came to: " · 1 rendered · 2 skipped"
const runCounts = r => ["running", "queued"].includes(r.status) ? ""
  : ["rendered", "skipped", "failed"].filter(k => r[k]).map(k => ` · ${r[k]} ${k}`).join("");

function when(r) {
  const t = r.started || r.created, d = new Date(t * 1000);
  const dur = r.started ? Math.round(((r.ended || Date.now() / 1000) - r.started) / 60) : null;
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) + (dur !== null ? ` · ${dur} min` : "");
}

// ---------- live events ----------

let eventsDropped = false;
function connectEvents() {
  const ws = new WebSocket(`ws://${location.host}/ws/events`);
  // back after the app restarted or the PC slept: reload what the missed events would have changed
  ws.onopen = () => {
    if (!eventsDropped) return;
    S.job = {}; showJob();
    (S.tab === "runs" ? renderRuns() : loadRuns()).then(() => loadSubjects()).catch(() => {});
    if (S.name) subjectChanged().catch(() => {});
    if (S.reqs && document.body.contains(S.reqs.el)) renderReqs(S.reqs.el).catch(() => {});  // the app's downloads ended with it
    pollComfy();
  };
  ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === "progress") { S.job.value = m.value; S.job.max = m.max; showJob(); }
    else if (m.type === "stage") {
      S.job.stage = m.stage;
      if (m.stage !== "sampling") { S.job.value = 0; S.job.max = 0; }
      if (m.stage === "idle") S.job.label = "";
      showJob();
    }
    else if (m.type === "run_log") {
      const q = m.line.match(/^queued (\S+)/);
      const run = S.runs.find(r => r.id === m.id);
      if (q) { S.job.label = (run ? run.subject + " · " : "") + q[1]; showJob(); }
      const pre = document.querySelector(`.run[data-id="${m.id}"] pre`);
      if (pre) { pre.textContent = (pre.textContent === "waiting…" ? "" : pre.textContent + "\n") + m.line; pre.scrollTop = pre.scrollHeight; }
    }
    else if (m.type === "runs") { (S.tab === "runs" ? renderRuns() : loadRuns()).then(() => loadSubjects()).catch(fail); }
    else if ((m.type === "renders_changed" || m.type === "subject_changed") && m.subject === S.name) debounceRefresh();
    else if (m.type === "renders_changed" || m.type === "subject_changed") debounceSubjects();
    else if (m.type === "subjects") loadSubjects();
    else if (m.type === "settings") settingsChanged().catch(fail);
    else if (m.type === "downloads") fetchesChanged().catch(() => {});
    else if (m.type === "comfy") pollComfy();
  };
  ws.onclose = () => { eventsDropped = true; setTimeout(connectEvents, 2000); };
}

let refreshTimer;
function debounceRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(() => { subjectChanged().catch(fail); loadSubjects(); }, 800);
}

// another subject got new renders: only its count in the sidebar changes
let subjectsTimer;
function debounceSubjects() {
  clearTimeout(subjectsTimer);
  subjectsTimer = setTimeout(() => loadSubjects().catch(fail), 800);
}

function showJob() {
  const j = S.job, busy = j.stage && j.stage !== "idle";
  $("#job").innerHTML = busy ? `<b>${esc(j.label || "ComfyUI")}</b> · ${esc(j.stage)}${j.max ? ` ${j.value}/${j.max}` : ""}` : "";
  $("#progress-bar").style.width = busy && j.max ? `${100 * j.value / j.max}%` : busy ? "100%" : "0";
  $("#progress-bar").style.opacity = busy && !j.max ? ".25" : "1";
  const lb = $("#live-bar"); if (lb) lb.style.width = j.max ? `${100 * j.value / j.max}%` : "0";
  if (S.tab === "runs" && $(".live")) $(".live > div").innerHTML = busy ? `<b>${esc(j.label || "rendering")}</b><div class="muted small">${esc(j.stage)}${j.max ? ` · ${j.value}/${j.max}` : ""}</div>` : `<span class="muted">GPU idle</span>`;
}

async function pollComfy() {
  try {
    const c = await api("GET", "/api/comfy");
    const gb = b => (b / 2 ** 30).toFixed(1);
    $("#comfy-status").innerHTML = c.online
      ? `<span class="pill on">ComfyUI ${esc(c.version || "")}</span><span class="pill">${esc((c.gpu || "").replace("NVIDIA GeForce ", ""))} · ${gb(c.vram_total - c.vram_free)}/${gb(c.vram_total)} GB</span>${c.running + c.pending ? `<span class="pill">queue ${c.running + c.pending}</span>` : ""}`
      : `<span class="pill off">ComfyUI offline</span>`;
  } catch { $("#comfy-status").innerHTML = `<span class="pill off">server offline</span>`; }
}

// ---------- terminal ----------

const T = { term: null, fit: null, ws: null, alive: false, queue: [], onboard: false };
const BANNER = $(".splash-art").textContent;  // the page splash's lettering, before that splash is removed

// LOOP STUDIO over the terminal while its agent starts, until the agent prints any visible text
function termSplash(on) {
  const el = $("#term-splash");
  if (on) {
    $(".splash-art", el).textContent = BANNER;
    $("#term-splash-status").textContent = `starting ${S.agent}…`;
    clearTimeout(T.splashTimer);
    T.splashTimer = setTimeout(() => termSplash(false), 90000);
  }
  el.hidden = !on;
}
// printable text left once escape sequences (cursor moves, colours, window titles) are taken out
const visibleText = s => s.replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, "").replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "")
  .replace(/\x1b[()=>][0-9A-Za-z]?/g, "").trim().length > 0;

function openDrawer() {
  $("#drawer").hidden = false;
  if (!T.term) initTerminal(); else { T.fit.fit(); T.term.focus(); }
}

function initTerminal() {
  T.term = new Terminal({ fontFamily: '"Cascadia Code", Consolas, monospace', fontSize: 13, cursorBlink: true, allowProposedApi: true,
    theme: { background: "#0a0a0b", foreground: "#e6e6e8", cursor: "#c8ff4d", selectionBackground: "#3a4a1288" } });
  T.fit = new FitAddon.FitAddon();
  T.term.loadAddon(T.fit);
  T.term.open($("#terminal"));
  T.fit.fit();
  T.term.onData(d => send({ type: "input", data: d }));
  T.term.onResize(({ cols, rows }) => send({ type: "resize", cols, rows }));
  new ResizeObserver(() => { if (!$("#drawer").hidden) T.fit.fit(); }).observe($("#terminal"));
  connectTerm();
  T.term.focus();
}

function connectTerm() {
  T.ws = new WebSocket(`ws://${location.host}/ws/term`);
  T.startSent = false;  // once per connection: an agent that can't start (not on PATH) mustn't be retried in a loop
  T.ws.onopen = () => T.term.reset();  // the server sends its whole buffer on connect
  T.ws.onmessage = e => {
    const m = JSON.parse(e.data);
    if (m.type === "output") { T.term.write(m.data); if (!$("#term-splash").hidden && visibleText(m.data)) termSplash(false); }
    else if (m.type === "state") {
      T.alive = m.alive;
      $("#term-state").textContent = m.alive ? "· running" : "· not started";
      if (!m.alive) {
        if (!T.startSent) { T.startSent = true; termSplash(true); send({ type: "start", cols: T.term.cols, rows: T.term.rows, onboard: T.onboard }); }
        else { T.onboard = false; T.queue = []; }  // the agent couldn't start (the terminal says why); don't type into the next one
      } else if (T.onboard) {
        // the welcome's setup request: on the command line when we just started an agent that takes one (prompted),
        // else typed in: straight away if the agent was already running ("prompted" is only in the reply to a start),
        // or once a just-started one is up
        T.onboard = false;
        if (!m.prompted) { T.queue.push(S.welcome.onboard_prompt); setTimeout(flushQueue, "prompted" in m ? 8000 : 300); }
      } else if (T.queue.length) setTimeout(flushQueue, T.justStarted ? 6000 : 300);
    }
  };
  T.ws.onclose = () => setTimeout(connectTerm, 2000);
}

function send(m) { if (T.ws && T.ws.readyState === 1) T.ws.send(JSON.stringify(m)); }

// each request, then Enter, then the next: sent together they'd merge into one prompt
async function flushQueue() {
  T.justStarted = false;
  const wait = ms => new Promise(r => setTimeout(r, ms));
  while (T.queue.length) { send({ type: "input", data: T.queue.shift() }); await wait(200); send({ type: "input", data: "\r" }); await wait(500); }
}

function sendToAgent(text) {
  T.queue.push(text);
  T.justStarted = !T.alive;
  const opened = !!T.term;
  openDrawer();
  if (T.alive) setTimeout(flushQueue, 300);
  // its session ended: start it again; the queue goes in once it's up
  else if (opened) { T.term.clear(); termSplash(true); send({ type: "start", cols: T.term.cols, rows: T.term.rows }); }
  toast(`Sent to ${S.agent} in the terminal`);
}

$("#term-toggle").onclick = () => $("#drawer").hidden ? openDrawer() : ($("#drawer").hidden = true);
$("#term-close").onclick = () => ($("#drawer").hidden = true);
$("#term-restart").onclick = () => { if (confirm(`Restart the ${S.agent} session?`)) { T.term.clear(); termSplash(true); send({ type: "restart", cols: T.term.cols, rows: T.term.rows }); } };
document.addEventListener("keydown", e => { if (e.ctrlKey && e.key === "`") { e.preventDefault(); $("#term-toggle").click(); } });

// the terminal sits at the bottom, left or right; the side and its size are remembered in this browser
const pref = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { return null; } };
let dockSide = "bottom";

function dock(side) {
  const d = $("#drawer"), size = pref(side === "bottom" ? "ms-dock-h" : "ms-dock-w");
  dockSide = ["left", "right"].includes(side) ? side : "bottom";
  document.body.classList.toggle("dock-left", dockSide === "left");
  document.body.classList.toggle("dock-right", dockSide === "right");
  const px = parseInt(size) || 0;
  d.style.height = dockSide === "bottom" && px ? Math.max(140, Math.min(px, innerHeight - 160)) + "px" : "";
  d.style.width = dockSide !== "bottom" && px ? Math.max(320, Math.min(px, innerWidth - 420)) + "px" : "";
  document.querySelectorAll("#dock [data-dock]").forEach(b => b.classList.toggle("active", b.dataset.dock === dockSide));
  pref("ms-dock", dockSide);
  if (T.fit && !d.hidden) requestAnimationFrame(() => T.fit.fit());
}
document.querySelectorAll("#dock [data-dock]").forEach(b => b.onclick = () => dock(b.dataset.dock));
dock(pref("ms-dock") || "bottom");

// drag the grip to resize: height at the bottom, width on a side
$("#drawer-grip").onmousedown = e => {
  e.preventDefault();
  const d = $("#drawer"), x = e.clientX, y = e.clientY, w = d.offsetWidth, h = d.offsetHeight;
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const move = ev => {
    if (dockSide === "bottom") d.style.height = clamp(h + y - ev.clientY, 140, innerHeight - 160) + "px";
    else d.style.width = clamp(w + (dockSide === "right" ? x - ev.clientX : ev.clientX - x), 320, innerWidth - 420) + "px";
  };
  const up = () => {
    removeEventListener("mousemove", move); removeEventListener("mouseup", up);
    pref(dockSide === "bottom" ? "ms-dock-h" : "ms-dock-w", dockSide === "bottom" ? d.style.height : d.style.width);
    T.fit && T.fit.fit();
  };
  addEventListener("mousemove", move); addEventListener("mouseup", up);
};

// ---------- modals: new / edit subject ----------

// locked: only its own buttons close it (not a click outside or Escape)
function modal(html, { locked = false } = {}) {
  $("#modal-card").innerHTML = html;
  $("#modal").hidden = false;
  $("#modal").dataset.locked = locked ? "1" : "";
  $("#modal-card").querySelectorAll("[data-close]").forEach(b => b.onclick = closeModal);
}
function closeModal() { $("#modal").hidden = true; $("#modal-card").classList.remove("wide"); }
$("#modal").onclick = e => { if (e.target.id === "modal" && !$("#modal").dataset.locked) closeModal(); };
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#modal").hidden && !$("#modal").dataset.locked) closeModal(); });

function subjectFields(s = {}) {
  return `
    <div class="field"><label>Who / what, as the prompt names them</label>
      <textarea name="subject" rows="2" placeholder="the orange tabby cat from <Picture 1>, with a white chest and…">${esc(s.subject || "")}</textarea>
      <div class="hint">Must mention &lt;Picture 1&gt; when there's a reference image.</div></div>
    <div class="field"><label>Signature trait, kept through every GIF</label>
      <input name="keep" value="${esc(s.keep || "")}" placeholder="Through all of it it keeps its wide, unblinking stare."></div>
    <div class="row">
      <div class="field"><label>He / She / It</label><input name="he" value="${esc(s.he || "He")}"></div>
      <div class="field"><label>His / Her / Its</label><input name="his" value="${esc(s.his || "his")}"></div>
    </div>
    <div class="field"><label>Shared tags</label><input name="base_tags" value="${esc((s.base_tags || []).join(", "))}" placeholder="cat, orange cat, reaction"></div>
`;
}

$("#new-subject").onclick = () => {
  modal(`
    <h2>New subject</h2>
    <div class="field"><label>Name</label><input name="name" placeholder="my_cat"></div>
    <div class="field"><label>Reference image</label>
      <label class="drop" id="drop"><span>Drop an image or click to choose (optional)</span><input type="file" name="image" accept="image/*" hidden></label></div>
    ${subjectFields()}
    <div class="actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="create">Create</button></div>`);
  const file = $("input[type=file]", $("#modal-card")), drop = $("#drop");
  const show = f => { drop.innerHTML = `<img src="${URL.createObjectURL(f)}"><span>${esc(f.name)}</span>`; drop.append(file); };
  file.onchange = () => file.files[0] && show(file.files[0]);
  drop.ondragover = e => e.preventDefault();
  drop.ondrop = e => { e.preventDefault(); const dt = new DataTransfer(); dt.items.add(e.dataTransfer.files[0]); file.files = dt.files; show(file.files[0]); };
  $("#create").onclick = async () => {
    const fd = new FormData();
    $("#modal-card").querySelectorAll("[name]").forEach(i => { if (i.type === "file") { if (i.files[0]) fd.append("image", i.files[0]); } else fd.append(i.name, i.value); });
    try { const s = await api("POST", "/api/subjects", fd, true); closeModal(); await loadSubjects(); S.tab = "concepts"; openSubject(s.name); toast(`Created ${s.name}`); }
    catch (e) { fail(e); }
  };
};

function editSubject() {
  modal(`<h2>Edit ${esc(S.name)}</h2>${subjectFields(S.data.subject)}
    <div class="actions"><button class="btn ghost danger left" id="delete-subject">Delete subject…</button>
      <button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="save-subject">Save</button></div>`);
  $("#delete-subject").onclick = deleteSubject;
  $("#save-subject").onclick = async () => {
    const body = {};
    $("#modal-card").querySelectorAll("[name]").forEach(i => body[i.name] = i.name === "base_tags" ? i.value.split(",") : i.value);
    try { await api("PUT", `/api/subjects/${S.name}`, body); closeModal(); await openSubject(S.name, true); toast("Saved"); } catch (e) { fail(e); }
  };
}

function deleteSubject() {
  const { subject, concepts, renders } = S.data;
  const norm = p => (p || "").replace(/[\\/]+/g, "/").toLowerCase();
  const inFolder = norm(S.data.data_dir) === norm(S.data.folder);  // its run data lives in the subject's folder
  const item = (name, text, path) => `<label class="check"><input type="checkbox" ${name ? `name="${name}"` : "disabled"} checked>
    <span>${text}${path ? `<div class="hint">${esc(path)}</div>` : ""}</span></label>`;
  modal(`<h2>Delete ${esc(S.name)}?</h2>
    <p>Everything ticked goes to the Recycle Bin, so you can restore it.</p>
    ${item(null, `The subject and its ${concepts.length} concepts` + (inFolder ? ", with its marks and render history" : ""), S.data.folder)}
    ${item("renders", `Its renders: ${renders.length} MP4s, GIFs and frame folders`, `ComfyUI/output/${subject.output}`)}
    ${inFolder ? "" : item("data", "Its marks and render history", S.data.data_dir)}
    ${subject.reference ? item("reference", "Its reference image", `ComfyUI/input/${subject.reference}`) : ""}
    <div class="actions"><button class="btn ghost" data-close>Cancel</button><button class="btn danger-fill" id="confirm-delete">Send to Recycle Bin</button></div>`);
  $("#confirm-delete").onclick = async () => {
    const body = {}, name = S.name;
    $("#modal-card").querySelectorAll("input[name]").forEach(i => body[i.name] = i.checked);
    try {
      const r = await api("DELETE", `/api/subjects/${name}`, body);
      closeModal();
      S.name = null; S.data = null; S.concept = null; S.draft = null; S.checked.clear();
      localStorage.removeItem("ms-subject");
      await loadSubjects();
      if (S.subjects.length) await openSubject(S.subjects[0].name);
      else { $("#main").innerHTML = `<div class="empty">Pick a subject, or make a new one.</div>`; pushState(); }
      toast(`Deleted ${name}: ${r.recycled.length} item${r.recycled.length === 1 ? "" : "s"} sent to the Recycle Bin`);
    } catch (e) { fail(e); }
  };
}

// ---------- settings (config.json) ----------

// what the terminal drawer runs; env goes to that process only
const AGENTS = {
  claude: { name: "Claude Code", command: "claude", env: {} },
  codex: { name: "Codex", command: "codex", env: {} },
  gemini: { name: "Gemini CLI", command: "gemini", env: {} },
  opencode: { name: "OpenCode", command: "opencode", env: {} },
  "claude-ollama": { name: "Claude Code (Ollama)", command: "claude",
    env: { ANTHROPIC_BASE_URL: "http://localhost:11434", ANTHROPIC_AUTH_TOKEN: "ollama", ANTHROPIC_MODEL: "qwen3-coder" } },
  "aider-ollama": { name: "Aider (Ollama)", command: "aider --model ollama_chat/qwen3-coder --read AGENTS.md", env: { OLLAMA_API_BASE: "http://127.0.0.1:11434" } },
  shell: { name: "PowerShell", command: "powershell", env: {} },
};

// is the folder a ComfyUI, with a Python that has what the render scripts import (Install fixes that); into el, and returned
async function showComfyCheck(el, comfy, noPython) {
  el.textContent = "Checking…";
  try {
    const r = await api("POST", "/api/check-comfy", comfy());
    el.innerHTML = !r.comfy ? `<b class="bad">✗ No ComfyUI here</b> (no models folder in ${esc(r.folder)})`
      : !r.python_ok ? `<b class="bad">✗ ComfyUI found, but not its Python</b>: ${noPython}`
      : `<b class="ok">✓ ComfyUI found</b> · Python: ${esc(r.python)}` + (r.missing.length
        ? ` · <b class="bad">missing ${esc(r.missing.join(", "))}</b> <button class="btn ghost small install-deps">Install</button>`
        : " · packages ✓");
    const b = $(".install-deps", el);
    if (b) b.onclick = async () => {
      b.disabled = true; b.textContent = "Installing…";
      try { const x = await api("POST", "/api/install-deps", { ...comfy(), missing: r.missing }); if (!x.ok) toast(x.output.trim().split("\n").pop(), true); }
      catch (e) { fail(e); }
      showComfyCheck(el, comfy, noPython);
      if (S.reqs && document.body.contains(S.reqs.el)) renderReqs(S.reqs.el).catch(() => {});
    };
    return r;
  } catch (e) { el.textContent = e.message; return null; }
}

// first boot, or whenever the ComfyUI folder isn't one: pick it before anything else (found ones are offered)
function setupComfy(s, then) {
  modal(`
    <h2>Where's your ComfyUI?</h2>
    <p>Loop Studio renders with your own ComfyUI. Pick its folder: the one with <code>models</code> in it, or for the portable
      download, the folder you unzipped.</p>
    <div class="field"><label>ComfyUI folder</label>
      <div class="pick"><input id="setup-dir" value="${esc(s.comfy_dir)}"><button class="btn ghost small" id="setup-browse">Browse…</button></div>
      <div class="hint" id="setup-check"></div></div>
    <div class="field" id="setup-found" hidden><label>Found on this PC</label><div class="found"></div></div>
    <div class="actions"><button class="btn ghost" id="setup-later">Later</button>
      <button class="btn primary" id="setup-save" disabled>Use this folder</button></div>`, { locked: true });
  const input = $("#setup-dir"), save = $("#setup-save");
  let seq = 0, touched = false;
  const check = async () => {
    const mine = ++seq, dir = input.value;
    save.disabled = true;
    const r = await showComfyCheck($("#setup-check"), () => ({ comfy_dir: dir }), "set its Python later in Settings → Advanced");
    if (mine === seq) save.disabled = !(r && r.comfy && r.python_ok && input.value === dir);
  };
  input.onchange = () => { touched = true; check(); };
  check();
  api("GET", "/api/comfy-guess").then(found => {
    found = found.filter(f => f !== s.comfy_dir || !s.comfy_ok);
    if (!found.length) return;
    $("#setup-found").hidden = false;
    $("#setup-found .found").innerHTML = found.map(f => `<button class="btn ghost small" data-dir="${esc(f)}">${esc(f)}</button>`).join("");
    $("#setup-found").querySelectorAll("[data-dir]").forEach(b => b.onclick = () => { touched = true; input.value = b.dataset.dir; check(); });
    if (!s.comfy_ok && !touched && found.length === 1) { input.value = found[0]; check(); }  // the only one: probably it
  }).catch(() => {});
  $("#setup-browse").onclick = async () => {
    try { const r = await api("POST", "/api/pick", { kind: "folder", initial: input.value, title: "ComfyUI folder" }); if (r.path) { touched = true; input.value = r.path; check(); } }
    catch (e) { fail(e); }
  };
  $("#setup-later").onclick = () => { closeModal(); then(); };
  save.onclick = async () => {
    try { await api("POST", "/api/comfy-folder", { comfy_dir: input.value }); closeModal(); toast("ComfyUI folder saved"); then(); }
    catch (e) { fail(e); }
  };
}

async function openSettings() {
  const { settings: c, python_auto, file } = await api("GET", "/api/settings");
  const path = (key, label, hint, kind = "folder", value = c[key]) => `
    <div class="field"><label>${label}</label>
      <div class="pick"><input data-k="${key}" value="${esc(value || "")}"><button class="btn ghost small" data-pick="${key}" data-kind="${kind}">Browse…</button></div>
      ${hint ? `<div class="hint">${hint}</div>` : ""}</div>`;
  const check = (k, label, on) => `<label class="check"><input type="checkbox" data-k="${k}" ${on ? "checked" : ""}><span>${label}</span></label>`;
  const o = c.outputs, env = Object.entries(c.agent.env || {}).map(([k, v]) => `${k}=${v}`).join("\n");
  modal(`
    <h2>Settings</h2>
    <p class="small">Saved to <code>${esc(file)}</code>. Changes apply as soon as you save.</p>
    <h3>Folders</h3>
    ${path("comfy_dir", "ComfyUI folder", `<span id="comfy-check">Checking…</span>`)}
    ${path("subjects_dir", "Subjects folder", "One folder per subject: its subject.json and concepts.json, and its marks and render history.")}
    ${path("data_dir", "Separate run data folder", "Leave empty to keep each subject's marks and render history in its own folder.")}
    <details class="advanced"><summary>Advanced</summary>
      ${path("comfy_python", "ComfyUI Python", "Leave empty: it's found in the ComfyUI folder. Only needed for an unusual install.", "file", python_auto ? "" : c.comfy_python)}
      <div class="row">
        <div class="field"><label>ComfyUI URL</label><input data-k="comfy_url" value="${esc(c.comfy_url)}"></div>
        ${path("font", "Caption font", "", "file")}
      </div>
    </details>
    <h3>Requirements</h3>
    <p class="small">What ComfyUI needs for Loop Studio's renders, checked against the running ComfyUI (every folder it reads models from, extra_model_paths.yaml included). Missing models download straight into a ComfyUI folder; missing nodes install into custom_nodes. <a href="#" id="recheck">Check again</a></p>
    <div id="reqs" class="reqs"><span class="muted small">Checking ComfyUI…</span></div>
    <h3>Outputs</h3>
    <p class="small">Every render keeps a full-size master MP4 (no caption) and its PNG frames. On top of that, at the size below:</p>
    <div class="checks">${check("gif", "GIF", o.gif)}${check("webp", "Animated WebP", o.webp)}${check("mp4", "MP4 (.share.mp4)", o.mp4)}${check("captions", "Draw the caption", o.captions)}</div>
    <div class="row">
      <div class="field"><label>Size (long side)</label><select data-k="size">${[...new Set([320, 480, 640, 720, 992, o.size])].sort((a, b) => a - b).map(n => `<option ${n === o.size ? "selected" : ""}>${n}</option>`).join("")}</select></div>
      <div class="field"><label>Frame rate</label><select data-k="fps"><option value="12" ${o.fps === 12 ? "selected" : ""}>12 fps (51 frames, smaller)</option><option value="24" ${o.fps === 24 ? "selected" : ""}>24 fps (102 frames, smoother)</option></select></div>
    </div>
    <h3>Agent in the terminal</h3>
    <div class="field"><label>Preset</label><select id="agent-preset"><option value="">Pick one to fill in below…</option>
      ${Object.entries(AGENTS).map(([k, a]) => `<option value="${k}">${esc(a.name)}</option>`).join("")}</select>
      <div class="hint">Any terminal agent works: it reads AGENTS.md (Claude Code reads CLAUDE.md) and drives the app with studio.py.</div></div>
    <div class="row">
      <div class="field"><label>Name</label><input data-k="agent_name" value="${esc(c.agent.name)}"></div>
      <div class="field"><label>Command</label><input data-k="agent_command" value="${esc(c.agent.command)}"></div>
    </div>
    <div class="field"><label>Environment</label><textarea data-k="agent_env" rows="3" placeholder="ANTHROPIC_BASE_URL=http://localhost:11434">${esc(env)}</textarea>
      <div class="hint">KEY=value per line, set for the agent only: point it at a local model or another provider.</div></div>
    <div class="actions"><button class="btn ghost" data-close>Cancel</button><button class="btn primary" id="save-settings">Save</button></div>`);
  const card = $("#modal-card");
  card.classList.add("wide");
  const val = k => $(`[data-k="${k}"]`, card);
  const comfy = () => ({ comfy_dir: val("comfy_dir").value, comfy_python: val("comfy_python").value });
  const checkComfy = () => showComfyCheck($("#comfy-check", card), comfy, "pick its python.exe under Advanced");
  val("comfy_dir").onchange = val("comfy_python").onchange = checkComfy;
  checkComfy();
  // nodes, models and version, from the running ComfyUI (needs.py)
  const reqsEl = $("#reqs", card);
  const checkReqs = () => renderReqs(reqsEl).catch(e => (reqsEl.textContent = e.message));
  $("#recheck", card).onclick = e => { e.preventDefault(); checkReqs(); };
  checkReqs();
  card.querySelectorAll("[data-pick]").forEach(b => b.onclick = async () => {
    const input = val(b.dataset.pick);
    b.disabled = true;
    try {
      const r = await api("POST", "/api/pick", { kind: b.dataset.kind, initial: input.value || c[b.dataset.pick], title: input.closest(".field").querySelector("label").textContent });
      if (r.path) { input.value = r.path; input.onchange && input.onchange(); }
    } catch (e) { fail(e); }
    b.disabled = false;
  });
  $("#agent-preset").onchange = e => {
    const a = AGENTS[e.target.value];
    if (!a) return;
    val("agent_name").value = a.name; val("agent_command").value = a.command;
    val("agent_env").value = Object.entries(a.env).map(([k, v]) => `${k}=${v}`).join("\n");
  };
  $("#save-settings").onclick = async () => {
    const env = Object.fromEntries(val("agent_env").value.split("\n").map(l => l.trim()).filter(l => l.includes("="))
      .map(l => [l.slice(0, l.indexOf("=")).trim(), l.slice(l.indexOf("=") + 1).trim()]));
    const body = {
      ...Object.fromEntries(["comfy_dir", "comfy_python", "subjects_dir", "data_dir", "comfy_url", "font"].map(k => [k, val(k).value])),
      outputs: { gif: val("gif").checked, webp: val("webp").checked, mp4: val("mp4").checked, captions: val("captions").checked, size: +val("size").value, fps: +val("fps").value },
      agent: { name: val("agent_name").value, command: val("agent_command").value, env },
    };
    try {
      const r = await api("PUT", "/api/settings", body);
      closeModal();
      toast(r.restart_agent ? "Saved. Press the terminal's Restart to switch to the new agent" : "Saved");
    } catch (e) { fail(e); }
  };
}

$("#settings").onclick = () => openSettings().catch(fail);

// ---------- first run: the agent checks the setup with the user ----------

function welcome() {
  const shell = S.welcome.prompt_flag === false;
  modal(`
    <h2>Welcome to Loop Studio</h2>
    <p>${shell
      ? "The terminal runs a plain shell, not an agent. Settings → Requirements shows what this PC's ComfyUI can render."
      : `${esc(S.agent)} will check this PC: which render modes your ComfyUI can run, what's missing for the others, and about
         how long a GIF takes in each. It asks before rendering anything.`}
      Missing models and nodes download or install from <b>Settings → Requirements</b>, each into the right ComfyUI folder.</p>
    <div class="actions"><button class="btn ghost" id="skip-welcome">${shell ? "Got it" : "Skip"}</button>
      <button class="btn ghost" id="models-welcome">Get the models</button>
      <button class="btn primary" id="start-welcome">${shell ? "Open Settings" : `Check my setup with ${esc(S.agent)}`}</button></div>`);
  const done = () => api("POST", "/api/onboarded").catch(() => {});
  $("#skip-welcome").onclick = () => { done(); closeModal(); };
  $("#models-welcome").onclick = () => { closeModal(); openSettings().then(() => $("#reqs")?.scrollIntoView({ block: "start" })).catch(fail); };
  $("#start-welcome").onclick = () => {
    closeModal();
    if (shell) { done(); openSettings().catch(fail); return; }
    // the agent marks it done (studio.py onboarded) once the user has confirmed a mode
    if (T.alive) { sendToAgent(S.welcome.onboard_prompt); return; }
    T.onboard = true;
    const opened = !!T.term;
    openDrawer();
    // the terminal was opened before and its agent has ended (or never started): start it again, with the request
    if (opened) { T.term.clear(); termSplash(true); send({ type: "start", cols: T.term.cols, rows: T.term.rows, onboard: true }); }
    toast(`Starting ${S.agent} in the terminal`);
  };
}

// ---------- start ----------

// the splash stays at least half a second, so a fast load doesn't just flash it
function hideSplash() {
  const s = $("#splash");
  setTimeout(() => { s.classList.add("gone"); setTimeout(() => s.remove(), 400); }, Math.max(0, 500 - performance.now()));
}

// ---------- setup: Settings > Requirements, with Download / Install / Open folder for what's missing ----------

const GB = n => n >= 1e9 ? (n / 1e9).toFixed(1) + " GB" : Math.round(n / 1e6) + " MB";
const linkify = text => esc(text).replace(/https?:\/\/[^\s)]+/g, u => `<a href="${u}" target="_blank" rel="noopener">${u}</a>`);

const INSTALL_OUT = {};  // what each Install button last said, kept when the list is rebuilt

async function renderReqs(el) {
  // the folders picked on the rows stay picked when the list is rebuilt
  const picks = Object.fromEntries([...el.querySelectorAll(".dl-folder")].map(s => [s.closest(".req").dataset.key, s.value]));
  el.innerHTML = `<span class="muted small">Checking ComfyUI…</span>`;
  const [items, folders, fetches] = await Promise.all([api("GET", "/api/requirements"),
    api("GET", "/api/model-folders"), api("GET", "/api/downloads?scan=1")]);
  S.reqs = { el, items, folders, fetches, picks };
  const core = items.filter(i => !i.ok && i.core && i.get?.kind === "model");
  const optional = items.filter(i => !i.ok && !i.core && i.get?.kind === "model");
  const bulk = (id, list, label) => list.length ? `<button class="btn ${id === "dl-core" ? "primary" : "ghost"} small" id="${id}">${label} · ${GB(list.reduce((a, i) => a + i.get.size, 0))}</button>` : "";
  el.innerHTML = (core.length || optional.length ? `<div class="req-all">
      ${bulk("dl-core", core, `Download the ${core.length} missing for default renders`)}
      ${bulk("dl-opt", optional, `Download the ${optional.length} optional`)}
      <div class="hint">One file at a time, each into the folder picked on its row. A stopped download resumes.</div></div>` : "")
    + items.map(reqRow).join("");
  const start = list => api("POST", "/api/downloads", { items: list.map(i => ({ key: i.key, folder: $(`.req[data-key="${i.key}"] .dl-folder`, el).value })) })
    .then(f => { S.reqs.fetches = f; showFetches(); }).catch(fail);
  if ($("#dl-core", el)) $("#dl-core", el).onclick = () => start(core);
  if ($("#dl-opt", el)) $("#dl-opt", el).onclick = () => start(optional);
  el.querySelectorAll(".req").forEach(row => {
    const i = items.find(x => x.key === row.dataset.key);
    const on = (sel, fn) => { const b = $(sel, row); if (b) b.onclick = fn; };
    on(".dl-go", () => start([i]));
    on(".dl-open", () => api("POST", "/api/open-model-folder", { folder: $(".dl-folder", row).value, sub: i.get.sub }).catch(fail));
    on(".node-go", async () => {
      const b = $(".node-go", row), out = $(".node-out", row);
      b.disabled = true; out.textContent = "Installing…";
      const said = (text, ok) => { INSTALL_OUT[i.key] = { text, ok }; out.textContent = text; out.className = "node-out hint " + (ok ? "ok" : "bad"); };
      try { const r = await api("POST", "/api/install-node", { key: i.key }); said(r.output, r.ok); }
      catch (e) { said(e.message, false); }
      b.disabled = false;
    });
    on(".pkg-go", async () => {
      const b = $(".pkg-go", row), out = $(".node-out", row);
      b.disabled = true; out.textContent = "Installing…";
      try {
        const r = await api("POST", "/api/install-deps", { missing: i.get.missing });
        INSTALL_OUT[i.key] = r.ok ? null : { text: lastLine(r.output), ok: false };
      } catch (e) { INSTALL_OUT[i.key] = { text: e.message, ok: false }; }
      $('[data-k="comfy_dir"]', $("#modal-card"))?.onchange?.();  // the folder check above says the same
      renderReqs(el).catch(e => (el.textContent = e.message));
    });
  });
  showFetches();
}

function reqRow(i) {
  const cls = i.ok ? "ok" : i.core ? "bad" : "opt";
  const said = INSTALL_OUT[i.key];
  const installOut = `<div class="node-out hint ${said ? (said.ok ? "ok" : "bad") : ""}">${esc(said?.text || "")}</div>`;
  let get = "";
  if (!i.ok && i.get?.kind === "model") {
    const f = S.reqs.folders[i.get.type] || { folders: [], default: "" };
    const sub = i.get.sub ? "\\" + i.get.sub : "";
    // the folder picked before this rebuild, else the one holding its unfinished download, else the suggestion
    const has = p => f.folders.some(x => x.path === p), under = S.reqs.fetches[i.key]?.dest.toLowerCase() || "";
    const started = S.reqs.fetches[i.key]?.state !== "done" && f.folders.find(p => under.startsWith(p.path.toLowerCase() + "\\"))?.path;
    const picked = has(S.reqs.picks[i.key]) ? S.reqs.picks[i.key] : started || f.default;
    get = `<div class="req-get">
      <span class="req-size">${GB(i.get.size)}</span>
      <select class="dl-folder" title="Where to save it: the ${esc(i.get.type)} folders this ComfyUI reads, extra_model_paths.yaml included">
        ${f.folders.map(p => `<option value="${esc(p.path)}" ${p.path === picked ? "selected" : ""}>${esc(p.path + sub)} · ${p.files} file${p.files === 1 ? "" : "s"}</option>`).join("")}</select>
      <button class="btn small dl-go">Download</button><button class="btn ghost small dl-open">Open folder</button>
      <a class="small" href="${esc(i.get.url)}" target="_blank" rel="noopener" title="${esc(i.get.page)}">direct link</a>
      <div class="dl-progress"></div></div>`;
  } else if (!i.ok && i.get?.kind === "node") {
    get = `<div class="req-get"><button class="btn small node-go">Install</button>
      <a class="small" href="${esc(i.get.git)}" target="_blank" rel="noopener">${esc(i.get.git.replace("https://github.com/", ""))}</a>
      ${installOut}</div>`;
  } else if (!i.ok && i.get?.kind === "packages") {
    get = `<div class="req-get"><button class="btn small pkg-go">Install</button>${installOut}</div>`;
  }
  // with buttons for it, only the part of the fix they don't cover; otherwise the whole fix, with its links clickable
  const hint = i.ok ? "" : i.get ? i.note : i.fix;
  return `<div class="req ${cls}" data-key="${esc(i.key)}">
    <span class="req-mark">${i.ok ? "✓" : i.core ? "✗" : "–"}</span>
    <div class="req-body"><div>${esc(i.name)}${i.need === "required" ? "" : ` <span class="muted">· for ${esc(i.need)}</span>`}</div>
    ${hint ? `<div class="hint">${linkify(hint)}</div>` : ""}${get}</div></div>`;
}

function showFetches() {
  // each download's progress on its row; a finished one checks ComfyUI again, so its row turns ✓
  const R = S.reqs;
  if (!R || !document.body.contains(R.el)) return;
  let finished = false;
  for (const [key, f] of Object.entries(R.fetches)) {
    const row = $(`.req[data-key="${key}"]`, R.el);
    if (!row || !$(".dl-progress", row)) continue;
    const busy = f.state === "queued" || f.state === "running";
    $(".dl-go", row).disabled = busy;
    $(".dl-folder", row).disabled = busy;
    const sel = $(".dl-folder", row), at = [...sel.options].find(o => f.dest.toLowerCase().startsWith(o.value.toLowerCase() + "\\"));
    if (at && (busy || f.state === "done")) sel.value = at.value;  // the folder it's going to, not the suggestion
    $(".dl-progress", row).innerHTML =
      f.state === "running" ? `<progress max="${f.size}" value="${f.done}"></progress> ${GB(f.done)} of ${GB(f.size)}${f.speed ? ` · ${Math.round(f.speed / 1e6)} MB/s` : ""} <a href="#" class="dl-cancel">Cancel</a>`
      : f.state === "queued" ? `Queued: starts when the download ahead of it finishes${f.done ? `, resuming at ${GB(f.done)}` : ""}. <a href="#" class="dl-cancel">Cancel</a>`
      : f.state === "done" ? `<span class="ok">${f.existed ? "Already there" : "Saved"}: ${esc(f.dest)}</span>${f.existed ? " · if this row stays ✗, restart ComfyUI and Check again" : ""}`
      : f.state === "failed" ? `<span class="bad">${esc(f.error)}</span>`
      : `Stopped at ${GB(f.done)} of ${GB(f.size)}; Download resumes it.`;
    const c = $(".dl-cancel", row);
    if (c) c.onclick = e => { e.preventDefault(); api("POST", `/api/downloads/${key}/cancel`).catch(fail); };
    if (f.state === "done" && R.shown?.[key] !== "done" && R.shown?.[key]) finished = true;
  }
  R.shown = Object.fromEntries(Object.entries(R.fetches).map(([k, f]) => [k, f.state]));
  if (finished) renderReqs(R.el).catch(e => (R.el.textContent = e.message));
}

async function fetchesChanged() {
  if (!S.reqs || !document.body.contains(S.reqs.el)) return;
  S.reqs.fetches = await api("GET", "/api/downloads");
  showFetches();
}

function applyState(s) {
  S.agent = s.agent; S.welcome = s;
  document.querySelectorAll(".agent-name").forEach(el => (el.textContent = s.agent));
}

// config.json was saved (Settings, or edited by hand / by the agent): the folders may hold other subjects now
async function settingsChanged() {
  applyState(await api("GET", "/api/state"));
  await loadSubjects();
  if (S.subjects.some(s => s.name === S.name)) await openSubject(S.name, true);
  else if (S.subjects.length) await openSubject(S.subjects[0].name);
  else { S.name = null; S.data = null; S.concept = null; S.draft = null; S.checked.clear(); $("#main").innerHTML = `<div class="empty">Pick a subject, or make a new one.</div>`; pushState(); }
}

(async () => {
  api("GET", "/api/state").then(s => {
    applyState(s);
    const next = () => { if (s.first_run) welcome(); };
    if (!s.comfy_ok) setupComfy(s, next); else next();
  }).catch(() => {});
  connectEvents();
  pollComfy(); setInterval(pollComfy, 5000);
  setInterval(() => { if (S.tab !== "runs") loadRuns().catch(() => {}); }, 15000);
  $("#splash-status").textContent = "loading runs…";
  await loadRuns().catch(() => {});
  $("#splash-status").textContent = "loading subjects…";
  await loadSubjects();
  const last = localStorage.getItem("ms-subject"), readable = S.subjects.filter(s => !s.error);
  if (readable.some(s => s.name === last)) await openSubject(last);
  else if (readable.length) await openSubject(readable[0].name);
  hideSplash();
})().catch(e => {
  hideSplash();
  $("#main").innerHTML = `<div class="empty"><b>Couldn't load:</b> ${esc(e.message)}<br><br><button class="btn" onclick="location.reload()">Try again</button></div>`;
});
