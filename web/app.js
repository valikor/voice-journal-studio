/* Voice Journal Studio — review UI. Vanilla JS, keyboard-first. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const api = async (path, body) => {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
};
const toastUndo = (msg, fn) => { const t = $("#toast"); t.innerHTML = `${esc(msg)} <button type="button" id="toast-undo" class="btn small">↶ Undo</button>`; t.classList.add("on");
  $("#toast-undo").onclick = async () => { t.classList.remove("on"); await fn(); toast("Put back"); }; clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("on"), 7000); };
const toast = (msg) => { const t = $("#toast"); t.textContent = msg; t.classList.add("on"); clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("on"), 1800); };
const SPK = ["kid", "mom", "dad", "other"];
let NAMES = { kid: "Kid", mom: "Mom", dad: "Dad", other: "Other" };
let EMOJI = { kid: "👧", mom: "👩", dad: "👨", other: "🗣️" };
const store = { get: (k, d) => { try { return JSON.parse(localStorage.getItem("vjs." + k)) ?? d; } catch { return d; } },
                set: (k, v) => { try { localStorage.setItem("vjs." + k, JSON.stringify(v)); } catch {} } };

function fmtDate(iso) {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { weekday: "short", month: "short", day: "numeric", year: "numeric" }) +
    " · " + d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}
const fmtT = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const spkChip = (spk, guess, conf) =>
  `<span class="spk ${spk}${guess ? " guess" : ""}" title="${guess ? `pitch guess (${Math.round((conf || 0) * 100)}%) — click or 1-4 to set` : "confirmed"}">${EMOJI[spk] || ""} ${esc(NAMES[spk] || spk)}</span>`;

/* ------------------------------------------------------------------ audio */
const player = $("#player");
const audio = {
  src: null, start: 0, end: 0, rate: 1,   // always 1x when the app opens: a slow speed is never remembered across sessions
  loopKey: null, card: null,   // card = the whole-card clip of the Triage/Polish card on screen
  play(rec, start, end, loop = false) {
    const src = `/api/clip?rec=${rec}&start=${start.toFixed(2)}&end=${end.toFixed(2)}`;
    this.start = start; this.end = end; player.loop = !!loop; if (!loop) this.loopKey = null;
    if (this.src !== src) { this.src = src; player.src = src; }
    player.currentTime = 0; player.playbackRate = this.rate;
    player.play().catch(() => {});
  },
  loopLine(rec, start, end, key) {  // 🔁 repeat one line until clicked again
    if (this.loopKey === key) { this.loopKey = null; player.loop = false; player.pause(); return false; }
    this.loopKey = key; this.play(rec, start, end, true); return true;
  },
  toggle() { if (!this.src) return; player.paused ? player.play() : player.pause(); },
  replay() { if (!this.src) return; player.currentTime = 0; player.play(); },
  setRate(r, quiet) { this.rate = r; player.playbackRate = r; document.dispatchEvent(new Event("vjs-rate")); if (!quiet) toast(`Speed ${r}×`); renderAudioBar(); renderSpeedPill(); },
  slower() { const L = [1, 0.8, 1.25, 1.5]; this.setRate(L[(L.indexOf(this.rate) + 1) % L.length]); },   // "," cycles 1 → 0.8 → 1.25 → 1.5 → 1
};
const RATES = [0.8, 1, 1.25, 1.5];
const speedBtns = () => `<span class="speed" title="playback speed (, cycles)">${RATES.map((r) => `<button type="button" data-rate="${r}" class="${audio.rate === r ? "on" : ""}">${r === 1 ? "1×" : r + "×"}</button>`).join("")}</span>`;
function wireSpeed(root) { $$("[data-rate]", root).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); audio.setRate(+b.dataset.rate, true); })); }
function renderSpeedPill() { const el = $("#speed"); if (!el) return; el.innerHTML = speedBtns(); wireSpeed(el); }
player.addEventListener("timeupdate", renderAudioBar);
function renderAudioBar() {
  const el = $("#abar"); if (!el) return;
  const dur = audio.end - audio.start || 1;
  el.innerHTML = `<button class="play" id="apl">${player.paused ? "▶" : "❚❚"}</button>
    <span class="t">${fmtT(player.currentTime || 0)} / ${fmtT(dur)}</span>${(() => { const c = audio.card; if (!c) return ""; const whole = Math.abs(audio.start - c.start) < 0.05 && Math.abs(audio.end - c.end) < 0.05;
      return whole ? `<span class="faint small" title="this is the audio of the whole card">whole card</span>` : `<span class="faint small">one line · </span><button type="button" class="btn small" id="a-card" title="play the whole card's audio (R replays what's loaded)">▶ whole card ${fmtT(c.end - c.start)}</button>`; })()}
    <div class="scrub" id="ascrub"><div style="width:${Math.min(100, 100 * (player.currentTime || 0) / dur)}%"></div></div>
    ${speedBtns()}`;
  wireSpeed(el);
  $("#apl").onclick = () => audio.toggle();
  const ac = $("#a-card"); if (ac) ac.onclick = () => audio.play(audio.card.rec, audio.card.start, audio.card.end);
  $("#ascrub").onclick = (e) => { const r = e.currentTarget.getBoundingClientRect(); player.currentTime = dur * (e.clientX - r.left) / r.width; player.play(); };
}
player.addEventListener("play", renderAudioBar); player.addEventListener("pause", renderAudioBar);

/* ------------------------------------------------------------------ router */
const views = {};
let current = null, STATUS = null;
/* Navigation: four jobs, each with its own views. Review = decide cards (three speeds); Polish & publish = finish what you kept;
   Library = look things up; Recordings = what's been recorded and what to do with it. Old routes (#tonight, #check, #lightning) land on Daily 5. */
const queuedN = () => (STATUS?.recordings || []).filter((r) => ["new", "analyzed", "transcribed"].includes(r.status)).reduce((a, r) => a + r.n, 0);
const NAV = [
  { id: "review", icon: "🗂", label: "Review", badge: () => STATUS?.moments?.pending /* all waiting: new + started + parked, so every Quick/Triage decision moves it */, views: [["daily", "⏱ Daily 5"], ["quick", "⚡ Quick"], ["triage", "🃏 Triage", () => STATUS?.moments?.new ?? STATUS?.moments?.pending]] },
  { id: "publish", icon: "✍️", label: "Polish & publish", badge: () => (STATUS?.moments?.journal || 0) + (STATUS?.moments?.ready || 0), views: [["polish", "Polish", () => STATUS?.moments?.journal], ["publish", "Publish", () => STATUS?.moments?.ready]] },
  { id: "library", icon: "📚", label: "Library", views: [["library", "Overview"], ["journal", "📖 Journal"], ["reader", "📜 Reader"], ["search", "🔎 Search"], ["log", "🗣 Language log"]] },
  { id: "recordings", icon: "🎙", label: "Recordings", badge: queuedN, views: [["inbox", "📥 Inbox", queuedN], ["recordings", "All recordings"], ["sort", "🧹 Sort the rest"]] },
];
const HINT = {
  daily: "5-minute guided round — the cards the system is least sure about, with sound; your answers sharpen it",
  quick: "fastest pass — one row per card with her best line · Y keep · N skip · L log",
  triage: "full cards with every tool (fix words & speakers, split, hide) and the piles: later · log · rejected · auto-skipped · no kid",
  polish: "finish what you kept — translations, notes, formatting", publish: "write the ready cards into the journal",
  library: "the archive at a glance — are you catching up, what to expect", journal: "everything on the site, plus the private notes it hides",
  reader: "read one recording as a continuous conversation and pick the gold", search: "find a line she said, or a recording by what it's about",
  log: "her language over time", inbox: "what's waiting to be processed — look, skip, or set aside",
  recordings: "every recording and where it stands", sort: "recordings that aren't about her — keep, label, or set aside",
};
const ALIAS = { tonight: "daily", check: "daily", lightning: "daily" };
const groupOf = (v) => NAV.find((g) => g.views.some((x) => x[0] === v));
function paintNav() {
  const g = groupOf(current), last = store.get("navLast", {});
  const n = (f) => { const x = f && f(); return x ? `<b>${x}</b>` : ""; };
  $("#tabs").innerHTML = NAV.map((gr) => `<a href="#${last[gr.id] || gr.views[0][0]}" class="${g === gr ? "on" : ""}">${gr.icon} ${gr.label}${n(gr.badge)}</a>`).join("");
  $("#subnav").innerHTML = g ? g.views.map(([v, label, badge]) => `<a href="#${v}" class="${v === current ? "on" : ""}">${label}${n(badge)}</a>`).join("") : "";
  if (g && HINT[current]) $("#subnav").insertAdjacentHTML("beforeend", `<span class="hint">${esc(HINT[current])}</span>`);
  $("#subnav").hidden = !g;
}
async function route() {
  window.HP = new URLSearchParams(location.hash.split("?")[1] || "");
  let v = (location.hash.slice(1) || "daily").split("?")[0];
  v = ALIAS[v] || v;
  current = v;
  const gr = groupOf(v); if (gr) store.set("navLast", { ...store.get("navLast", {}), [gr.id]: v });
  paintNav();
  player.pause(); player.loop = false; audio.loopKey = null; audio.card = null;
  document.onkeydown = null;
  await (views[v] || views.daily)();
}
window.addEventListener("hashchange", route);

// The app was updated while this tab was open: reload by itself, unless you're mid-edit
let UI_V = null;
function uiUpdateCheck(v) {
  if (!v) return;
  if (UI_V === null) { UI_V = v; return; }
  if (v === UI_V) return;
  const busy = document.querySelector("[contenteditable=true], .ins.open") || /INPUT|TEXTAREA/.test(document.activeElement?.tagName || "")
    || (typeof polishDirty !== "undefined" && polishDirty);
  if (!busy) return location.reload();
  if (!$("#ui-new")) {
    const b = document.createElement("div"); b.id = "ui-new"; b.className = "ui-new";
    b.innerHTML = `The review tool was updated — <a href="#">reload</a> when you finish this edit`;
    b.querySelector("a").onclick = (e) => { e.preventDefault(); location.reload(); };
    document.body.appendChild(b);
  }
}
async function refreshStatus() {
  try {
    const s = await api("/api/status");
    NAMES = { ...NAMES, ...s.names }; EMOJI = { ...EMOJI, ...s.emoji };
    uiUpdateCheck(s.ui);
    const m = s.moments;
    STATUS = s; paintNav();
    const rec = Object.fromEntries(s.recordings.map((r) => [r.status, r]));
    const left = ["new", "transcribed", "analyzed"].reduce((a, k) => a + (rec[k]?.n || 0), 0);
    const p = s.pipeline || {};
    const el = $("#pipe");
    el.classList.toggle("run", s.running);
    el.innerHTML = `<span class="dot"></span>` + (s.running
      ? `Processing ${p.done + 1 || ""}/${p.total || ""} · ${esc(p.current || "")} · ${esc(p.detail || "")}`
      : `${left ? left + " recordings waiting · " : ""}${rec.mined?.n || 0} processed`);
    return s;
  } catch (e) { $("#pipe").textContent = "server offline?"; }
}
setInterval(refreshStatus, 8000);

/* ================================================================== TRIAGE */
const T = { items: [], i: 0, focus: 0, history: [], total: 0, done: 0, opts: store.get("triageOpts2", { sort: "family", min: 7, tag: "", stage: "triage" }) };

views.triage = async () => {
  const v = $("#view");
  v.innerHTML = `
  <div class="row filters">
    <select id="f-stage"><option value="triage">New</option><option value="mixed">Mixed lines to correct</option><option value="hints">Tuned model disagrees</option><option value="started">Started (edited, not decided)</option><option value="parked">Parked small cards (skim in Quick)</option><option value="later">Later pile</option><option value="log">Log-only</option><option value="rejected">Rejected</option><option value="auto_skipped">Auto-skipped</option><option value="thin">Thin (she barely speaks)</option><option value="no_kid">No kid</option></select>
    ${T.opts.month ? `<button class="btn ghost" id="f-month" title="Showing only ${T.opts.month} — click to clear">📅 ${T.opts.month} ✕</button>` : ""}
    <select id="f-sort"><option value="family">Family first</option><option value="score">Best score</option><option value="date">Newest first</option></select>
    <label class="muted small">min score <select id="f-min">${[0, 5, 6, 7, 8, 9].map((n) => `<option>${n}</option>`).join("")}</select></label>
    <select id="f-tag"><option value="">all tags</option>${["funny", "code-switch", "grammar-slip", "new-word", "sweet", "milestone", "conversation", "question", "pretend-play", "song", "reasoning"].map((t) => `<option>${t}</option>`).join("")}</select>
    <span class="spacer"></span><span class="muted small" id="t-count"></span>
  </div>
  <div class="progress"><div id="t-prog" style="width:0"></div></div>
  <div id="t-card"></div>`;
  for (const k of ["stage", "sort", "min", "tag"]) {
    const el = $("#f-" + k); el.value = T.opts[k];
    el.onchange = () => { T.opts[k] = el.value; store.set("triageOpts2", T.opts); loadTriage(); };
  }
  if ($("#f-month")) $("#f-month").onclick = () => { delete T.opts.month; store.set("triageOpts2", T.opts); views.triage(); };
  document.onkeydown = triageKeys;
  await loadTriage();
};

async function loadTriage() {
  const o = T.opts;
  const params = new URLSearchParams({ stage: o.stage, sort: o.sort, min: o.min, tag: o.tag, limit: 40 });
  if (T.rec) params.set("rec", T.rec);
  if (o.month) params.set("month", o.month);
  if (T.pin) { const one = await api(`/api/moment/${T.pin}`); T.pin = null; T.items = [one]; T.i = 0; T.total = 1; T.done = 0; T.focus = firstFocus(one); return showCard(); }
  const r = await api("/api/queue?" + params);
  T.items = r.items; T.i = 0; T.total = r.total; T.done = 0; T.focus = firstFocus(T.items[0]);
  showCard();
}
function firstFocus(m) { if (!m) return 0; const i = m.segments.findIndex((s) => s.in && s.speaker === "kid"); return i >= 0 ? i : Math.max(0, m.segments.findIndex((s) => s.in)); }

function showCard(autoplay = true) {
  const m = T.items[T.i];
  $("#t-count").textContent = T.total ? `${Math.min(T.done + 1, T.total)} of ${T.total}${T.rec ? " (one recording)" : ""}` : "";
  $("#t-prog").style.width = T.total ? `${100 * T.done / T.total}%` : "0";
  const card = $("#t-card");
  if (!m) {
    card.innerHTML = `<div class="empty"><h2>Nothing here right now</h2>
      <p>${T.opts.stage === "triage" ? "New moments appear as the pipeline works through recordings. You can also lower the minimum score, or find a quote with Search." : "This pile is empty."}</p>
      ${T.rec ? `<p><a href="#triage" onclick="T.rec=null">Show all recordings</a></p>` : ""}</div>`;
    return;
  }
  const inside = m.segments.filter((s) => s.in);
  audio.card = m.clip ? { rec: m.rec.id, start: m.clip.start, end: m.clip.end } : null;
  const first = m.segments.findIndex((s) => s.in), last = m.segments.length - 1 - [...m.segments].reverse().findIndex((s) => s.in);
  card.innerHTML = `${T.opts.stage === "no_kid" ? `<div class="nokid-banner" id="nk-b">These were set aside by the rule that she can't be in this recording. Found her? <button class="btn small primary" id="nk-one">👧 She's in this recording</button>
      <button class="btn small" id="nk-year">👧 Allow her in all ${esc(m.rec.recorded_at.slice(0, 4))} recordings…</button><span class="faint small" id="nk-note"></span></div>` : ""}<div class="card">
    <div class="meta"><span class="date">${fmtDate(m.rec.recorded_at)}</span>
      ${m.rec.title ? `<span class="muted small">${esc(m.rec.title)}</span>` : ""}
      ${m.priority >= 2 ? `<span class="pill" title="from a work / background-media stretch — lower priority">${esc(m.rec.category || "work")}</span>` : ""}
      <button type="button" class="btn small ghost" id="t-textedit" title="edit the whole card as text (W)">✎ edit as text <kbd>W</kbd></button>
      <a class="btn small ghost" href="#reader?rec=${m.rec.id}&seg=${(inside[0] || {}).id || ""}" title="read this recording as one continuous conversation">📖 open in reader</a>
      <button type="button" class="btn small ghost" id="t-label" title="add a note / tags to this recording (place, people, trip…)">🏷 label</button>${labelChips(m.rec.label)}
      <span class="spacer"></span>${m.star ? `<span class="star-on">★</span>` : ""}
      <span class="pill score" title="AI score">${m.score}</span>${m.tags.map((t) => `<span class="pill">${esc(t)}</span>`).join(" ")}</div>
    ${m.why ? `<div class="why">${esc(m.why)}</div>` : ""}
    ${m.seen ? `<div class="seen-banner">${m.seen.overlap ? `↔ <b>same audio is already in</b> ${m.seen.overlap.map((o) => `<a href="#" data-open="${o.id}">card #${o.id} (${esc(o.status)})</a>`).join(", ")}. ` : ""}${m.seen.twin ? `↔ <b>looks like a card you already decided</b> on ${esc(m.seen.twin.date)}: <a href="#" data-open="${m.seen.twin.id}">#${m.seen.twin.id} — <b>${esc(m.seen.twin.status)}</b></a> “${esc(m.seen.twin.text)}” <span class="faint small">(${Math.round(m.seen.twin.cos * 100)}% alike)</span>` : ""}</div>` : ""}
    ${m.media_hint ? `<div class="win-sug">📺 <b>may not be her</b> — the voice on this card doesn't match hers (${m.media_hint.sim}); TV or another child? <button type="button" class="btn small" data-d="rejected">Reject it</button></div>` : ""}
    ${m.window ? `<div class="win-sug">✨ <b>better window:</b> ${esc(m.window.text)} <span class="faint small">(built around her best line${m.punch_tag ? " — " + esc(m.punch_tag) : ""})</span> <button type="button" class="btn small primary" id="win-use">use</button></div>` : ""}
    <div class="bar-audio" id="abar"></div>
    <div class="lines">
      ${first > 0 || m.seg_from > 0 ? `<div class="extend" id="ext-b" title="pull in more of the recording before this moment">↑ show more of the conversation before <kbd>[</kbd></div>` : ""}
      ${m.segments.map((s, k) => (s.in && !m.segments.slice(0, k).some((x) => x.in) ? insSlot("") : "") + `
        <div class="ln ${s.in ? "" : "ctx"} ${k === T.focus ? "focus" : ""}" data-k="${k}">
          ${spkChip(s.speaker, !s.spk, s.spk_conf)}
          <div class="txt" data-id="${s.id}">${esc(s.text)}${s.text !== s.raw ? ` <span class="faint small" title="original: ${esc(s.raw)}">✎</span>` : ""}${zhHint(s) ? ` <button type="button" class="zhchip" title="the overnight scan thinks this was said in Chinese — see the Chinese reading (H)">中文? ${Math.round(zhHint(s).p_zh * 100)}%</button>` : ""}${s.in && s.dup_of ? ` <button type="button" class="dupchip" data-hide="${s.id}" title="this looks like the same moment of audio heard twice">⧉ ${s.dup_kind === "translation" ? "same line in the other language" : "duplicate of a line above"} — hide</button>` : ""}${s.in && k === T.focus ? vibeBar() : ""}</div>
          <div class="ln-btns">${s.in ? "" : `<button type="button" class="ctxadd" title="add this line to the card (text and audio)">＋ add to card</button>`}<button class="play" title="play this line (P)">▶</button><button type="button" class="fromhere" title="play from this line to the end of the card">⏵</button><button type="button" class="loopbtn" title="repeat this line until you click again">🔁</button>${s.in ? `<button type="button" class="hearbtn" title="wrong language? re-hear this line in Chinese and English (H)">文</button><button type="button" class="flipbtn" title="speakers off by one from here? swap ${esc(NAMES.kid)} ↔ the adult on this line and every line below (F)">⇅</button><button type="button" class="cutbtn" title="several people in one line? select the words someone else said — or E, then / between turns">✂</button><button type="button" class="echobtn" title="she (or you) repeated this line — copy it below as the other person">↻</button><button type="button" class="hidebtn" title="hide this line (a stray fragment, a repeat) — undo right after, or hide is reversible from the log">🙈</button>` + addBtn(s.id) : ""}</div>
        </div>` + (s.in ? noteBar(s.id, s.text) + insSlot(s.id) : "")).join("")}
      <div class="extend" id="ext-a" title="pull in more of the recording after this moment">↓ show more of the conversation after <kbd>]</kbd></div>
    </div>
    <div class="actions">
      <button class="btn big j" data-d="journal">Journal <kbd>J</kbd></button>
      <button class="btn big" data-d="log">Log only <kbd>L</kbd></button>
      <button class="btn big" data-d="later">Later <kbd>N</kbd></button>
      <button class="btn big x" data-d="rejected">Reject <kbd>X</kbd></button>
      ${T.opts.stage === "auto_skipped" ? `<button class="btn big" data-d="pending" title="put it back in the normal Triage pile">↩ Back to Triage</button>` : ""}
      <span class="spacer"></span>
      <button class="btn t-rp" id="t-replay" type="button">⟲ Replay <kbd>R</kbd></button>
      <button class="btn" id="t-undo" ${T.history.length ? "" : "disabled"}>Undo <kbd>U</kbd></button>
    </div>
    <div class="keys"><kbd>Space</kbd> play/pause · <kbd>R</kbd> replay · <kbd>,</kbd> slower · <kbd>↑</kbd><kbd>↓</kbd> pick line · <kbd>1</kbd>–<kbd>4</kbd> speaker, then next line (${SPK.map((s) => NAMES[s]).join(" / ")}) · <kbd>F</kbd> swap speakers from here down · <kbd>H</kbd> re-hear in 中文/EN · <kbd>E</kbd> fix text (type <b>/</b> between turns to split a line) · <kbd>I</kbd> add a missed line below (<kbd>⇧I</kbd> above) · <kbd>P</kbd> play line · <kbd>[</kbd><kbd>]</kbd> more context · <kbd>{</kbd><kbd>}</kbd> trim · <kbd>S</kbd> star · <kbd>T</kbd> full transcript</div>
  </div>`;
  $$(".actions [data-d]").forEach((b) => (b.onclick = () => decide(b.dataset.d)));
  if ($("#nk-one")) {
    const go = async (body, msg) => { if (!confirm(msg)) return; const r = await api("/api/kidok", body); toast(`She's in: ${r.recordings} recording(s) · ${r.cards_back} card(s) back in Triage · ${r.lines_back} line(s) hers again${r.queued ? ` · ${r.queued} queued for transcription (next ./vjs.sh run)` : ""}`); refreshStatus(); loadTriage(); };
    $("#nk-one").onclick = () => go({ rec: m.rec.id, allow: true }, "Mark her as present in this recording? Its parked cards go back to Triage and her lines are restored (undoable).");
    $("#nk-year").onclick = async () => { const i = await api(`/api/kidok/info?rec=${m.rec.id}`); go({ rec: m.rec.id, year: m.rec.recorded_at.slice(0, 4), allow: true }, `Allow her in all ${i.recordings} ${m.rec.recorded_at.slice(0, 4)} recordings the rule currently sets aside?\n${i.cards} cards go back to Triage; ${i.unprocessed} never-transcribed recording(s) get queued for processing (GPU time). Undoable per recording.`); };
  }
  $$("#t-card [data-open]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); T.pin = +a.dataset.open; T.opts = { ...T.opts, stage: "triage" }; loadTriage(); }));
  if ($("#win-use")) $("#win-use").onclick = () => changeRange(m.window.before, m.window.after);
  $("#t-textedit").onclick = () => openTextEditor(m, false);
  $("#t-label").onclick = async () => { const pl = (await api(`/api/places?all=1`)).places.find((x) => x.place === m.rec.place); labelDialog({ recId: m.rec.id, title: `${m.rec.title || m.rec.filename} · ${fmtDate(m.rec.recorded_at)}`, place: m.rec.place, nPlace: pl ? pl.n : 1, label: m.rec.label }, async () => { T.items[T.i] = await api(`/api/moment/${m.id}`); showCard(false); }); };
  $("#t-undo").onclick = undo;
  $("#t-replay").onclick = () => { if (audio.card && audio.src) audio.replay(); else if (audio.card) audio.play(audio.card.rec, audio.card.start, audio.card.end); };
  $("#ext-b") && ($("#ext-b").onclick = () => changeRange(1, 0));
  $("#ext-a").onclick = () => changeRange(0, 1);
  $$(".ln").forEach((el) => {
    const k = +el.dataset.k;
    el.onclick = (e) => { if (e.target.closest(".spk")) { T.focus = k; cycleSpeaker(); } else if (!e.target.isContentEditable) { T.focus = k; paintFocus(); } };
    $(".play", el).onclick = (e) => { e.stopPropagation(); T.focus = k; playLine(); };
    wireLineAudio(el, m.rec.id, m.segments[k], m.clip && m.clip.end);
    const add = $(".ctxadd", el); if (add) add.onclick = (e) => { e.stopPropagation(); const sg = m.segments[k]; sg.idx < m.seg_from ? changeRange(m.seg_from - sg.idx, 0) : changeRange(0, sg.idx - m.seg_to); };
    $(".txt", el).ondblclick = () => { T.focus = k; editLine(); };
    const chip = $(".zhchip", el); if (chip) chip.onclick = (e) => { e.stopPropagation(); T.focus = k; paintFocus(); rehear(); };
    const hear = $(".hearbtn", el); if (hear) hear.onclick = (e) => { e.stopPropagation(); T.focus = k; paintFocus(); rehear(); };
    const flip = $(".flipbtn", el); if (flip) flip.onclick = (e) => { e.stopPropagation(); T.focus = k; flipFromHere(); };
    const cut = $(".cutbtn", el); if (cut) cut.onclick = (e) => { e.stopPropagation(); T.focus = k; editLine(); toast("Type / between the turns — speakers alternate automatically"); };
  });
  // power tools: hide duplicate · echo · one-tap vibe · split by selecting words
  $$("#t-card [data-hide]").forEach((b) => (b.onclick = async (e) => { e.stopPropagation();
    T.items[T.i] = await api(`/api/moment/${m.id}/hide`, { seg_id: +b.dataset.hide, via: "triage" }); toast("Hidden ✓"); showCard(false); }));
  $$("#t-card .hidebtn").forEach((b) => (b.onclick = async (e) => { e.stopPropagation();
    const s = m.segments[+b.closest(".ln").dataset.k];
    T.items[T.i] = await api(`/api/moment/${m.id}/hide`, { seg_id: s.id, via: "triage-hide" }); showCard(false);
    toastUndo(`Hidden: "${s.text.slice(0, 40)}"`, async () => { T.items[T.i] = await api(`/api/moment/${m.id}/unhide`, { seg_id: s.id, via: "triage-hide" }); showCard(false); }); }));
  $$("#t-card .echobtn").forEach((b) => (b.onclick = async (e) => { e.stopPropagation();
    const s = m.segments[+b.closest(".ln").dataset.k];
    T.items[T.i] = await api(`/api/moment/${m.id}/addline`, { text: s.text, speaker: otherSpeaker(s.speaker, m.segments), after_seg_id: s.id, via: "echo" });
    toast("Echo added ↻"); showCard(false); }));
  $$("#t-card .vb").forEach((b) => (b.onclick = async (e) => { e.stopPropagation();
    const s = m.segments[+b.closest(".ln").dataset.k], t = vibe(s.text, b.dataset.vibe);
    if (t === s.text) return;
    s.text = t; showCard(false); await api(`/api/moment/${m.id}/segment`, { seg_id: s.id, text: t, via: "vibe" }); }));
  $$("#t-card .ln .txt").forEach((el) => (el.onmouseup = () => {
    if (el.isContentEditable) return;
    const sel = getSelection(); if (!sel || sel.isCollapsed || !el.contains(sel.anchorNode)) return;
    const k = +el.closest(".ln").dataset.k, s = m.segments[k];
    const r = sel.getRangeAt(0), pre = document.createRange(); pre.setStart(el, 0); pre.setEnd(r.startContainer, r.startOffset);
    const start = pre.toString().length, len = sel.toString().length;
    if (start >= s.text.length || !sel.toString().trim()) return;
    splitPopup(r.getBoundingClientRect(), s.speaker, async (spk) => {
      const parts = partsFromSelection(s.text, start, Math.min(len, s.text.length - start), s.speaker, spk);
      sel.removeAllRanges();
      if (parts.length < 2) { T.focus = k; return setSpeaker(spk, false); }
      T.items[T.i] = await api(`/api/moment/${m.id}/split`, { seg_id: s.id, parts, via: "triage-select" });
      toast(`Split into ${parts.length} lines ✂`); showCard(false);
    });
  }));
  const order = m.segments.filter((s) => s.in).map((s) => String(s.id));
  T.openInsert = wireInserts($("#t-card"), m.id, order, (upd) => { T.items[T.i] = upd; showCard(false); },
    async (note, segId) => {  // the note leaves its host line once it's a real line
      const s = m.segments.find((x) => String(x.id) === segId);
      const t = s.text.replace(note.full, "").replace(/\s{2,}/g, " ").trim();
      if (t) await api(`/api/moment/${m.id}/segment`, { seg_id: s.id, text: t, via: "triage-note" });
    });
  if (m.clip && autoplay) audio.play(m.rec.id, m.clip.start, m.clip.end);
  renderAudioBar();
}
function triageInsert(above) {  // I: missed line below the picked line · ⇧I: above it
  const m = T.items[T.i]; if (!m || !T.openInsert) return;
  const ins = m.segments.filter((s) => s.in), s = m.segments[T.focus];
  let i = ins.indexOf(s);
  if (i < 0) i = m.segments.indexOf(ins[0]) > T.focus ? -1 : ins.length - 1;  // on a grey context line
  else if (above) i -= 1;
  T.openInsert(i >= 0 ? String(ins[i].id) : "");
}
function paintFocus() { $$(".ln").forEach((el) => el.classList.toggle("focus", +el.dataset.k === T.focus)); }
function playLine() { const m = T.items[T.i], s = m.segments[T.focus]; audio.play(m.rec.id, Math.max(0, s.start - 0.3), s.end + 0.4); }

async function setSpeaker(spk, advance = true) {
  const m = T.items[T.i], s = m.segments[T.focus];
  s.speaker = spk; s.spk = spk;
  // move to the next line, so a run of turns is just 1 3 1 3 …
  if (advance) T.focus = Math.min(m.segments.length - 1, T.focus + 1);
  showCard(false);
  await api(`/api/moment/${m.id}/segment`, { seg_id: s.id, speaker: spk });
}
function zhHint(s) {  // overnight wrong-language scan result, if this line looked like Chinese speech
  if (s.fixed || !s.lang_hint) return null;
  try { const h = JSON.parse(s.lang_hint); return h.zh ? h : null; } catch { return null; }
}
async function rehear(fresh) {  // whisper sometimes translates Chinese speech into English: hear it again both ways, pick one
  const m = T.items[T.i], s = m.segments[T.focus], row = $(`.ln[data-k="${T.focus}"]`);
  if (!s || !row) return;
  row.nextElementSibling?.classList.contains("hear-box") && row.nextElementSibling.remove();
  const box = document.createElement("div"); box.className = "hear-box";
  row.after(box);
  let r;
  const cached = !fresh && zhHint(s);
  if (cached) r = { zh: cached.zh, p_zh: cached.p_zh, en: "", p_en: 0 };  // already heard overnight — instant
  else {
    box.innerHTML = `<span class="spinner"></span> listening again in 中文 and English…`;
    try { r = await api(`/api/moment/${m.id}/rehear`, { seg_id: s.id }); } catch { box.textContent = "Couldn't re-hear (is whisper running?)"; return; }
  }
  const opt = (lang, label, p) => r[lang] && r[lang] !== s.text
    ? `<button type="button" class="alt-pick" data-lang="${lang}"><span class="k">${label} ${Math.round(p * 100)}%</span> ${esc(r[lang])} <kbd>${lang === "zh" ? 1 : 2}</kbd></button>` : "";
  const opts = opt("zh", "中文", r.p_zh) + opt("en", "EN", r.p_en);
  box.innerHTML = opts ? `<span class="faint small">Heard again — pick the right one (% = how sure whisper is of the language):</span>${opts}<div class="row"><button type="button" class="btn ghost small" data-keep>keep as is <kbd>Esc</kbd></button>${cached ? `<button type="button" class="btn ghost small" data-fresh>↻ hear both again</button>` : ""}</div>`
                       : `<span class="faint small">Both listens match what's there — nothing to change.</span>`;
  const fr = box.querySelector("[data-fresh]"); if (fr) fr.onclick = () => { document.removeEventListener("keydown", keys, true); rehear(true); };
  const pick = async (lang) => {
    document.removeEventListener("keydown", keys, true); box.remove();
    s.text = r[lang]; showCard(false);
    await api(`/api/moment/${m.id}/segment`, { seg_id: s.id, text: r[lang], via: "rehear" }); toast("Replaced ✓");
  };
  const keys = (e) => {
    if (!document.body.contains(box)) return document.removeEventListener("keydown", keys, true);
    if ((e.key === "1" && r.zh) || (e.key === "2" && r.en)) { e.preventDefault(); e.stopPropagation(); pick(e.key === "1" ? "zh" : "en"); }
    if (e.key === "Escape") { e.stopPropagation(); box.remove(); document.removeEventListener("keydown", keys, true); }
  };
  if (opts) document.addEventListener("keydown", keys, true);
  box.querySelectorAll("[data-lang]").forEach((b) => (b.onclick = () => pick(b.dataset.lang)));
  const keep = box.querySelector("[data-keep]"); if (keep) keep.onclick = () => { box.remove(); document.removeEventListener("keydown", keys, true); };
}
async function flipFromHere() {  // everything from this line down was off by one turn: swap her ↔ the adult
  const m = T.items[T.i], partner = partnerOf(m.segments);
  const todo = m.segments.slice(T.focus).filter((s) => s.in && (s.speaker === "kid" || s.speaker === partner));
  todo.forEach((s) => { s.speaker = s.spk = s.speaker === "kid" ? partner : "kid"; });
  showCard(false); toast(`Swapped ${EMOJI.kid} ↔ ${EMOJI[partner]} on ${todo.length} line(s) — U to undo`);
  T.flipUndo = { mid: m.id, focus: T.focus };
  for (const s of todo) await api(`/api/moment/${m.id}/segment`, { seg_id: s.id, speaker: s.speaker, via: "flip" });
}
function cycleSpeaker() { const s = T.items[T.i].segments[T.focus]; setSpeaker(SPK[(SPK.indexOf(s.speaker) + 1) % SPK.length], false); }

function editLine() {
  const m = T.items[T.i], s = m.segments[T.focus];
  const el = $(`.ln[data-k="${T.focus}"] .txt`);
  el.textContent = s.text; el.contentEditable = "true"; el.focus();
  document.getSelection().selectAllChildren(el); document.getSelection().collapseToEnd();
  const unwatch = watchSplit(el, s, m.segments, el.closest(".ln"));
  let finishing = false;  // Enter + blur (or a double press) must not save the same edit twice — that duplicated split lines
  const finish = async (save, redraw) => {
    if (finishing) return; finishing = true;
    el.contentEditable = "false"; el.onkeydown = null; el.onblur = null; unwatch();
    const t = el.textContent.trim();
    const parts = save && splitParts(t, s.speaker, partnerOf(m.segments));
    if (parts) {
      T.items[T.i] = await api(`/api/moment/${m.id}/split`, { seg_id: s.id, parts, via: "triage" });
      toast(`Split into ${parts.length} lines ✂`); return showCard(false);
    }
    if (save && t && t !== s.text) { s.text = t; await api(`/api/moment/${m.id}/segment`, { seg_id: s.id, text: t, via: "triage" }); toast("Saved correction"); }
    else el.textContent = s.text;
    // clicking elsewhere saves quietly; only Enter/Esc redraw, so the click you made still lands
    if (redraw || noteOf(s.text)) showCard(false);
  };
  el.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); finish(true, true); } if (e.key === "Escape") finish(false, true); };
  el.onblur = () => finish(true);
}

async function changeRange(before, after) {
  const m = T.items[T.i];
  const upd = await api(`/api/moment/${m.id}/range`, { before, after });
  T.items[T.i] = upd; showCard(true);
}

async function decide(d) {
  const m = T.items[T.i]; if (!m) return;
  const speakers = {};
  m.segments.filter((s) => s.in).forEach((s) => (speakers[s.id] = s.speaker));
  await api(`/api/moment/${m.id}/triage`, { decision: d, speakers, star: m.star ? 1 : 0 });
  T.history.push({ m, prev: T.opts.stage === "triage" ? "pending" : T.opts.stage === "later" ? "later" : m.status });
  toast({ journal: "→ Journal (polish later)", log: "→ Language log", later: "→ Later pile", rejected: "Rejected" }[d]);
  T.items.splice(T.i, 1); T.done++; T.flipUndo = null;
  if (T.items.length < 5) { const keep = T.done; await loadTriage(); T.done = keep; }
  T.focus = firstFocus(T.items[T.i]);
  showCard(); refreshStatus();
}
async function undo() {
  const m = T.items[T.i];
  if (T.flipUndo && m && T.flipUndo.mid === m.id) { T.focus = T.flipUndo.focus; T.flipUndo = null; return flipFromHere(); }
  const h = T.history.pop(); if (!h) return;
  await api(`/api/moment/${h.m.id}/triage`, { decision: h.prev });
  T.items.splice(T.i, 0, h.m); T.done = Math.max(0, T.done - 1); T.focus = firstFocus(h.m);
  showCard(); refreshStatus(); toast("Undone");
}

function triageKeys(e) {
  if (e.metaKey || e.ctrlKey || e.altKey || e.target.isContentEditable || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
  const m = T.items[T.i];
  const k = e.key;
  const map = { j: () => decide("journal"), l: () => decide("log"), n: () => decide("later"), x: () => decide("rejected"), u: undo,
    " ": () => audio.toggle(), r: () => audio.replay(), ",": () => audio.slower(), p: playLine, e: editLine, w: () => { const mm = T.items[T.i]; if (mm) openTextEditor(mm, false); },
    i: () => triageInsert(false), I: () => triageInsert(true), f: flipFromHere, h: rehear,
    "[": () => changeRange(1, 0), "]": () => changeRange(0, 1), "{": () => changeRange(-1, 0), "}": () => changeRange(0, -1),
    s: () => { m.star = m.star ? 0 : 1; showCard(false); toast(m.star ? "Starred ★" : "Unstarred"); },
    t: () => m && openTranscript(m.rec.id, m.segments.find((s) => s.in)?.idx),
    ArrowDown: () => { T.focus = Math.min(m.segments.length - 1, T.focus + 1); paintFocus(); },
    ArrowUp: () => { T.focus = Math.max(0, T.focus - 1); paintFocus(); },
    1: () => setSpeaker("kid"), 2: () => setSpeaker("mom"), 3: () => setSpeaker("dad"), 4: () => setSpeaker("other") };
  const fn = map[k] || map[k.toLowerCase()];
  if (fn && (m || k === "u")) { e.preventDefault(); fn(); }
}

/* ================================================================== POLISH */
const P = { items: [], sel: null, cur: null, poll: null };
views.polish = async () => {
  $("#view").innerHTML = `<div class="polish"><div class="plist" id="p-list"></div><div id="p-main"></div></div>`;
  await loadPolish();
  document.onkeydown = (e) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter" && !e.isComposing) { e.preventDefault(); savePolish("ready"); }
    if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); savePolish(); }
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.metaKey || e.ctrlKey) return;
    if (e.key === " ") { e.preventDefault(); audio.toggle(); }
    if (e.key === "r") audio.replay();
    if (e.key === ",") audio.slower();
  };
};
async function loadPolish() {
  const r = await api("/api/queue?stage=polish&sort=date&limit=200");
  P.items = r.items;
  if (!P.items.find((m) => m.id === P.sel)) P.sel = P.items[0]?.id;
  renderPList();
  if (P.sel) openPolish(P.sel); else $("#p-main").innerHTML = `<div class="empty"><h2>Nothing to polish</h2><p>Moments you mark <b>Journal</b> in Triage land here.</p></div>`;
}
// what only listening can fix, plus what the safe clean-up already did (with undo)
function polishBanner(m) {
  const e = m.ears || {}, rows = [];
  if (e.missing?.length) rows.push(`<div><b>👂 Needs ears:</b> the picker quoted ${e.missing.map((q) => `“${esc(q)}”`).join(", ")}, but it isn't in the draft. `
    + (e.widen ? `<button class="btn small" id="p-widen">↔ Widen to include “${esc((e.found[0] || "").slice(0, 40))}”</button>`
               : `<button class="btn small ghost" id="p-wide-play">🔊 Play 10 s either side</button> <span class="faint">— then fix the words, or ＋ the missing line</span>`) + `</div>`);
  const soft = [e.disputed ? `${e.disputed} line${e.disputed > 1 ? "s" : ""} the second listen heard differently (see “2nd listen” under the line)` : "",
                e.unsure ? `${e.unsure} unsure speaker guess${e.unsure > 1 ? "es" : ""}` : ""].filter(Boolean);
  if (soft.length) rows.push(`<div class="faint">👂 ${soft.join(" · ")}</div>`);
  const ac = m.autocleaned || [];
  if (ac.length) {
    const hid = ac.filter((c) => c.include && !c.after.include).length, tidy = ac.length - hid;
    const detail = ac.map((c) => `${c.text}  →  ${c.after.include ? c.after.text : "(hidden)"}`).join("\n");
    rows.push(`<div class="faint" title="${esc(detail)}">✨ Tidied automatically: ${[tidy ? `${tidy} line${tidy > 1 ? "s" : ""} punctuated/merged` : "", hid ? `${hid} empty or merged line${hid > 1 ? "s" : ""} hidden` : ""].filter(Boolean).join(", ")} <button class="btn ghost small" id="p-undoclean">undo</button></div>`);
  }
  return rows.length ? `<div class="ears">${rows.join("")}</div>` : "";
}
function renderPList() {
  $("#p-list").innerHTML = P.items.map((m) => {
    const q = m.segments.filter((s) => s.in && s.speaker === "kid").map((s) => s.text).join(" ") || m.segments.find((s) => s.in)?.text || "";
    return `<div class="it ${m.id === P.sel ? "on" : ""}" data-id="${m.id}"><div class="faint small">${fmtDate(m.rec.recorded_at).split(" · ")[0]}${m.star ? " ★" : ""}${m.ears?.missing?.length ? ` <span title="needs ears: the picked punchline isn't in the draft">👂</span>` : ""}</div><div class="q">${esc(q)}</div></div>`;
  }).join("") || `<div class="faint small">Empty</div>`;
  $$("#p-list .it").forEach((el) => (el.onclick = () => { P.sel = +el.dataset.id; renderPList(); openPolish(P.sel); }));
}
async function openPolish(id) {
  clearTimeout(P.poll);
  await flushPolishSave();
  let m = await api(`/api/moment/${id}`);
  if (!m.lines.length) { await api(`/api/moment/${id}/prepare`, {}); m = await api(`/api/moment/${id}`); }
  P.cur = m;
  audio.card = m.clip ? { rec: m.rec.id, start: m.clip.start, end: m.clip.end } : null;
  m.lines.forEach((l) => { const r = m.relisten?.lines?.[l.seg_id]; if (r && (r.alt || r.other) && r.verdict !== "agree") l.alts.relisten = r.alt || r.other; });
  if (!m.relisten) api(`/api/moment/${id}/relisten`, {}).then(() => { if (P.sel === id && current === "polish" && !polishDirty) openPolish(id); }).catch(() => {});
  const busy = ["queued", "working"].includes(m.prep);
  const dateDefault = m.entry_date || m.rec.recorded_at.slice(0, 10);
  $("#p-main").innerHTML = `
    <div class="meta"><span class="date">${fmtDate(m.rec.recorded_at)}</span>${m.rec.title ? `<span class="muted small">${esc(m.rec.title)}</span>` : ""}
      <button type="button" class="btn small ghost" id="p-textedit" title="edit the whole card as text">✎ edit as text</button>
      <a class="btn small ghost" href="#reader?rec=${m.rec.id}&seg=${(m.lines.find((l) => l.seg_id) || {}).seg_id || ""}">📖 open in reader</a>
      <button type="button" class="btn small ghost" id="p-label">🏷 label</button>${labelChips(m.rec.label)}</div>
    ${m.why ? `<div class="why">${esc(m.why)}</div>` : ""}
    ${polishBanner(m)}
    <div class="bar-audio" id="abar"></div>
    ${busy ? `<p class="muted small"><span class="spinner"></span>Re-listening in Mandarin and English, and drafting translations…</p>` : ""}
    <div class="legend small faint">Sounds in any brackets — <b>[giggles]</b> <b>(whispers)</b> <b>&lt;roaring&gt;</b> — become <i>[giggles]</i> outside the quote ·
      <b>{curly}</b> = a note to yourself, never published · type the English in the box under a Chinese line; formatting is automatic</div>
    <div id="p-lines">${insSlot("")}${m.lines.map((l, k) => polishLine(l, k) + (l.seg_id ? noteBar(String(l.seg_id), l.text) + insSlot(l.seg_id) : "")).join("")}</div>
    ${relistenExtras(m)}
    <div class="row small"><span class="extend" id="p-eb" title="pull in more of the recording">↑ more conversation before</span><span class="extend" id="p-ea" title="pull in more of the recording">↓ more after</span>
      <span class="spacer"></span><button class="btn ghost small" id="p-add">＋ missing line (typed)</button></div>
    <div id="p-addform"></div>
    <label class="f">Context note — <b>published</b> as <i>*[note]*</i> after the last line. Only facts you know.</label>
    <input type="text" id="p-npub" style="width:100%" value="${esc(m.note_public)}" placeholder="e.g. At the skate park, watching others.">
    <label class="f">Private note — never leaves this app</label>
    <input type="text" id="p-npriv" style="width:100%" value="${esc(m.note_private)}" placeholder="e.g. re-check audio later">
    <div class="row" style="margin-top:12px">
      <label class="small muted">Journal date <input type="date" id="p-date" value="${dateDefault}"></label>
      <label class="small muted"><input type="checkbox" id="p-star" ${m.star ? "checked" : ""}> ⭐ highlight</label>
      <label class="small muted" title="a clip of the real recording plays with this quote on the family journal"><input type="checkbox" id="p-audio" ${m.audio_off ? "" : "checked"}> 🔊 her voice on the site</label>
    </div>
    <label class="f">Preview (house style)</label>
    <div class="small warn-hint" id="p-hint" hidden>Highlighted <b>{curly}</b> text is a note to yourself and won't be published. For sounds use <b>[square brackets]</b>, e.g. [giggles], and they'll appear as <i>[giggles]</i>.</div>
    <div class="preview pretty" id="p-prev"></div>
    <div class="actions" style="margin-top:16px;border:0">
      <button class="btn big primary" id="p-ready">Ready to publish <kbd>⌘↵</kbd></button>
      <span class="saved" id="p-saved">All changes saved</span>
      <span class="spacer"></span>
      <button class="btn" id="p-back">Back to triage</button>
      <button class="btn x" id="p-rej">Reject</button>
    </div>`;
  $$("#p-lines .pline").forEach(wirePolishLine);
  const order = m.lines.filter((l) => l.seg_id).map((l) => String(l.seg_id));
  wireInserts($("#p-lines"), id, order, () => openPolish(id), async (note, segId) => {
    const el = $$("#p-lines .pline").find((x) => String(P.cur.lines[+x.dataset.k].seg_id) === segId);
    if (!el) return;
    $(".text", el).value = $(".text", el).value.replace(note.full, "").replace(/\s{2,}/g, " ").trim();
    await rerender(el); await flushPolishSave();
  });
  // save typing first, so the reload after an insert never drops it
  $$("#p-lines [data-addafter], #p-lines .ins.top").forEach((b) => b.addEventListener("click", () => flushPolishSave(), { capture: true }));
  $("#p-textedit").onclick = async () => { await flushPolishSave(); openTextEditor(P.cur, true); };
  $("#p-label").onclick = async () => { const pl = (await api(`/api/places?all=1`)).places.find((x) => x.place === m.rec.place); labelDialog({ recId: m.rec.id, title: m.rec.title || m.rec.filename, place: m.rec.place, nPlace: pl ? pl.n : 1, label: m.rec.label }, () => openPolish(id)); };
  $("#p-ready").onclick = () => savePolish("ready");
  const wid = $("#p-widen"); if (wid) wid.onclick = async () => { await flushPolishSave(); await api(`/api/moment/${id}/range`, m.ears.widen); toast("Widened — the missing line is in"); openPolish(id); };
  const wpl = $("#p-wide-play"); if (wpl) wpl.onclick = () => m.clip && audio.play(m.rec.id, Math.max(0, m.clip.start - 10), m.clip.end + 10);
  const und = $("#p-undoclean"); if (und) und.onclick = async () => { await flushPolishSave(); await api(`/api/moment/${id}/undoclean`, {}); toast("Put back as it was"); openPolish(id); };
  $("#p-back").onclick = async () => { await api(`/api/moment/${id}/triage`, { decision: "pending" }); toast("Back in triage"); loadPolish(); refreshStatus(); };
  $("#p-rej").onclick = async () => { await api(`/api/moment/${id}/triage`, { decision: "rejected" }); toast("Rejected"); loadPolish(); refreshStatus(); };
  $("#p-eb").onclick = async () => { await api(`/api/moment/${id}/range`, { before: 1, after: 0 }); openPolish(id); };
  $("#p-ea").onclick = async () => { await api(`/api/moment/${id}/range`, { before: 0, after: 1 }); openPolish(id); };
  $("#p-add").onclick = () => {
    const opts = [`<option value="">at the start</option>`].concat(m.lines.map((l, k) =>
      `<option value="${l.seg_id}" ${k === m.lines.length - 1 ? "selected" : ""}>after: ${esc((l.text || "").slice(0, 40))}</option>`));
    $("#p-addform").innerHTML = `<div class="n-add">
      <select id="pa-spk">${SPK_CYCLE.map((k) => `<option value="${k}">${EMOJI[k]} ${esc(NAMES[k])}</option>`).join("")}</select>
      <input type="text" id="pa-text" placeholder="what was said">
      <select id="pa-pos">${opts.join("")}</select>
      <button class="btn primary" id="pa-save">Add</button></div>`;
    $("#pa-text").focus();
    const save = async () => {
      const text = $("#pa-text").value.trim(); if (!text) return;
      await savePolish();  // keep edits made so far
      await api(`/api/moment/${id}/addline`, { text, speaker: $("#pa-spk").value, after_seg_id: +$("#pa-pos").value || null });
      toast("Line added ✓"); openPolish(id);
    };
    $("#pa-save").onclick = save;
    $("#pa-text").onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) save(); };
  };
  ["#p-npub", "#p-star"].forEach((s) => $(s).addEventListener("input", updatePreview));
  $("#p-main").addEventListener("input", schedulePolishSave);
  $("#p-main").addEventListener("change", schedulePolishSave);
  updatePreview();
  if (m.clip) audio.play(m.rec.id, m.clip.start, m.clip.end);
  renderAudioBar();
  if (busy) P.poll = setTimeout(() => P.sel === id && current === "polish" && openPolish(id), 2500);
}
function polishLine(l, k) {
  const alts = [["auto", "heard"], ["relisten", "2nd listen"], ["zh", "中文"], ["en", "EN"]].filter(([key]) => l.alts[key] !== undefined && l.alts[key] !== "");
  return `<div class="pline ${l.include ? "" : "off"}" data-id="${l.id}" data-k="${k}">
    <div class="head">
      <input type="checkbox" class="inc" ${l.include ? "checked" : ""} title="include in journal">
      <select class="spk-sel">${SPK.map((s) => `<option value="${s}" ${s === l.speaker ? "selected" : ""}>${EMOJI[s]} ${esc(NAMES[s])}</option>`).join("")}</select>
      <button class="play" title="play line">▶</button><button type="button" class="fromhere" title="play from this line to the end of the card">⏵</button><button type="button" class="loopbtn" title="repeat this line until you click again">🔁</button>${l.seg_id ? addBtn(l.seg_id) : ""}<span class="spacer"></span>
      <button class="btn small snd" title="add a sound — giggles, crying…">🔊</button>
      <button class="btn small echo" title="she (or you) repeated this — copy it below as the other person">↻ echo</button>
      <button class="btn small rr" title="suggest a fresh translation, using the whole conversation">↻ translate</button>
    </div>
    <input type="text" class="text" value="${esc(l.text)}" title="Several people in one line? Type / between turns (d: m: c: picks who), then Enter" placeholder="Several people? Type / between turns, e.g.  yes! / d: what? then Enter">
    ${vibeBar()}${l.dup_of ? `<button type="button" class="dupchip" data-hidel title="same moment of audio as another line">⧉ ${l.dup_kind === "translation" ? "same line in the other language" : "duplicate of a line above"} — hide</button>` : ""}
    <div class="tr-row" ${mostlyChinese(l.text) ? "" : "hidden"}><span class="k">English</span><input type="text" class="tr" value="${esc(trOf(l.rendered))}" placeholder="translation — type your own, or ↻ for a suggestion"><button type="button" class="btn ghost small tr-more" title="two more ways to translate it">⋯ more</button></div><div class="tr-alts" hidden></div>
    <div class="snd-row" hidden>${soundChips("")}</div>
    ${alts.length > 1 ? `<div class="alts">${alts.map(([key, lab]) => {
      const p = l.alts.p?.[key], unlikely = key !== "auto" && p != null && p < 0.1;
      return `<div class="alt ${l.alts[key] === l.text || unlikely ? "same" : ""}" data-alt="${esc(l.alts[key])}" title="${p != null ? `whisper thinks this audio is ${Math.round(p * 100)}% ${lab}` : ""}"><span class="k">${lab}${p != null && key !== "auto" ? ` ${Math.round(p * 100)}%` : ""}</span><span>${esc(l.alts[key])}${unlikely ? ` <i class="faint small">— unlikely, probably a translation</i>` : ""}</span></div>`;
    }).join("")}</div>` : ""}
    <details class="md"><summary>formatting (automatic)</summary><textarea class="rendered" rows="2">${esc(l.rendered)}</textarea></details>
  </div>`;
}
// house-style bits the page needs to know: is a line Chinese (→ translation box), and its current translation
function mostlyChinese(t) {
  const clean = (t || "").replace(/\[[^\]]*\]|\{[^}]*\}|[<《【(（][^>》】)）]*[>》】)）]/g, " ");
  const zh = (clean.match(/[\u3400-\u9fff]/g) || []).length, en = (clean.match(/[A-Za-z]+/g) || []).length;
  return zh > 0 && en <= Math.max(1, Math.floor(zh / 6));
}
const trOf = (rendered) => ((rendered || "").match(/"\s*\*\((.+?)\)\*/) || [])[1] || "";
function wirePolishLine(el) {
  const l = P.cur.lines[+el.dataset.k];
  el._lastText = (l.text || "").trim(); el._trTyped = l.edited && !!trOf(l.rendered);
  $(".inc", el).onchange = (e) => { el.classList.toggle("off", !e.target.checked); updatePreview(); };
  $(".play", el).onclick = () => l.start != null && audio.play(P.cur.rec.id, Math.max(0, l.start - 0.3), l.end + 0.4);
  wireLineAudio(el, P.cur.rec.id, l, P.cur.clip && P.cur.clip.end);
  $(".rr", el).onclick = () => { el._trTyped = false; rerender(el, false, true); };
  const tr = $(".tr", el);
  tr.addEventListener("input", () => { el._trTyped = true; if (tr.value.trim()) rerenderSoon(el); });
  tr.addEventListener("blur", () => { if (!tr.value.trim()) rerender(el); });
  tr.onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); rerender(el); } };
  $(".text", el).addEventListener("input", (e) => { $(".tr-row", el).hidden = !mostlyChinese(e.target.value); });
  $(".snd", el).onclick = () => { const r = $(".snd-row", el); r.hidden = !r.hidden; $(".text", el).focus(); };
  wireSounds(el, $(".text", el), () => rerenderSoon(el));
  // keep the preview in step while you type (re-renders ~0.5 s after you pause; Enter still does it at once)
  $(".text", el).addEventListener("input", () => rerenderSoon(el));
  $(".spk-sel", el).onchange = () => rerender(el, true);
  const polishParts = () => l.seg_id ? splitParts($(".text", el).value, $(".spk-sel", el).value, partnerOf(P.cur.segments || [])) : null;
  const doSplit = async (parts) => {
    await flushPolishSave();
    await api(`/api/moment/${P.cur.id}/split`, { seg_id: l.seg_id, parts, via: "polish-typed" });
    toast(`Split into ${parts.length} lines ✂`); openPolish(P.cur.id);
  };
  const hintBox = document.createElement("div"); hintBox.className = "split-prev-host"; $(".text", el).after(hintBox);
  const showHint = () => {
    const parts = polishParts();
    hintBox.innerHTML = parts ? `<div class="split-prev">✂ This will become ${parts.length} lines: ${parts.map((p) => `<span class="spk ${p.speaker}">${EMOJI[p.speaker] || ""}</span> ${esc(p.text)}`).join(" <span class='faint'>·</span> ")} <button type="button" class="btn primary small" data-dosplit>Split now ↵</button></div>` : "";
    const b = hintBox.querySelector("[data-dosplit]"); if (b) b.onclick = () => doSplit(parts);
  };
  $(".text", el).addEventListener("input", showHint); showHint();
  $(".text", el).onkeydown = (e) => { if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); const parts = polishParts(); if (parts) doSplit(parts); else rerender(el); } };
  $(".rendered", el).oninput = updatePreview;
  $$(".alt", el).forEach((a) => (a.onclick = () => { $(".text", el).value = a.dataset.alt; rerender(el); }));
  // power tools
  const txt = $(".text", el);
  $$(".vb", el).forEach((b) => (b.onclick = () => { txt.value = vibe(txt.value, b.dataset.vibe); el._lastText = undefined; rerender(el); }));
  $(".echo", el).onclick = async () => {
    if (!l.seg_id) return toast("Can't echo a typed-in line");
    await flushPolishSave();
    await api(`/api/moment/${P.cur.id}/addline`, { text: txt.value.trim(), speaker: otherSpeaker($(".spk-sel", el).value, P.cur.segments), after_seg_id: l.seg_id, via: "echo" });
    toast("Echo added ↻"); openPolish(P.cur.id);
  };
  const hide = $("[data-hidel]", el);
  if (hide) hide.onclick = async () => { await flushPolishSave(); await api(`/api/moment/${P.cur.id}/hide`, { seg_id: l.seg_id, via: "polish" }); toast("Hidden ✓"); openPolish(P.cur.id); };
  $(".tr-more", el).onclick = async () => {
    const box = $(".tr-alts", el); box.hidden = false; box.innerHTML = `<span class="spinner"></span> thinking of other ways to say it…`;
    const r = await api(`/api/moment/${P.cur.id}/translate_alts`, { line_id: l.id, text: txt.value.trim() });
    box.innerHTML = r.alts.length ? r.alts.map((a) => `<button type="button" class="alt-pick" data-a="${esc(a)}">${esc(a)}</button>`).join("") : `<span class="faint small">No other suggestions.</span>`;
    box.querySelectorAll("[data-a]").forEach((b) => (b.onclick = () => { $(".tr", el).value = b.dataset.a; el._trTyped = true; box.hidden = true; rerender(el); }));
  };
  const trySplit = () => {
    const a = txt.selectionStart, z = txt.selectionEnd;
    if (z <= a || !l.seg_id || !txt.value.slice(a, z).trim()) return;
    splitPopup(txt.getBoundingClientRect(), $(".spk-sel", el).value, async (spk) => {
      const parts = partsFromSelection(txt.value, a, z - a, $(".spk-sel", el).value, spk);
      if (parts.length < 2) { $(".spk-sel", el).value = spk; return rerender(el, true); }
      await flushPolishSave();
      await api(`/api/moment/${P.cur.id}/split`, { seg_id: l.seg_id, parts, via: "polish-select" });
      toast(`Split into ${parts.length} lines ✂`); openPolish(P.cur.id);
    });
  };
  txt.addEventListener("mouseup", trySplit);
  txt.addEventListener("keyup", (e) => { if (e.shiftKey) trySplit(); });
}
let _rrT = null;
function rerenderSoon(el) {  // Chinese lines may fetch a fresh translation, so wait a little longer for those
  clearTimeout(_rrT);
  _rrT = setTimeout(() => rerender(el), mostlyChinese($(".text", el).value) ? 1200 : 500);
}
async function rerender(el, speakerOnly, retranslate) {
  const text = $(".text", el).value.trim(), speaker = $(".spk-sel", el).value;
  const ta = $(".rendered", el), tr = $(".tr", el);
  if (speakerOnly) { ta.value = ta.value.replace(/^\S+/, EMOJI[speaker]); updatePreview(); schedulePolishSave(); return; }
  // the words changed and you haven't written this translation yourself → fresh one; yours is never overwritten
  const fresh = retranslate || (!el._trTyped && el._lastText !== undefined && el._lastText !== text);
  el._lastText = text;
  if (fresh) tr.value = ""; if (fresh || !tr.value.trim()) tr.placeholder = "translating…";
  const r = await api(`/api/moment/${P.cur.id}/render`, { line_id: +el.dataset.id, text, speaker,
    translation: tr.value, retranslate: !!fresh });
  ta.value = r.rendered; if (r.translation !== undefined && (fresh || !tr.value.trim())) tr.value = r.translation;
  tr.placeholder = "translation — type your own, or ↻ for a suggestion";
  updatePreview(); schedulePolishSave();
}
function collectPolish() {
  return {
    lines: $$("#p-lines .pline").map((el) => ({ id: +el.dataset.id, include: $(".inc", el).checked, speaker: $(".spk-sel", el).value,
      text: $(".text", el).value.trim(), rendered: $(".rendered", el).value.trim() })),
    note_public: $("#p-npub").value.trim(), note_private: $("#p-npriv").value.trim(),
    entry_date: $("#p-date").value, star: $("#p-star").checked ? 1 : 0, audio_off: $("#p-audio").checked ? 0 : 1,
  };
}
function mdPreview(b) {
  const lines = b.lines.filter((l) => l.include && l.rendered).map((l) => l.rendered);
  if (!lines.length) return "";
  let note = b.note_public.replace(/^[\[\*\s]+|[\]\*\s]+$/g, "");
  if (note && !/[.!?。]$/.test(note)) note += ".";
  if (note) lines[lines.length - 1] += ` *[${note}]*`;
  if (b.star) lines[0] = "⭐ " + lines[0];
  return lines.join("\n\n");
}
function updatePreview() {
  const md = mdPreview(collectPolish());
  const hint = $("#p-hint");
  if (hint) hint.hidden = !/\{/.test(md);
  $("#p-prev").innerHTML = md.split("\n").filter(Boolean).map((ln) => `<p>${esc(ln).replace(/\*\[(.+?)\]\*/g, "<em>[$1]</em>").replace(/\*\((.+?)\)\*/g, "<em>($1)</em>").replace(/(\{[^{}]*[}\]]?)/g, '<mark class="todo" title="note to yourself — not published until resolved">$1</mark>')}</p>`).join("") || `<span class="faint">No lines included</span>`;
}
let polishTimer = null, polishDirty = false;
function schedulePolishSave() {
  if (!P.cur) return;
  polishDirty = true;
  const el = $("#p-saved"); if (el) { el.textContent = "Saving…"; el.classList.add("pending"); }
  clearTimeout(polishTimer);
  polishTimer = setTimeout(flushPolishSave, 600);
}
async function flushPolishSave() {
  clearTimeout(polishTimer);
  if (!polishDirty || !P.cur || !$("#p-lines")) return;
  polishDirty = false;
  try {
    await api(`/api/moment/${P.cur.id}/save`, { ...collectPolish(), quiet: true });
    const el = $("#p-saved"); if (el) { el.textContent = `Saved ✓ ${new Date().toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" })}`; el.classList.remove("pending"); }
  } catch (e) { polishDirty = true; const el = $("#p-saved"); if (el) el.textContent = "⚠ Not saved — retrying"; setTimeout(flushPolishSave, 2000); }
}
window.addEventListener("hashchange", flushPolishSave, true);
window.addEventListener("beforeunload", (e) => { if (polishDirty) { flushPolishSave(); e.preventDefault(); } });
async function savePolish(status) {
  if (!P.cur) return;
  await flushPolishSave();
  const b = collectPolish();
  if (status === "ready") {
    const pending = $$("#p-lines .pline").filter((el) => el.querySelector(".split-prev-host .split-prev"));
    if (pending.length) { pending[0].scrollIntoView({ block: "center" }); return toast(`A line still has a "/" turn marker — press "Split now" on it first (it would publish as a literal slash)`); }
    if (!mdPreview(b)) return toast("Include at least one line");
    if (/\{/.test(mdPreview(b))) return toast("Resolve the {…} notes first — they're notes to yourself and would be published");
    if (b.lines.some((l) => l.include && /\*\(\s*\)\*|…$/.test(l.rendered))) return toast("A line still has an empty translation");
  }
  await api(`/api/moment/${P.cur.id}/save`, { ...b, status });
  toast(status === "ready" ? "Ready to publish ✓" : "Saved");
  if (status === "ready") { const i = P.items.findIndex((m) => m.id === P.cur.id); P.sel = P.items[i + 1]?.id || P.items[i - 1]?.id; loadPolish(); refreshStatus(); }
}

/* ================================================================== PUBLISH */
views.publish = async () => {
  const [ready, pub] = await Promise.all([api("/api/queue?stage=publish&sort=date&limit=500"), api("/api/queue?stage=published&sort=date&limit=30")]);
  const byDate = {};
  ready.items.forEach((m) => { const d = m.entry_date || m.rec.recorded_at.slice(0, 10); (byDate[d] = byDate[d] || []).push(m); });
  $("#view").innerHTML = `
    <div class="row"><h2 style="margin:0">Ready to publish</h2><span class="spacer"></span>
      <button class="btn big primary" id="pub-go" ${ready.items.length ? "" : "disabled"}>Write ${ready.items.length} moment(s) to the journal</button></div>
    <p class="muted small">Writes house-style markdown into <code>family-journal/src/content/entries/&lt;year&gt;/&lt;date&gt;.md</code> (audience: family). Each moment sits between <code>&lt;!-- vjs:m… --&gt;</code> markers, so editing and re-publishing a moment replaces it instead of duplicating it. Nothing is deployed from here.</p>
    <div id="pub-res"></div>
    ${Object.keys(byDate).sort().map((d) => `<h3>${d}</h3>` + byDate[d].map((m) => `
      <div class="panel" style="margin-bottom:8px"><div class="row"><div class="preview pretty" style="flex:1;border:0;background:none;padding:0">${prettyMd(m.preview)}</div>
      <button class="btn small" data-edit="${m.id}">Edit</button></div></div>`).join("")).join("") || `<div class="empty"><p>Nothing ready yet — finish moments in Polish.</p></div>`}
    ${pub.items.length ? `<h3 class="muted">Recently published</h3>${pub.items.map((m) => `<div class="small muted" style="margin:4px 0">${m.entry_date || m.rec.recorded_at.slice(0, 10)} — ${esc((m.preview || "").split("\n")[0]).slice(0, 120)}</div>`).join("")}` : ""}`;
  $$("[data-edit]").forEach((b) => (b.onclick = async () => { await api(`/api/moment/${b.dataset.edit}/save`, { status: "journal" }); P.sel = +b.dataset.edit; location.hash = "#polish"; }));
  $("#pub-go") && ($("#pub-go").onclick = async () => {
    $("#pub-go").disabled = true; $("#pub-res").innerHTML = `<p class="muted small"><span class="spinner"></span>Publishing…</p>`;
    let r;
    try { r = await api("/api/publish", {}); }
    catch (e) { $("#pub-res").innerHTML = `<div class="panel warn-hint" style="margin:12px 0"><b>Publish failed</b> — nothing was lost; the quotes are still Ready. ${esc(String(e))}</div>`; $("#pub-go").disabled = false; return; }
    if (!r.written.length) { $("#pub-res").innerHTML = `<div class="panel" style="margin:12px 0">Nothing was published — moments with a {note} still in them go back to Polish.</div>`; $("#pub-go").disabled = false; refreshStatus(); return; }
    const files = [...new Set(r.written.map((w) => w.path))];
    $("#pub-res").innerHTML = `<div class="panel" style="margin:12px 0"><b>Wrote ${r.written.length} moment(s) into ${files.length} file(s):</b>
      <ul class="small">${files.map((f) => `<li><code>${esc(f.split("/entries/")[1] || f)}</code></li>`).join("")}</ul>
      <p class="small muted">Next: preview with <code>npm run dev</code> in family-journal, then build and deploy the way you normally do. A copy of what was added is saved in <code>voice-journal-studio/data/exports/</code> for the master diary doc.</p></div>`;
    refreshStatus(); setTimeout(views.publish, 4000);
  });
};
const prettyMd = (md) => (md || "").split("\n").filter(Boolean).map((ln) => `<p>${esc(ln).replace(/\*\[(.+?)\]\*/g, "<em>[$1]</em>").replace(/\*\((.+?)\)\*/g, "<em>($1)</em>")}</p>`).join("");

/* ================================================================== LOG */
const L = { tier: store.get("tier", "all"), flags: [], fi: 0, hideCommon: store.get("hideCommon", true) };
const COMMON = new Set(("i you he she it we they me my your his her its our their a an the and or but so if to of in on at for with from by " +
  "is am are was were be been do does did have has had not no yes yeah okay ok oh that that's this these those what it's i'm don't can will " +
  "just like um uh gonna wanna go get got there here then now up out one").split(" ").concat(
  ["的", "了", "是", "啊", "吗", "呢", "吧", "我", "你", "他", "她", "它", "这", "那", "这个", "那个", "就", "也", "都", "在", "有", "不", "嗯", "哦", "呀", "一个", "就是", "对"]));
views.log = async () => {
  $("#view").innerHTML = `<p class="muted">Crunching…</p>`;
  const [s, w, f] = await Promise.all([api("/api/stats?tier=" + L.tier), api("/api/watch"), api("/api/usage?status=suggested")]);
  L.flags = f.items; L.fi = 0;
  const ms = s.months, lastM = ms[ms.length - 1] || {};
  const allUtts = ms.reduce((a, m) => a + m.utts, 0);
  const mixedPct = allUtts ? Math.round(100 * ms.reduce((a, m) => a + m.mixed, 0) / allUtts) : 0;
  const lastCorrect = [...ms].reverse().find((m) => m.correct_pct != null);
  $("#view").innerHTML = `
    <div class="row"><h2 style="margin:0">Language log</h2><span class="spacer"></span>
      <select id="tier"><option value="all">All her speech (auto + verified)</option><option value="verified">Verified by you only</option></select></div>
    <p class="muted small">"Auto" = segments whose voice pitch says it's her and whisper was confident; nobody checked them. "Verified" = lines you confirmed in Triage/Polish. Usage flags come from the local model and count once you haven't dismissed them.</p>
    <div class="tiles">
      <div class="tile"><div class="v">${s.totals.utts.toLocaleString()}</div><div class="l">utterances (${s.totals.verified} verified)</div></div>
      <div class="tile"><div class="v">${lastM.mlu ?? "–"}</div><div class="l">words per utterance, ${lastM.month || ""}</div></div>
      <div class="tile"><div class="v">${mixedPct}%</div><div class="l">utterances that code-switch</div></div>
      <div class="tile"><div class="v">${lastCorrect ? lastCorrect.correct_pct + "%" : "–"}</div><div class="l">adult-standard usage${lastCorrect ? ", " + lastCorrect.month : ""} (${s.totals.checked} checked)</div></div>
    </div>
    <h3>Utterances per month by language</h3>
    <div class="legend"><span><i style="background:var(--dad)"></i>English</span><span><i style="background:var(--kid)"></i>Mandarin</span><span><i style="background:var(--mom)"></i>Mixed</span></div>
    <div class="chart">${barChart(ms)}</div>
    <div class="two">
      <div><h3>Words per utterance (MLU)</h3><div class="chart">${lineChart(ms, "mlu", "")}</div></div>
      <div><h3>Adult-standard usage %</h3><div class="chart">${lineChart(ms, "correct_pct", "%")}</div></div>
    </div>
    <h3>Watched words</h3>
    <div class="row" style="margin-bottom:10px"><input type="text" id="ww" placeholder="add a word, e.g. went, me, 的, 因为"><button class="btn" id="ww-add">Watch</button></div>
    <div class="watch-grid">${Object.entries(s.watch).map(([word, series]) => `<div class="tile"><div class="row"><b>${esc(word)}</b><span class="spacer"></span><span class="faint small">${series.reduce((a, x) => a + x.n, 0)}×</span><button class="play" data-rm="${esc(word)}" title="stop watching">✕</button></div>${spark(series)}</div>`).join("") || `<span class="faint small">No watched words yet.</span>`}</div>
    <h3>Usage flags to check <span class="faint small">(${L.flags.length} shown) — <kbd>Y</kbd> real · <kbd>N</kbd> not an error · <kbd>P</kbd> play · <kbd>↑</kbd><kbd>↓</kbd></span></h3>
    <div id="flags"></div>
    <h3>Kinds of non-standard usage</h3>
    <table><tr><th>Language</th><th>Kind</th><th class="num">Count</th></tr>${s.kinds.map((k) => `<tr><td>${k.lang}</td><td>${esc(k.kind)}</td><td class="num">${k.n}</td></tr>`).join("") || `<tr><td colspan=3 class="faint">None yet</td></tr>`}</table>
    <div class="two">
      <div><h3>Top English words <label class="small muted" style="font-weight:400"><input type="checkbox" id="hidec" ${L.hideCommon ? "checked" : ""}> hide common words</label></h3><div class="words">${s.top.en.filter((x) => !L.hideCommon || !COMMON.has(x.w)).map((x) => `<span class="w">${esc(x.w)}<b>${x.n}</b></span>`).join("")}</div></div>
      <div><h3>Top Mandarin words</h3><div class="words">${s.top.zh.filter((x) => !L.hideCommon || !COMMON.has(x.w)).map((x) => `<span class="w">${esc(x.w)}<b>${x.n}</b></span>`).join("")}</div></div>
    </div>
    <h3>First heard, by month</h3>
    ${Object.entries(s.new_words).reverse().map(([m, ws]) => `<div style="margin-bottom:12px"><div class="small muted">${m} · ${ws.length}${ws.length >= 80 ? "+" : ""} new</div><div class="words">${ws.slice(0, 50).map((x) => `<span class="w" title="${esc(x.ex)}">${esc(x.w)}</span>`).join("")}</div></div>`).join("") || `<span class="faint small">Not enough data yet.</span>`}
    <h3>Monthly table</h3>
    <table><tr><th>Month</th><th class="num">Utterances</th><th class="num">Verified</th><th class="num">EN</th><th class="num">中文</th><th class="num">Mixed</th><th class="num">MLU</th><th class="num">New words</th><th class="num">Standard %</th></tr>
      ${[...ms].reverse().map((m) => `<tr><td>${m.month}</td><td class="num">${m.utts}</td><td class="num">${m.verified}</td><td class="num">${m.en}</td><td class="num">${m.zh}</td><td class="num">${m.mixed}</td><td class="num">${m.mlu}</td><td class="num">${m.new_words}</td><td class="num">${m.correct_pct ?? "–"}</td></tr>`).join("")}</table>`;
  $("#tier").value = L.tier;
  $("#hidec").onchange = (e) => { L.hideCommon = e.target.checked; store.set("hideCommon", L.hideCommon); views.log(); };
  $("#tier").onchange = (e) => { L.tier = e.target.value; store.set("tier", L.tier); views.log(); };
  $("#ww-add").onclick = async () => { const v = $("#ww").value.trim(); if (v) { await api("/api/watch", { word: v }); views.log(); } };
  $("#ww").onkeydown = (e) => e.key === "Enter" && !e.isComposing && $("#ww-add").click();
  $$("[data-rm]").forEach((b) => (b.onclick = async () => { await api("/api/watch", { word: b.dataset.rm, remove: true }); views.log(); }));
  renderFlags();
  document.onkeydown = (e) => {
    if (/INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.metaKey || e.ctrlKey) return;
    const f = L.flags[L.fi]; if (!f) return;
    if (e.key === "y") judgeFlag("confirmed"); if (e.key === "n") judgeFlag("dismissed");
    if (e.key === "p") audio.play(f.rec_id, Math.max(0, f.start - 0.3), f.end + 0.4);
    if (e.key === "ArrowDown") { e.preventDefault(); L.fi = Math.min(L.flags.length - 1, L.fi + 1); renderFlags(); }
    if (e.key === "ArrowUp") { e.preventDefault(); L.fi = Math.max(0, L.fi - 1); renderFlags(); }
  };
};
function renderFlags() {
  $("#flags").innerHTML = L.flags.slice(0, 12).map((f, i) => {
    const hl = esc(f.text).replace(esc(f.span), `<mark>${esc(f.span)}</mark>`);
    return `<div class="flag ${i === L.fi ? "focus" : ""}" data-i="${i}"><div class="row"><span style="font-size:16px">${hl}</span><span class="spacer"></span>
      <button class="play" data-p>▶</button><button class="btn small" data-y>Real</button><button class="btn small" data-n>Not an error</button></div>
      <div class="small muted">${esc(f.kind)} · adult form: <b>${esc(f.correct)}</b> — ${esc(f.explanation)} <span class="faint">· ${f.recorded_at.slice(0, 10)}</span></div></div>`;
  }).join("") || `<span class="faint small">Nothing to check.</span>`;
  $$("#flags .flag").forEach((el) => {
    const i = +el.dataset.i, f = L.flags[i];
    $("[data-p]", el).onclick = () => audio.play(f.rec_id, Math.max(0, f.start - 0.3), f.end + 0.4);
    $("[data-y]", el).onclick = () => { L.fi = i; judgeFlag("confirmed"); };
    $("[data-n]", el).onclick = () => { L.fi = i; judgeFlag("dismissed"); };
  });
}
async function judgeFlag(status) {
  const f = L.flags[L.fi]; if (!f) return;
  await api(`/api/usage/${f.id}`, { status });
  L.flags.splice(L.fi, 1); L.fi = Math.min(L.fi, L.flags.length - 1); renderFlags();
  toast(status === "confirmed" ? "Confirmed" : "Dismissed");
}
function barChart(ms) {
  if (!ms.length) return `<p class="faint small">No data yet.</p>`;
  const W = 900, H = 200, pad = 28, bw = Math.max(4, (W - pad * 2) / ms.length - 4);
  const max = Math.max(...ms.map((m) => m.en + m.zh + m.mixed), 1);
  const y = (v) => (H - pad) * v / max;
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Utterances per month">
    ${ms.map((m, i) => { const x = pad + i * (bw + 4); let top = H - pad; return [["en", "var(--dad)"], ["zh", "var(--kid)"], ["mixed", "var(--mom)"]].map(([k, c]) => { const h = y(m[k]); top -= h; return `<rect x="${x}" y="${top}" width="${bw}" height="${h}" fill="${c}" rx="1.5"><title>${m.month} ${k}: ${m[k]}</title></rect>`; }).join("") +
      (ms.length <= 24 || i % 3 === 0 ? `<text x="${x + bw / 2}" y="${H - 10}" text-anchor="middle">${m.month.slice(2)}</text>` : ""); }).join("")}
    <text x="${pad - 6}" y="12" text-anchor="end">${max}</text></svg>`;
}
function lineChart(ms, key, unit) {
  const pts = ms.map((m, i) => [i, m[key]]).filter(([, v]) => v != null && v !== 0);
  if (pts.length < 2) return `<p class="faint small">Needs at least two months of data.</p>`;
  const W = 440, H = 150, pad = 26;
  const vals = pts.map((p) => p[1]), lo = Math.min(...vals), hi = Math.max(...vals), span = hi - lo || 1;
  const x = (i) => pad + (W - pad * 2) * i / Math.max(1, ms.length - 1), y = (v) => H - pad - (H - pad * 2) * (v - lo) / span;
  return `<svg viewBox="0 0 ${W} ${H}"><polyline fill="none" stroke="var(--accent)" stroke-width="2" points="${pts.map(([i, v]) => `${x(i)},${y(v)}`).join(" ")}"/>
    ${pts.map(([i, v]) => `<circle cx="${x(i)}" cy="${y(v)}" r="3" fill="var(--accent)"><title>${ms[i].month}: ${v}${unit}</title></circle>`).join("")}
    <text x="4" y="${y(hi) + 4}">${hi}${unit}</text><text x="4" y="${y(lo) + 4}">${lo}${unit}</text>
    <text x="${x(0)}" y="${H - 6}">${ms[0].month}</text><text x="${x(ms.length - 1)}" y="${H - 6}" text-anchor="end">${ms[ms.length - 1].month}</text></svg>`;
}
function spark(series) {
  const W = 190, H = 40, max = Math.max(1, ...series.map((s) => s.n)), bw = W / Math.max(1, series.length);
  return `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:40px">${series.map((s, i) => `<rect x="${i * bw + 1}" y="${H - H * s.n / max}" width="${Math.max(1, bw - 2)}" height="${H * s.n / max}" fill="var(--accent)" opacity=".8"><title>${s.month}: ${s.n}</title></rect>`).join("")}</svg>`;
}

/* ================================================================== SEARCH */
views.search = async () => {
  $("#view").innerHTML = `
    <h2 style="margin-top:0">Find a moment you remember</h2>
    <div class="row"><input type="search" id="q" placeholder="words she said, e.g. ladybug, 晒太阳, Cybertruck" style="flex:1;font-size:16px" autofocus>
      <label class="small muted"><input type="checkbox" id="kidonly" checked> her lines only</label></div>
    <div id="res" style="margin-top:14px"></div>`;
  let t;
  const run = async () => {
    const q = $("#q").value.trim();
    if (!q) { $("#res").innerHTML = ""; return; }
    const r = await api(`/api/search?q=${encodeURIComponent(q)}&kid=${$("#kidonly").checked ? 1 : 0}`);
    const recHits = (r.recordings || []).map((x) => `<div class="rl-row"><span class="rl-d">${esc(fmtDate(x.recorded_at).split(" · ")[0])}</span><span class="rl-t">${esc(x.title || "recording")} <span class="pill">${esc(x.kind || "")}</span> ${labelChips({ note: x.note, tags: JSON.parse(x.tags || "[]") })}
      <div class="faint small">${esc(x.summary || "")}</div></span><span class="faint small">${fmtT(x.duration || 0)}</span><a href="#reader?rec=${x.id}">open in reader →</a></div>`).join("");
    $("#res").innerHTML = (recHits ? `<div class="faint small">Recordings whose summary matches</div>${recHits}<div class="faint small" style="margin-top:10px">Lines</div>` : "") + r.items.map((s) => `<div class="ln" data-id="${s.id}">${spkChip(s.speaker, true, 0)}<div><div class="txt" style="font-size:16px">${esc(s.text)}</div><div class="faint small">${fmtDate(s.recorded_at)}${s.title ? " · " + esc(s.title) : ""}</div></div>
      <div class="row"><button class="play" data-p>▶</button><button class="btn small" data-ctx>Context</button><button class="btn small" data-mk>Make moment</button></div></div>`).join("") || `<p class="faint">No matches.</p>`;
    $$("#res .ln").forEach((el, i) => {
      const s = r.items[i]; if (!s) return;
      $("[data-p]", el).onclick = () => audio.play(s.rec_id, Math.max(0, s.start - 0.4), s.end + 0.6);
      $("[data-ctx]", el).onclick = () => openTranscript(s.rec_id, s.idx);
      $("[data-mk]", el).onclick = async () => { await api("/api/moment/new", { seg_id: s.id }); toast("Added to triage (top of the pile)"); refreshStatus(); };
    });
  };
  $("#q").oninput = () => { clearTimeout(t); t = setTimeout(run, 250); };
  $("#kidonly").onchange = run;
};

/* ================================================================== RECORDINGS */
views.recordings = async () => {
  const r = await api("/api/recordings");
  const s = await refreshStatus();
  $("#view").innerHTML = `
    <div class="row"><h2 style="margin:0">Recordings</h2><span class="spacer"></span>
      <button class="btn primary" id="run" ${s?.running ? "disabled" : ""}>${s?.running ? "Pipeline running…" : "Check for new recordings & process"}</button></div>
    <p class="muted small">New voice memos: drop them in <code>~/VoiceMemosInbox</code> (or the Recordings folder) and press the button — or run <code>./vjs.sh run</code>. Newest recordings are processed first.</p>
    <table><tr><th>Recorded</th><th>Place / title</th><th class="num">Length</th><th>Status</th><th class="num">Her lines</th><th class="num">Moments</th><th></th></tr>
    ${r.items.map((x) => `<tr><td>${fmtDate(x.recorded_at)}</td><td class="small">${esc(x.title || x.filename)}</td><td class="num">${x.duration ? Math.round(x.duration / 60) + "m" : ""}</td>
      <td class="small ${x.status === "error" ? "" : "muted"}" title="${esc(x.error || "")}">${x.status}${x.status === "error" ? " ⚠" : ""}${x.category ? ` · ${esc(x.category)}` : x.prior ? ` · <span class="faint">${esc(x.prior)}?</span>` : ""}</td><td class="num">${x.kid_segments || ""}</td>
      <td class="num">${x.moments ? `${x.pending ? `<a href="#triage" data-rec="${x.id}">${x.pending} new</a> / ` : ""}${x.moments}` : ""}</td>
      <td>${["analyzed", "mined"].includes(x.status) ? `<a href="#" data-tr="${x.id}">transcript</a>` : ""}</td></tr>`).join("")}</table>`;
  $("#run").onclick = async () => { await api("/api/pipeline/start", {}); toast("Started"); setTimeout(views.recordings, 1500); };
  $$("[data-rec]").forEach((a) => (a.onclick = () => { T.rec = a.dataset.rec; T.opts.stage = "triage"; T.opts.min = 0; }));
  $$("[data-tr]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openTranscript(a.dataset.tr); }));
};

/* ------------------------------------------------------------------ transcript modal */
async function openTranscript(rec, aroundIdx) {
  const r = await api(`/api/transcript?rec=${rec}` + (aroundIdx != null ? `&from=${aroundIdx - 40}&to=${aroundIdx + 40}` : ""));
  const wrap = document.createElement("div");
  wrap.className = "modal";
  wrap.innerHTML = `<div class="panel"><div class="row"><b>${fmtDate(r.rec.recorded_at)}</b><span class="muted small">${esc(r.rec.title || "")}</span><span class="spacer"></span><button class="btn" data-close>Close <kbd>Esc</kbd></button></div>
    <p class="faint small">Click ▶ to hear a line, ＋ to turn it (and its neighbours) into a moment for triage.</p>
    ${r.segments.map((s) => `<div class="ln ${s.idx === aroundIdx ? "focus" : ""}" data-i="${s.idx}">${spkChip(s.speaker, !s.spk, s.spk_conf)}<div class="txt" style="font-size:15.5px"><span class="faint small">${fmtT(s.start)}</span> ${esc(s.text)}</div>
      <div class="row"><button class="play" data-p="${s.id}">▶</button><button class="play" data-mk="${s.id}" title="make moment">＋</button></div></div>`).join("")}</div>`;
  document.body.appendChild(wrap);
  const prevKeys = document.onkeydown;
  const close = () => { wrap.remove(); document.onkeydown = prevKeys; };
  document.onkeydown = (e) => { if (e.key === "Escape") close(); if (e.key === " ") { e.preventDefault(); audio.toggle(); } };
  wrap.onclick = (e) => { if (e.target === wrap) close(); };
  $("[data-close]", wrap).onclick = close;
  $$("[data-p]", wrap).forEach((b) => (b.onclick = () => { const s = r.segments.find((x) => x.id === +b.dataset.p); audio.play(rec, Math.max(0, s.start - 0.3), s.end + 0.4); }));
  $$("[data-mk]", wrap).forEach((b) => (b.onclick = async () => { await api("/api/moment/new", { seg_id: +b.dataset.mk }); toast("Added to triage"); refreshStatus(); }));
  $(".ln.focus", wrap)?.scrollIntoView({ block: "center" });
}

refreshStatus().then(route);
renderSpeedPill();

/* answers for the quick transcript checks (used by Daily 5) */
const CHECK_ANSWERS = {
  foreign: [["noise", "Noise / gibberish", "good"], ["media", "Song · TV · video", "good"], ["her", "Her, really talking", "bad"], ["adult", "An adult, really talking", "bad"]],
  kid: [["her", "Yes, it's her", "good"], ["adult", "No, an adult", "bad"], ["media", "TV / audiobook", "bad"], ["noise", "Can't tell / noise", "meh"]],
  kept: [["ok", "Yes, roughly right", "good"], ["badtext", "Real speech, wrong words", "meh"], ["junk", "Noise / TV / made up", "bad"]],
};
const SPK_CYCLE = ["kid", "dad", "mom", "other"];

/* ================================================================== LIBRARY — the whole archive at a glance */
views.library = async () => {
  setTimeout(async () => {
    const box = $("#corpus"); if (!box) return;
    const c = await api("/api/corpus");
    const byM = Object.fromEntries(c.months.map((m) => [m.month, m]));
    const years = [...new Set(c.months.map((m) => m.month.slice(0, 4)))].sort();
    const shade = (m) => (m.waiting + m.kept + m.log + m.rejected ? `rgba(214,120,40,${(0.08 + 0.55 * m.unreviewed).toFixed(2)})` : "transparent");
    const MN = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
    const cell = (y, k) => {
      const m = byM[`${y}-${String(k + 1).padStart(2, "0")}`];
      if (!m) return `<td class="cg-empty"></td>`;
      return `<td class="cg" data-m="${m.month}" style="background:${shade(m)}" title="${m.month}: ${m.hours} h recorded · ${m.waiting} waiting · ${m.kept} kept (${m.published} published) · ${m.log} log · ${m.rejected} rejected · piles: ${Object.entries(m.piles).filter(([, n]) => n).map(([k2, n]) => k2 + " " + n).join(", ") || "none"}">
        <b>${m.hours ? m.hours + "h" : "–"}</b><span>${m.waiting ? m.waiting + " ⏳" : "✓"}</span><span class="faint">${m.kept}★${m.published ? "/" + m.published : ""}</span><a class="cg-rd" href="#reader?month=${m.month}" title="open ${m.month} in the Reader">📖</a></td>`;
    };
    const wk = c.flow, mx = Math.max(10, ...wk.map((f) => Math.max(f.arrived, f.decided))), bw = 12;
    const bars = wk.map((f, i) => `<rect x="${i * (bw * 2 + 4)}" y="${100 - 100 * f.arrived / mx}" width="${bw}" height="${100 * f.arrived / mx}" fill="#b9b3a6"><title>${f.week}: ${f.arrived} cards arrived</title></rect>
      <rect x="${i * (bw * 2 + 4) + bw}" y="${100 - 100 * f.decided / mx}" width="${bw}" height="${100 * f.decided / mx}" fill="#2f7a5f"><title>${f.week}: you decided ${f.decided}</title></rect>`).join("");
    const e = c.eta, p = c.piles;
    const sk = await api("/api/autoskip");
    box.innerHTML = `<div class="n-q" style="text-align:left">🗺️ Corpus <span class="faint small">— am I catching up? shading = share of cards still waiting</span></div>
      <div style="overflow:auto"><table class="cgrid"><tr><th></th>${MN.map((n) => `<th>${n}</th>`).join("")}</tr>
      ${[...years].reverse().map((y) => `<tr><th>${y}</th>${MN.map((_, k) => cell(y, k)).join("")}</tr>`).join("")}</table></div>
      ${c.older_years.length ? `<details class="small muted"><summary>2019–2022 (${c.older_years.reduce((a, y) => a + y.hours, 0).toFixed(0)} h, before the date cut-off)</summary>${c.older_years.map((y) => `${y.year}: ${y.recordings} recordings, ${y.hours.toFixed(1)} h · `).join("")}</details>` : ""}
      <div class="lg-p small"><b>Filed-away piles:</b> thin ${p.thin} · no-kid ${p.no_kid} · auto-skipped ${p.auto_skipped} · later ${p.later} · merged ${p.merged} · empty ${p.empty} · ${p.recordings_skipped_by_date} recordings before the cut-off date${c.unprocessed.recordings ? ` · ⚠ ${c.unprocessed.recordings} recordings (${c.unprocessed.hours} h) not processed yet` : ""}</div>
      <div class="lg-p small">${sk.pile ? `<b>Auto-skipped pile:</b> ${sk.pile} cards (Triage → "Auto-skipped" to browse). Safety net: ${sk.sampled ? `you kept ${sk.kept} of the last ${sk.sampled} sampled (${Math.round((sk.rate || 0) * 100)}%)` : "no samples judged yet — Daily 5 slips in a few each day"}.` : "No auto-skipped cards."}
        ${sk.warn ? `<div class="warn-hint" style="margin-top:6px">⚠ <b>auto-skip may be too aggressive</b> — you keep more than 1 in 10 of the sampled cards. Consider restoring the best slice: <code>${esc(sk.suggest)}</code> (it never tightens by itself).</div>` : ""}</div>
      <div class="lg-p"><b>Catch-up (per week):</b> <span style="color:#8a8474">■ cards arriving</span> <span style="color:#2f7a5f">■ cards you decided</span></div>
      <svg viewBox="0 0 ${wk.length * (bw * 2 + 4)} 104" style="width:100%;max-width:760px;height:120px">${bars}</svg>
      <div class="lg-p">${e.waiting.toLocaleString()} waiting · you decide ~${e.decided_per_week}/week · ~${e.arriving_per_week}/week arrive. ${e.date ? `<b>At this pace you'll clear the backlog by ${e.date}</b> (${e.weeks} weeks).` : `<b>${esc(e.note)}</b>`}</div>`;
    $$("td.cg .cg-rd", box).forEach((a) => (a.onclick = (e) => e.stopPropagation()));
    $$("td.cg", box).forEach((td) => (td.onclick = () => { T.opts = { ...T.opts, stage: "triage", min: 0, month: td.dataset.m }; store.set("triageOpts2", T.opts); location.hash = "#triage"; }));
  }, 30);
  setTimeout(async () => {
    const el = $("#forecast"); if (!el) return;
    const f = await api("/api/forecast");
    el.innerHTML = `<div class="n-q" style="text-align:left">📈 Backlog forecast <span class="faint small">(replaces Quality check — from your ${f.decided} real decisions)</span></div>
      <p class="lg-p">You keep <b>${Math.round(f.keep_rate * 100)}%</b> of the cards you review (${f.kept} of ${f.decided}). The ${f.pending.toLocaleString()} waiting cards should hold about <b>${f.pending_expected_keepers}</b> keepers.</p>
      <p class="lg-p">Unprocessed audio: ${f.waiting_recordings} recordings (${f.waiting_hours} h) → expect roughly <b>${f.expected_keepers_waiting}</b> keepers (≈ ${f.expected_keepers_per_10h} per 10 h of audio, ${f.moments_per_hour} cards/h).
      Transcript spot-checks: ${f.transcripts_roughly_right.ok} of ${f.transcripts_roughly_right.of} lines "roughly right".</p>
      <p class="lg-p faint small">Go / no-go before a big batch: process ~5 h first, review the top cards in Daily 5, and if the keep rate on that sample is under about half of ${Math.round(f.keep_rate * 100)}%, pause and look at the transcripts.</p>`;
  }, 50);
  $("#view").innerHTML = `<p class="muted">Loading the archive…</p>`;
  const d = await api("/api/library");
  const t = d.totals, sg = d.segments, ms = d.moments, q = d.quality, ag = d.agreement, ed = d.edits;
  const moTotal = Object.values(ms).reduce((a, b) => a + b, 0);
  const reviewed = (ms.journal || 0) + (ms.ready || 0) + (ms.published || 0) + (ms.log || 0) + (ms.rejected || 0);
  const pct = (a, b) => (b ? Math.round((100 * a) / b) : 0);
  $("#view").innerHTML = `
  <div class="row"><h2 style="margin:0">Library</h2><span class="spacer"></span><span class="muted small">Everything recorded, what's been done with it, and how trustworthy it is.</span></div>

  <div id="corpus" class="n-card lg" style="max-width:none;margin:14px 0"><span class="faint small">Loading corpus…</span></div>
  <div id="forecast" class="n-card lg" style="max-width:none;margin:14px 0"><span class="faint small">Loading forecast…</span></div>
  <div class="funnel">
    ${funnelStep("Recorded", `${t.n} recordings`, `${t.h} h since 2019`, 100)}
    ${funnelStep("In scope", `${t.in_scope} recordings`, `${t.h_scope} h since Jun 2024`, pct(t.h_scope, t.h))}
    ${funnelStep("Transcribed", `${t.done} recordings`, `${t.h_done} h · ${t.full_pass} with full recovery`, pct(t.h_done, t.h))}
    ${funnelStep("Her speech", `${sg.kid.toLocaleString()} lines`, `pitch-guessed, junk removed`, null)}
    ${funnelStep("Moments", `${moTotal}`, `${ms.pending || 0} waiting for you`, null)}
    ${funnelStep("Reviewed", `${reviewed}`, `${(ms.journal || 0) + (ms.ready || 0)} kept · ${ms.log || 0} logged · ${ms.rejected || 0} skipped`, null)}
    ${funnelStep("Published", `${ms.published || 0}`, `in the journal`, null)}
  </div>

  <h3>The archive, month by month</h3>
  <div class="legend"><span><i style="background:var(--line)"></i>before cut-off</span><span><i style="background:var(--faint)"></i>waiting</span>
    <span><i style="background:var(--dad)"></i>first pass only</span><span><i style="background:var(--accent)"></i>fully transcribed</span>
    <span><i style="background:var(--kid)"></i>her speech (minutes)</span></div>
  <div class="chart" id="lib-chart">${archiveChart(d.months)}</div>
  <div id="lib-month" class="small muted">Click a month to see its recordings.</div>

  <h3>Transcript quality</h3>
  <div class="tiles">
    <div class="tile"><div class="v">${ag.mean != null ? Math.round(ag.mean * 100) + "%" : "…"}</div><div class="l">of lines where a second, independent listen agrees (${ag.n} moments checked so far)</div></div>
    <div class="tile"><div class="v">${q.ground_truth.mean_cer != null ? Math.round(q.ground_truth.mean_cer * 100) + "%" : "–"}</div><div class="l">character error rate on the ${q.ground_truth.n} lines you've corrected</div></div>
    <div class="tile"><div class="v">${sg.fixed}</div><div class="l">lines corrected by you · ${ed.total} edits logged</div></div>
    <div class="tile"><div class="v">${sg.recovered.toLocaleString()}</div><div class="l">lines recovered that the first pass had dropped</div></div>
  </div>

  <h3>Noise removed before you ever see it</h3>
  <div class="words">
    <span class="w">duplicates from recovery<b>${sg.dup.toLocaleString()}</b></span>
    <span class="w">"subscribe / thanks for watching" filler<b>${sg.halluc.toLocaleString()}</b></span>
    <span class="w">repetition loops<b>${sg.loop_.toLocaleString()}</b></span>
    <span class="w">audiobook / show replays<b>${sg.rep.toLocaleString()}</b></span>
    <span class="w">wrong-language junk<b>${sg.foreign_.toLocaleString()}</b></span>
  </div>

  <div class="two">
    <div><h3>Words it has learned from your corrections</h3>
      <div class="words">${ed.lexicon.map((x) => `<span class="w" title="heard as “${esc(x.wrong)}”">${esc(x.right)}<b>${x.n}×</b></span>`).join("") || `<span class="faint small">None yet — every fix you make teaches it.</span>`}</div>
      <p class="small muted">These are fed to Whisper as vocabulary for every new transcription.</p></div>
    <div><h3>Your recent edits</h3>
      <table>${ed.recent.slice(0, 10).map((e) => `<tr><td class="faint small">${esc((e.at || "").slice(5, 16))}</td><td class="small">${esc(e.kind)}</td><td class="small"><s class="faint">${esc((e.before || "").slice(0, 40))}</s> → ${esc((e.after || "").slice(0, 40))}</td></tr>`).join("") || `<tr><td class="faint small">No edits yet.</td></tr>`}</table></div>
  </div>
  <h3>📝 Your {notes} <span class="faint small">— everything you've marked with curly braces; say "apply my notes" to Claude to work through them</span></h3>
  <div id="lib-notes" class="small muted">Loading…</div>
  <p class="faint small">Second-listen checker: ${d.relisten_worker.busy ? "working" : "idle"} · ${d.relisten_worker.done} done this session${d.relisten_worker.last_error ? " · last error: " + esc(d.relisten_worker.last_error) : ""}</p>`;
  $$("#lib-chart [data-month]").forEach((el) => (el.onclick = () => showLibMonth(el.dataset.month)));
  const n = await api("/api/notes");
  $("#lib-notes").innerHTML = n.items.length ? `<table>${n.items.map((x) => `<tr><td class="faint">${esc((x.recorded_at || "").slice(0, 10))}</td>
      <td>${esc(x.where)}</td><td>${esc(x.text).replace(/(\{[^{}]*\}?)/g, '<mark class="todo">$1</mark>')}</td>
      <td><a href="#" data-tr="${x.rec_id}" data-idx="${x.idx ?? ""}">open</a></td></tr>`).join("")}</table>`
    : `<span class="faint">No {notes} right now.</span>`;
  $$("#lib-notes [data-tr]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openTranscript(a.dataset.tr, a.dataset.idx ? +a.dataset.idx : undefined); }));
};
function funnelStep(label, big, small, pctW) {
  return `<div class="fstep"><div class="fl">${label}</div><div class="fb">${big}</div><div class="fs">${small}</div>${pctW != null ? `<div class="fbar"><div style="width:${pctW}%"></div></div>` : ""}</div>`;
}
function archiveChart(months) {
  if (!months.length) return "";
  // fill gaps so the timeline is continuous
  const all = [], idx = Object.fromEntries(months.map((m) => [m.month, m]));
  let [y, mo] = months[0].month.split("-").map(Number);
  const [ly, lm] = months[months.length - 1].month.split("-").map(Number);
  while (y < ly || (y === ly && mo <= lm)) { const k = `${y}-${String(mo).padStart(2, "0")}`; all.push(idx[k] || { month: k, hours: 0 }); mo++; if (mo > 12) { mo = 1; y++; } }
  const W = 960, H = 230, pad = 30, bw = (W - pad * 2) / all.length, top = 150, maxH = Math.max(...all.map((m) => m.hours || 0), 1);
  const maxK = Math.max(...all.map((m) => m.minutes || 0), 1);
  const y1 = (h) => (top - 10) * h / maxH;
  const bars = all.map((m, i) => {
    const x = pad + i * bw; let yb = top; let out = "";
    for (const [k, c] of [["h_full", "var(--accent)"], ["h_first", "var(--dad)"], ["h_waiting", "var(--faint)"], ["h_skipped", "var(--line)"]]) {
      const h = y1(m[k] || 0); if (!h) continue; yb -= h;
      out += `<rect x="${x + 1}" y="${yb}" width="${Math.max(1, bw - 2)}" height="${h}" fill="${c}" rx="1"/>`;
    }
    const kh = 44 * (m.minutes || 0) / maxK;
    out += kh ? `<rect x="${x + 1}" y="${top + 8}" width="${Math.max(1, bw - 2)}" height="${kh}" fill="var(--kid)" opacity=".85" rx="1"/>` : "";
    const tip = `${m.month}: ${m.n || 0} recordings, ${m.hours || 0} h${m.minutes ? ` · her speech ${m.minutes} min` : ""}${m.moments ? ` · ${m.moments} moments (${m.kept || 0} kept)` : ""}${m.q != null ? ` · confidence ${m.q}` : ""}`;
    const lab = (m.month.endsWith("-01") || i === 0) ? `<text x="${x}" y="${H - 4}">${m.month.slice(0, 4)}</text>` : "";
    return `<g data-month="${m.month}" style="cursor:pointer"><title>${tip}</title><rect x="${x}" y="0" width="${bw}" height="${H - 16}" fill="transparent"/>${out}</g>${lab}`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Recordings per month">${bars}
    <text x="${pad - 6}" y="12" text-anchor="end">${maxH.toFixed(0)}h</text><text x="${pad - 6}" y="${top + 20}" text-anchor="end">her</text></svg>`;
}
async function showLibMonth(month) {
  const r = await api("/api/recordings");
  const rows = r.items.filter((x) => (x.recorded_at || "").startsWith(month));
  $("#lib-month").innerHTML = `<h3>${month}</h3>` + (rows.length ? `<table><tr><th>Recorded</th><th>Place / title</th><th class="num">Length</th><th>Status</th><th class="num">Her lines</th><th class="num">Moments</th><th></th></tr>
    ${rows.map((x) => `<tr><td>${fmtDate(x.recorded_at)}</td><td class="small">${esc(x.title || x.filename)}</td><td class="num">${x.duration ? Math.round(x.duration / 60) + "m" : ""}</td>
      <td class="small muted">${x.status}${x.category ? " · " + esc(x.category) : ""}</td><td class="num">${x.kid_segments || ""}</td><td class="num">${x.moments || ""}</td>
      <td>${["analyzed", "mined"].includes(x.status) ? `<a href="#" data-tr="${x.id}">transcript</a>` : ""}</td></tr>`).join("")}</table>`
    : `<p class="faint">No in-scope recordings this month (before the Jun 2024 cut-off, or none recorded).</p>`);
  $$("#lib-month [data-tr]").forEach((a) => (a.onclick = (e) => { e.preventDefault(); openTranscript(a.dataset.tr); }));
}


/* ---- second listen on cards */
function relistenExtras(m) {
  const ex = m.relisten?.extra || [];
  if (!ex.length) return "";
  return `<div class="n-extra"><div class="small muted">🎧 Heard in the audio but missing from the lines:</div>${ex.map((e, k) =>
    `<div class="n-alt"><span>${esc(e.text)}</span> <button class="btn ghost small" data-extra="${k}">＋ add</button></div>`).join("")}</div>`;
}
document.addEventListener("click", async (e) => {
  const use = e.target.closest("[data-use]"), ext = e.target.closest("[data-extra]");
  if (!use && !ext) return;
  const m = current === "polish" ? P.cur : T.items[T.i];
  if (!m) return;
  if (current === "polish" && ext) {
    const ex = m.relisten.extra[+ext.dataset.extra];
    const before = m.lines.filter((l) => l.start != null && l.start <= ex.start).slice(-1)[0];
    await flushPolishSave();
    await api(`/api/moment/${m.id}/addline`, { text: ex.text, speaker: "kid", after_seg_id: before ? before.seg_id : null, via: "relisten" });
    toast("Added — change the speaker if it wasn't her"); return openPolish(m.id);
  }
  if (use) {
    const s = m.segments.find((x) => x.id === +use.dataset.use), rl = m.relisten.lines[s.id], alt = use.dataset.other ? rl.other : rl.alt;
    await api(`/api/moment/${m.id}/segment`, { seg_id: s.id, text: alt, via: "relisten" });
    s.text = alt; s.fixed = 1; m.relisten.lines[s.id].verdict = "you";
    toast("Used the second listen ✓");
  } else {
    const ex = m.relisten.extra[+ext.dataset.extra];
    const before = m.segments.filter((x) => x.in && x.start <= ex.start).slice(-1)[0];
    const upd = await api(`/api/moment/${m.id}/addline`, { text: ex.text, speaker: "kid", after_seg_id: before ? before.id : null, via: "relisten" });
    upd.relisten.extra = (m.relisten.extra || []).filter((x) => x !== ex);
    T.items[T.i] = upd;
    toast("Added — tap the name if it wasn't her");
  }
  showCard(false);
});

/* ---- review power tools (2026-10-12): one-tap vibe fixes, split by selecting words, echo, hide duplicate */
const VIBES = [["!", "!"], ["?", "?"], ["…", "…"], ["—", "—"], ["Aa", "cap"], ["aa", "lower"], ["ABC", "caps"], ["no .", "nodot"]];
const vibeBar = () => `<div class="vibes" title="one-tap tone fixes">${VIBES.map(([l, k]) => `<button type="button" class="vb" data-vibe="${k}">${l}</button>`).join("")}</div>`;
function vibe(text, k) {
  const t = (text || "").trim(), END = /[\s.!?…—。！？,，]+$/;
  const cut = t.replace(END, "");
  switch (k) {
    case "!": return cut + (/[㐀-鿿]$/.test(cut) ? "！" : "!");
    case "?": return cut + (/[㐀-鿿]$/.test(cut) ? "？" : "?");
    case "…": return cut + "…";
    case "—": return cut + "—";
    case "cap": return t.replace(/(^|[.!?…]\s+)([a-z])/g, (m, a, b) => a + b.toUpperCase()).replace(/(^|\s)i(?=\s|'|$)/g, "$1I");
    case "lower": return t.replace(/^([^A-Za-z]*)([A-Z])(?![A-Z])/, (m, a, b) => a + b.toLowerCase());
    case "caps": return t === t.toUpperCase() ? t.toLowerCase().replace(/^([^a-z]*)([a-z])/, (m, a, b) => a + b.toUpperCase()) : t.toUpperCase();
    case "nodot": return t.replace(/[.。]+$/, "");
  }
  return t;
}
const otherSpeaker = (spk, segs) => (spk === "kid" ? partnerOf(segs || []) : "kid");
function partsFromSelection(text, start, len, spkOrig, spkNew) {
  const before = text.slice(0, start).trim(), mid = text.slice(start, start + len).trim(), after = text.slice(start + len).trim();
  return [before && { text: before, speaker: spkOrig }, mid && { text: mid, speaker: spkNew }, after && { text: after, speaker: spkOrig }].filter(Boolean);
}
function splitPopup(rect, curSpk, onPick) {  // "these words were said by…"
  $("#spl-pop")?.remove();
  const d = document.createElement("div"); d.id = "spl-pop"; d.className = "spl-pop";
  d.innerHTML = `<span class="faint small">Said by</span>` + SPK.map((k) => `<button type="button" class="spk ${k}" data-p="${k}" ${k === curSpk ? "disabled" : ""}>${EMOJI[k]} ${esc(NAMES[k])}</button>`).join("")
    + `<button type="button" class="btn ghost small" data-x>✕</button>`;
  d.style.left = `${Math.max(8, rect.left + window.scrollX)}px`; d.style.top = `${rect.bottom + window.scrollY + 6}px`;
  document.body.appendChild(d);
  d.querySelectorAll("[data-p]").forEach((b) => (b.onmousedown = (e) => { e.preventDefault(); d.remove(); onPick(b.dataset.p); }));
  d.querySelector("[data-x]").onmousedown = (e) => { e.preventDefault(); d.remove(); };
  setTimeout(() => document.addEventListener("mousedown", function off(e) { if (!d.contains(e.target)) { d.remove(); document.removeEventListener("mousedown", off); } }), 0);
}

/* ---- insert a missed line anywhere; sounds in [brackets] */
const SOUNDS = ["giggles", "laughs", "crying", "whines", "gasps", "sings", "whispers", "shouts", "sighs", "sniffles", "screams", "hums"];
// a hidden form slot after each line; the visible control is the ＋ button on the line itself
const insSlot = (afterId) => afterId === ""
  ? `<div class="ins top" data-after=""><span>＋ a line is missing before this</span></div>`
  : `<div class="ins" data-after="${afterId}"></div>`;
const addBtn = (afterId) => `<button type="button" class="addbtn" data-addafter="${afterId}" title="a line is missing right after this one — add it">＋</button>`;
// {notes} typed into a line — "{Mom: blue one!}" can become its own line
const NOTE_RE = /\{([^{}]*)(?:\}|$)/;
function noteOf(text) { const m = (text || "").match(NOTE_RE); return m ? { full: m[0], body: m[1].trim() } : null; }
function speakerPrefix(str) {
  const m = (str || "").match(/^\s*([A-Za-z\u4e00-\u9fff]+)\s*[:：]\s*/);
  if (!m) return null;
  const w = m[1].toLowerCase();
  const table = { kid: ["c", "k", "kid", "baby"], mom: ["m", "mom", "mommy", "mama", "mum", "妈妈"], dad: ["d", "dad", "daddy", "papa", "爸爸"], other: ["o", "other"] };
  for (const k of SPK) if (table[k].includes(w) || (NAMES[k] || "").toLowerCase() === w) return { spk: k, rest: str.slice(m[0].length) };
  return null;
}
function noteToLine(body) {  // "the child: 我是有。。。 missed half sentence)" → kid, "我是有。。。"
  let text = body.replace(/[)）]\s*$/, "").trim(), spk = null;
  const p = speakerPrefix(text); if (p) { spk = p.spk; text = p.rest.trim(); }
  const cut = text.match(/^(.*[\u3400-\u9fff。，！？…!?.]+)\s+[a-z][a-z ,'-]*$/);  // drop trailing English comment after Chinese speech
  if (cut) text = cut[1].trim();
  return { spk, text };
}
const noteBar = (segKey, text) => noteOf(text) ? `<div class="notebar" data-notefor="${segKey}" data-note="${esc(noteOf(text).full)}"><span>📝 your note: <b>${esc(noteOf(text).full)}</b></span>
  <button type="button" class="btn small" data-note2="below">↳ make it a line below</button><button type="button" class="btn small ghost" data-note2="above">↑ above</button></div>` : "";
/* Splitting a line whisper merged: "i found it / did you find this / i found it / yeah you did / okay".
   Unlabelled turns alternate between her and whoever she's talking to; "m: …" / "d: …" / "c: …" pins one. */
const SPLIT_RE = /\s*(?<!\d)[\/|](?!\d)\s*/;
function splitParts(text, firstSpk, partner) {
  const pieces = (text || "").split(SPLIT_RE).map((x) => x.trim()).filter(Boolean);
  if (pieces.length < 2) return null;
  const other = (k) => (k === "kid" ? partner : "kid");
  let prev = null;
  return pieces.map((p, i) => {
    const pre = speakerPrefix(p);
    const spk = pre ? pre.spk : i === 0 ? firstSpk : other(prev);
    prev = spk;
    return { text: pre ? pre.rest.trim() : p, speaker: spk };
  });
}
function partnerOf(segs) {  // the adult she's mostly talking with in this moment
  const n = (k) => segs.filter((s) => s.speaker === k).length;
  return n("mom") > n("dad") ? "mom" : "dad";
}
const splitPreviewHtml = (parts) => parts ? `<div class="split-prev">✂ becomes ${parts.length} lines: ${parts.map((p) =>
  `<span class="spk ${p.speaker}">${EMOJI[p.speaker] || ""}</span> ${esc(p.text)}`).join(" <span class='faint'>·</span> ")}
  <span class="faint small">— Enter to split</span></div>` : "";
function watchSplit(el, seg, segs, host) {  // live preview under the line being edited
  let box = host.querySelector(".split-prev-host");
  if (!box) { box = document.createElement("div"); box.className = "split-prev-host"; host.after(box); }
  const upd = () => { box.innerHTML = splitPreviewHtml(splitParts(el.textContent, seg.speaker, partnerOf(segs)))
    || `<div class="split-prev hint">Several people? Type <b>/</b> between turns — speakers alternate. Add missing words anywhere. <b>d:</b> <b>m:</b> <b>c:</b> sets who. Enter saves.</div>`; };
  el.addEventListener("input", upd); upd();
  return () => box.remove();
}
/* Wire the ＋ buttons, the top slot and note→line buttons in one list.
   order: ids of the lines in the moment, top to bottom (for "above").
   beforeInsert(note, segKey): strip the note from its host line before the new line goes in. */
function wireInserts(scope, momentId, order, done, beforeInsert) {
  const slotFor = (afterId) => scope.querySelector(`.ins[data-after="${afterId}"]`);
  const open = (afterId, prefill) => {
    const slot = slotFor(afterId); if (!slot) return;
    scope.querySelectorAll(".ins.open").forEach((x) => x !== slot && x._close?.());
    openInsert(slot, momentId, afterId, done, prefill);
  };
  scope.querySelectorAll("[data-addafter]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); open(b.dataset.addafter); }));
  const top = slotFor(""); if (top) top.onclick = () => { if (!top.classList.contains("open")) open(""); };
  scope.querySelectorAll("[data-notefor]").forEach((bar) => bar.querySelectorAll("[data-note2]").forEach((b) => (b.onclick = async (e) => {
    e.stopPropagation();
    const key = bar.dataset.notefor, i = order.indexOf(key);
    const afterId = b.dataset.note2 === "below" ? key : (i > 0 ? order[i - 1] : "");
    const note = noteOf(bar.dataset.note);
    open(afterId, { ...noteToLine(note.body), onSave: () => beforeInsert && beforeInsert(note, key) });
  })));
  return open;
}
function soundChips(targetSel) {
  return `<div class="sounds">${SOUNDS.map((x) => `<button type="button" class="chip" data-sound="${x}" data-target="${targetSel}">[${x}]</button>`).join("")}</div>`;
}
function insertAtCursor(input, str) {
  const a = input.selectionStart ?? input.value.length, b = input.selectionEnd ?? a;
  const pad = (a > 0 && !/\s$/.test(input.value.slice(0, a)) ? " " : "");
  input.value = input.value.slice(0, a) + pad + str + " " + input.value.slice(b);
  const pos = a + pad.length + str.length + 1; input.setSelectionRange(pos, pos); input.focus();
  input.dispatchEvent(new Event("input", { bubbles: true }));
}
document.addEventListener("mousedown", (e) => {  // keep focus in the text box when a chip is clicked
  if (e.target.closest("[data-sound]")) e.preventDefault();
});
function wireSounds(scope, input, after) {
  scope.querySelectorAll("[data-sound]").forEach((c) => (c.onclick = (e) => {
    e.stopPropagation();
    insertAtCursor(input, `[${c.dataset.sound}]`);
    if (after) after();
  }));
}
function openInsert(slot, momentId, afterId, done, prefill = {}) {
  if (slot.classList.contains("open")) {  // already open here: just refill / refocus, never stack a second form
    const inp = slot.querySelector(".ins-text");
    if (inp) { if (prefill.text) inp.value = prefill.text; inp.focus(); return; }
  }
  const idle = afterId === "" ? `<span>＋ a line is missing before this</span>` : "";
  slot.classList.add("open");
  let spk = prefill.spk || "kid";
  slot.innerHTML = `<div class="ins-form">
    <div class="ins-head">${afterId === "" ? "Missing line at the start" : "Missing line — goes right here"}</div>
    <div class="ins-spk">${SPK.map((k, i) => `<button type="button" class="spk ${k} ${k === spk ? "sel" : ""}" data-pick="${k}">${EMOJI[k]} ${esc(NAMES[k])}</button>`).join("")}</div>
    <input type="text" class="ins-text" placeholder="what was said · tip: start with m: / d: / ${esc((NAMES.kid || "k")[0].toLowerCase())}: to pick the speaker">
    ${soundChips("")}
    <div class="row"><button type="button" class="btn primary small" data-save>Insert ↵</button><button type="button" class="btn ghost small" data-cancel>Cancel <kbd>Esc</kbd></button></div></div>`;
  const input = slot.querySelector(".ins-text");
  input.value = prefill.text || ""; input.focus(); input.setSelectionRange(input.value.length, input.value.length);
  wireSounds(slot, input);
  const pick = (k) => { spk = k; slot.querySelectorAll("[data-pick]").forEach((x) => x.classList.toggle("sel", x.dataset.pick === k)); };
  slot.querySelectorAll("[data-pick]").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); pick(b.dataset.pick); input.focus(); }));
  input.oninput = () => { const p = speakerPrefix(input.value); if (p) { pick(p.spk); input.value = p.rest; } };
  const close = (e) => { e?.stopPropagation(); slot.classList.remove("open"); slot.innerHTML = idle; slot._close = null; };
  slot._close = close;
  let busy = false;
  const save = async (e) => {
    e?.stopPropagation(); if (busy) return;
    const text = input.value.trim(); if (!text) return close();
    busy = true;
    try {
      if (prefill.onSave) await prefill.onSave();
      const upd = await api(`/api/moment/${momentId}/addline`, { text, speaker: spk, after_seg_id: afterId ? +afterId : null, via: current });
      toast(`Inserted ${EMOJI[spk] || ""} ✓`); done(upd);
    } finally { busy = false; }
  };
  slot.querySelector("[data-save]").onclick = save;
  slot.querySelector("[data-cancel]").onclick = close;
  input.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter" && !e.isComposing) { e.preventDefault(); save(); } if (e.key === "Escape") close(); };
  slot.onclick = (e) => e.stopPropagation();
  slot.scrollIntoView({ block: "nearest" });
}

/* ------------------------------------------------------------------ ⏱ Daily 5 (replaces Quality check + Tonight + Lightning) */
views.daily = async () => {
  const D = { items: [], i: 0, ans: [], undo: [], t0: 0, timer: null, secs: 300, checks: 0 };
  const stop = () => { clearInterval(D.timer); document.onkeydown = null; player.pause(); };
  const intro = async () => {
    $("#view").innerHTML = `<div class="night"><div class="n-card lg"><p class="faint">Loading today's cards…</p></div></div>`;
    const day = await api("/api/daily");
    D.items = [];
    day.cards.forEach((c, k) => { D.items.push({ type: "moment", c }); if (k % 6 === 5 && day.checks[Math.floor(k / 6)]) D.items.push({ type: "check", c: day.checks[Math.floor(k / 6)] }); });
    $("#view").innerHTML = `<div class="night"><div class="n-card lg"><div class="n-q">⏱ Daily 5</div>
      <p class="lg-p">${day.done_today ? `✓ Done for today (${day.done_today.cards} cards). ` : ""}${day.streak ? `🔥 ${day.streak}-day streak. ` : ""}Up to ${day.cards.length} cards with sound and context, plus ${day.checks.length} quick transcript checks —
      5 minutes, then you're done. The cards are the ones the system is least sure about; your answers sharpen it and shrink the backlog.</p>
      <p class="lg-p"><kbd>J</kbd> keep · <kbd>L</kbd> log only · <kbd>X</kbd> pass · <kbd>N</kbd> later · <kbd>⌫</kbd> undo · <kbd>R</kbd> replay · <kbd>Space</kbd> pause</p>
      <div class="n-actions"><button class="n-btn keep" id="dy-go" ${day.cards.length ? "" : "disabled"}><b>${day.cards.length ? "Start" : "Nothing left today"}</b><span>press Enter</span></button></div>
      <details class="tidy"><summary>🧹 Tidy up the backlog (sweep · merge)</summary><div id="tidy"></div></details></div></div>`;
    $("#dy-go").onclick = start;
    document.onkeydown = (e) => { if (e.key === "Enter" && !/INPUT|SELECT|TEXTAREA|SUMMARY/.test(e.target.tagName)) start(); };
    $(".tidy").addEventListener("toggle", () => { if ($(".tidy").open) tidy($("#tidy")); }, { once: true });
    if (/[?&]tidy=1/.test(location.search)) $(".tidy").open = true;
  };
  const start = () => {
    if (!D.items.length) return;
    document.onkeydown = null; D.t0 = Date.now(); D.i = 0;
    D.timer = setInterval(() => { const left = D.secs - (Date.now() - D.t0) / 1000; const c = $("#dy-clock"); if (c) c.textContent = fmtT(Math.max(0, left)); if (left <= 0) finish(); }, 250);
    document.onkeydown = (e) => {
      if (e.metaKey || e.ctrlKey) return;
      const k = e.key.toLowerCase(), it = D.items[D.i];
      if (k === "backspace") { e.preventDefault(); return back(); }
      if (k === "r") return audio.replay();
      if (k === " ") { e.preventDefault(); return audio.toggle(); }
      if (!it) return;
      if (it.type === "moment") { const d = { j: "journal", l: "log", x: "rejected", n: "later" }[k]; if (d) answer(d); }
      else { const a = CHECK_ANSWERS[it.c.mode][+k - 1]; if (a) answer(a[0]); }
    };
    show();
  };
  const warm = (it) => { const c = it && (it.type === "moment" ? it.c : null); if (c && c.clip) fetch(`/api/clip?rec=${c.rec}&start=${c.clip.start.toFixed(2)}&end=${c.clip.end.toFixed(2)}`).catch(() => {}); };
  const top = () => `<div class="n-top"><div class="n-prog"><div style="width:${100 * D.i / D.items.length}%"></div></div><span class="n-clock" id="dy-clock"></span><button class="btn ghost" id="dy-end">Finish</button></div>`;
  const show = () => {
    const it = D.items[D.i];
    if (!it) return finish();
    if (it.type === "moment") {
      const c = it.c;
      $("#view").innerHTML = `<div class="night">${top()}<div class="n-card lg"><div class="faint small">${esc(fmtDate(c.date))} · ${D.i + 1}/${D.items.length}</div>
        <div class="lg-lines">${(c.ctx || []).map((l) => `<div class="lg-l ctx">${EMOJI[l.spk] || ""} ${esc(l.t)}</div>`).join("")}${c.lines.map((l) => `<div class="lg-l ${l.spk === "kid" ? "kid" : ""}">${EMOJI[l.spk] || ""} ${esc(l.t)}</div>`).join("")}</div>
        <div class="n-actions four"><button class="n-btn keep" data-d="journal"><b>Keep</b><kbd>J</kbd></button><button class="n-btn" data-d="log"><b>Log only</b><kbd>L</kbd></button>
        <button class="n-btn bad" data-d="rejected"><b>Pass</b><kbd>X</kbd></button><button class="n-btn" data-d="later"><b>Later</b><kbd>N</kbd></button></div></div></div>`;
      $$("[data-d]").forEach((b) => (b.onclick = () => answer(b.dataset.d)));
      if (c.clip) audio.play(c.rec, c.clip.start, c.clip.end);
    } else {
      const c = it.c, sg = c.seg, ans = CHECK_ANSWERS[c.mode];
      $("#view").innerHTML = `<div class="night">${top()}<div class="n-card lg check"><div class="n-kicker">Quick check</div><div class="n-q">${esc(c.question)}</div>
        <div class="n-quote">“${esc(sg.text)}”</div><div class="faint small">${esc(fmtDate(sg.recorded_at))}</div>
        <div class="row" style="justify-content:center;gap:8px"><button class="btn ghost" id="dy-play">🔊 Play again</button><button class="btn ghost" id="dy-ctx">🔊 With 5 s around it</button></div>
        <div class="n-actions ${ans.length === 4 ? "four" : "three"}">${ans.map(([v, label, tone], k) => `<button class="n-btn ${tone}" data-v="${v}"><b>${esc(label)}</b><kbd>${k + 1}</kbd></button>`).join("")}</div></div></div>`;
      const play = (pad) => audio.play(sg.rec_id, Math.max(0, sg.start - pad), sg.end + pad + 0.1);
      $("#dy-play").onclick = () => play(0.3); $("#dy-ctx").onclick = () => play(5);
      $$("[data-v]").forEach((b) => (b.onclick = () => answer(b.dataset.v)));
      play(0.3);
    }
    $("#dy-end").onclick = finish;
    warm(D.items[D.i + 1]);
  };
  const answer = (d) => {
    const it = D.items[D.i]; if (!it) return;
    D.ans[D.i] = d; D.undo.push(D.i); D.i++; show();
    const req = it.type === "moment" ? api(`/api/moment/${it.c.id}/triage`, { decision: d }) : api("/api/check", { seg_id: it.c.seg.id, mode: it.c.mode, verdict: d });
    req.catch((e) => toast("Save failed: " + e.message));
  };
  const back = () => {
    if (!D.undo.length) return;
    const i = D.undo.pop(), it = D.items[i]; D.i = i; delete D.ans[i];
    (it.type === "moment" ? api(`/api/moment/${it.c.id}/triage`, { decision: "pending" }) : api("/api/check", { seg_id: it.c.seg.id, undo: true })).catch(() => {});
    show(); toast("Undone");
  };
  const finish = async () => {
    stop();
    const done = D.items.map((it, i) => [it, D.ans[i]]).filter(([, a]) => a);
    const cards = done.filter(([it]) => it.type === "moment"), checks = done.length - cards.length;
    const kept = cards.filter(([, a]) => a === "journal").length;
    const rnd = cards.filter(([it]) => it.c.rand), rk = rnd.filter(([, a]) => a === "journal").length;
    api("/api/daily/done", { cards: cards.length, kept, checks }).catch(() => {});
    $("#view").innerHTML = `<div class="night"><div class="n-card lg"><div class="n-q">✓ Done — ${cards.length} cards${checks ? ` + ${checks} checks` : ""} in ${fmtT(Math.min(D.secs, (Date.now() - D.t0) / 1000))}</div>
      <p class="lg-p">Kept <b>${kept}</b> of ${cards.length}${rnd.length ? ` (on the ${rnd.length} random test cards: ${rk})` : ""}. See you tomorrow.</p>
      <details class="tidy" open><summary>🧹 Tidy up the backlog (sweep · merge)</summary><div id="tidy"></div></details></div></div>`;
    tidy($("#tidy"));
  };
  // ---- tidy: sweep (model) + merge (nearby cards), both opt-in and undoable
  const tidy = async (box) => {
    box.innerHTML = `<p class="faint small">Working out what could be cleared…</p>`;
    const [sw, mg] = await Promise.all([api("/api/lightning/sweep"), api("/api/merge")]);
    let h = "";
    if (sw.available) h += `<div class="tidy-b"><p class="lg-p"><b>Sweep:</b> ${sw.count} waiting cards look like the ones you pass on (past cards like these were not keepers ${Math.round(sw.precision * 100)}% of the time; ~${Math.max(1, Math.round(sw.count * (1 - sw.precision)))} might be). Filed as <b>log only</b> — her speech still counts — one tap to undo.</p>
      <div class="n-actions three"><button class="n-btn keep" id="t-sw"><b>File ${sw.count}</b></button><button class="n-btn" id="t-peek"><b>Show me</b></button><button class="n-btn" id="t-swu"><b>Undo last sweep</b></button></div><div id="t-list"></div></div>`;
    else h += `<div class="tidy-b"><p class="lg-p faint">Sweep: ${esc(sw.reason)}</p></div>`;
    const act = mg.batches.filter((b) => b.active > 0);
    h += `<div class="tidy-b"><p class="lg-p"><b>Merge nearby cards:</b> cards within 30 s of each other in the same recording are usually one conversation. Pilot on the cards you'll see first: <b>${mg.pilot.groups}</b> exchanges would absorb <b>${mg.pilot.cards_absorbed}</b> cards (all: ${mg.all.groups} / ${mg.all.cards_absorbed}).
      Nothing is deleted; each merge is recorded and can be undone.</p><div class="n-actions three"><button class="n-btn keep" id="t-mg" ${mg.pilot.groups ? "" : "disabled"}><b>Run pilot</b><span>${mg.pilot.cards_absorbed} fewer cards</span></button></div>
      ${act.map((b) => `<div class="lg-mini">Batch ${esc(b.batch)} — ${b.groups} exchanges, ${b.cards} cards absorbed <button class="btn ghost" data-undo="${esc(b.batch)}">Undo this batch</button></div>`).join("")}</div>`;
    box.innerHTML = h;
    if (sw.available) {
      $("#t-sw").onclick = async () => { const r = await api("/api/lightning/sweep", { ids: sw.ids }); toast(`Filed ${r.filed} as log only`); await refreshStatus(); tidy(box); };
      $("#t-swu").onclick = async () => { const r = await api("/api/lightning/undo", {}); toast(`Restored ${r.restored} to Triage`); await refreshStatus(); tidy(box); };
      $("#t-peek").onclick = () => { $("#t-list").innerHTML = sw.cards.map((c) => `<div class="lg-mini">${c.lines.map((l) => `<span class="${l.spk === "kid" ? "kid" : ""}">${EMOJI[l.spk] || ""} ${esc(l.t)}</span>`).join(" ")}</div>`).join(""); };
    }
    $("#t-mg").onclick = async () => { const r = await api("/api/merge/apply", { scope: "pilot" }); toast(`Merged ${r.groups} exchanges (${r.cards_absorbed} cards)`); await refreshStatus(); tidy(box); };
    $$("[data-undo]", box).forEach((b) => (b.onclick = async () => { const r = await api("/api/merge/undo", { batch: b.dataset.undo }); toast(`Restored ${r.restored} cards${r.skipped_decided ? ` (${r.skipped_decided} already decided, left as is)` : ""}`); await refreshStatus(); tidy(box); }));
  };
  intro();
};
views.lightning = views.daily;


/* ------------------------------------------------------------------ per-line audio: ⏵ from here · 🔁 loop */
function wireLineAudio(el, rec, line, cardEnd) {
  const from = $(".fromhere", el), lp = $(".loopbtn", el);
  if (line.start == null) { if (from) from.disabled = true; if (lp) lp.disabled = true; return; }
  if (from) from.onclick = (e) => { e.stopPropagation(); audio.play(rec, Math.max(0, line.start - 0.3), Math.max(line.end, cardEnd || line.end) + 0.4); };
  if (lp) lp.onclick = (e) => {
    e.stopPropagation();
    const on = audio.loopLine(rec, Math.max(0, line.start - 0.2), line.end + 0.3, `${rec}:${line.id ?? line.seg_id}`);
    $$(".loopbtn.on").forEach((b) => b.classList.remove("on")); if (on) lp.classList.add("on");
  };
}

/* ------------------------------------------------------------------ ✎ edit the whole card as text
   One line per turn, "c:" her · "d:" Dad · "m:" Mom · "o:" other. On save the text is diffed against the card and turned into
   the existing, logged actions: changed text/speaker → /segment · one line cut in two → /split · new line → /addline ·
   deleted line → /hide · two lines joined → text into the first, second hidden. Nothing is deleted. */
const SPK_LETTER = { kid: "c", mom: "m", dad: "d", other: "o" };
const normT = (t) => (t || "").toLowerCase().replace(/[^\w\u3400-\u9fff]+/g, "");
function cardRows(m, polish) {
  return polish ? m.lines.filter((l) => l.include && l.seg_id).map((l) => ({ seg: l.seg_id, spk: l.speaker, text: l.text }))
                : m.segments.filter((s) => s.in).map((s) => ({ seg: s.id, spk: s.speaker, text: s.text }));
}
const rowsToText = (rows) => rows.map((r) => `${SPK_LETTER[r.spk] || "o"}: ${r.text}`).join("\n");
function parseCardText(txt, fallback) {
  let prev = fallback || "kid"; const out = [];
  for (const raw of txt.split("\n")) {
    const t = raw.trim(); if (!t) continue;
    const p = speakerPrefix(t);
    if (p) { prev = p.spk; if (p.rest.trim()) out.push({ spk: p.spk, text: p.rest.trim() }); }
    else out.push({ spk: prev, text: t });
  }
  return out;
}
function diffCard(old, neu) {
  // 1) align lines whose text is unchanged (LCS on exact text)
  const n = old.length, k = neu.length, L = Array.from({ length: n + 1 }, () => new Array(k + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) for (let j = k - 1; j >= 0; j--) L[i][j] = old[i].text === neu[j].text ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
  const ops = []; let i = 0, j = 0;
  const tok = (t) => new Set((t || "").toLowerCase().match(/[a-z0-9']+|[㐀-鿿]/g) || []);
  const sim = (x, y) => { const A = tok(x.text), B = tok(y.text); if (!A.size || !B.size) return 0; let c = 0; A.forEach((w) => B.has(w) && c++); return c / (A.size + B.size - c); };
  const gap = (O, N) => {  // 2) between unchanged lines: find splits/merges anywhere, then pair edits by similarity — in reading order
    if (!O.length) return N.forEach((x) => ops.push({ op: "add", neu: x }));
    if (!N.length) return O.forEach((x) => ops.push({ op: "del", old: x }));
    for (let a = 0; a < O.length; a++) for (let b = 0; b < N.length; b++) {
      for (let c = 2; c <= 4; c++) {
        if (b + c <= N.length && normT(N.slice(b, b + c).map((x) => x.text).join("")) === normT(O[a].text)) {
          gap(O.slice(0, a), N.slice(0, b)); ops.push({ op: "split", old: O[a], parts: N.slice(b, b + c) }); return gap(O.slice(a + 1), N.slice(b + c));
        }
        if (a + c <= O.length && normT(O.slice(a, a + c).map((x) => x.text).join("")) === normT(N[b].text)) {
          gap(O.slice(0, a), N.slice(0, b)); ops.push({ op: "merge", olds: O.slice(a, a + c), neu: N[b] }); return gap(O.slice(a + c), N.slice(b + 1));
        }
      }
    }
    if (O.length === N.length) return O.forEach((x, q) => ops.push({ op: "edit", old: x, neu: N[q] }));  // same count: edit in place
    // different counts: align by word overlap (a pair needs some words in common, else it's a hide + a new line)
    const D = Array.from({ length: O.length + 1 }, () => new Array(N.length + 1).fill(0)), sc = (x, y) => { const v = sim(x, y); return v >= 0.34 ? v : -9; };
    for (let x = O.length - 1; x >= 0; x--) for (let y = N.length - 1; y >= 0; y--) D[x][y] = Math.max(D[x + 1][y], D[x][y + 1], D[x + 1][y + 1] + sc(O[x], N[y]));
    let x = 0, y = 0;
    while (x < O.length || y < N.length) {
      if (x < O.length && y < N.length && sc(O[x], N[y]) > 0 && D[x][y] === D[x + 1][y + 1] + sc(O[x], N[y])) ops.push({ op: "edit", old: O[x++], neu: N[y++] });
      else if (x < O.length && (y >= N.length || D[x][y] === D[x + 1][y])) ops.push({ op: "del", old: O[x++] });
      else ops.push({ op: "add", neu: N[y++] });
    }
  };
  let O = [], N = [];
  while (i < n || j < k) {
    if (i < n && j < k && old[i].text === neu[j].text && L[i][j] === L[i + 1][j + 1] + 1) {
      gap(O, N); O = []; N = [];
      ops.push(old[i].spk !== neu[j].spk ? { op: "speaker", old: old[i], neu: neu[j] } : { op: "same", old: old[i], neu: neu[j] });
      i++; j++;
    } else if (j < k && (i >= n || L[i][j + 1] >= L[i + 1][j])) N.push(neu[j++]);
    else O.push(old[i++]);
  }
  gap(O, N);
  return ops;
}
function diffSummary(ops) {
  const c = {}; ops.forEach((o) => { const key = o.op === "edit" ? (o.old.spk !== o.neu.spk ? "edit+speaker" : "edit") : o.op; c[key] = (c[key] || 0) + 1; });
  const lab = { edit: "text changed", "edit+speaker": "text+speaker changed", speaker: "speaker changed", split: "split", merge: "merged", add: "new line", del: "hidden" };
  const parts = Object.entries(c).filter(([key]) => key !== "same").map(([key, v]) => `${v} ${lab[key]}`);
  return parts.length ? parts.join(" · ") : "no changes";
}
async function applyCardOps(m, ops, polish) {
  const known = new Set((polish ? m.lines.map((l) => l.seg_id) : m.segments.map((s) => s.id)));
  let prevId = null, payload = null;
  const learn = (p) => { payload = p; (p.segments || []).forEach((s) => known.add(s.id)); (p.lines || []).forEach((l) => l.seg_id && known.add(l.seg_id)); };
  const newIdAfter = (p, fallbackAfter) => { const ids = (p.segments || []).filter((s) => !known.has(s.id)).sort((a, b) => a.idx - b.idx).map((s) => s.id); return ids.length ? ids[ids.length - 1] : fallbackAfter; };
  for (const o of ops) {
    if (o.op === "same") prevId = o.old.seg;
    else if (o.op === "speaker") { await api(`/api/moment/${m.id}/segment`, { seg_id: o.old.seg, speaker: o.neu.spk, via: "textedit" }); prevId = o.old.seg; }
    else if (o.op === "edit") {
      await api(`/api/moment/${m.id}/segment`, { seg_id: o.old.seg, text: o.neu.text !== o.old.text ? o.neu.text : undefined, speaker: o.neu.spk !== o.old.spk ? o.neu.spk : undefined, via: "textedit" }); prevId = o.old.seg;
    } else if (o.op === "split") {
      const r = await api(`/api/moment/${m.id}/split`, { seg_id: o.old.seg, parts: o.parts.map((x) => ({ text: x.text, speaker: x.spk })), via: "textedit" });
      const last = newIdAfter(r, o.old.seg); learn(r); prevId = last;
    } else if (o.op === "merge") {
      await api(`/api/moment/${m.id}/segment`, { seg_id: o.olds[0].seg, text: o.neu.text, speaker: o.neu.spk !== o.olds[0].spk ? o.neu.spk : undefined, via: "textedit" });
      for (const x of o.olds.slice(1)) learn(await api(`/api/moment/${m.id}/hide`, { seg_id: x.seg, via: "textedit" }));
      prevId = o.olds[0].seg;
    } else if (o.op === "del") learn(await api(`/api/moment/${m.id}/hide`, { seg_id: o.old.seg, via: "textedit" }));
    else if (o.op === "add") {
      const r = await api(`/api/moment/${m.id}/addline`, { after_seg_id: prevId, speaker: o.neu.spk, text: o.neu.text, via: "textedit" });
      const id = newIdAfter(r, prevId); learn(r); prevId = id;
    }
  }
}
function openTextEditor(m, polish, opts = {}) {
  const host = opts.host || (polish ? $("#p-lines") : $("#t-card .lines"));
  if (!host || host.dataset.editing) return;
  const old = cardRows(m, polish); host.dataset.editing = "1";
  const html0 = host.innerHTML;
  const fallback = old.find((r) => r.spk === "kid") ? "kid" : (old[0] && old[0].spk) || "kid";
  host.innerHTML = `<div class="textedit"><textarea id="te-ta" spellcheck="false" rows="${Math.max(6, old.length + 3)}">${esc(rowsToText(old))}</textarea>
    <div class="small faint">One line per turn · <b>c:</b> ${esc(NAMES.kid)} · <b>d:</b> ${esc(NAMES.dad)} · <b>m:</b> ${esc(NAMES.mom)} · <b>o:</b> other · a line with no letter continues the speaker above · delete a line to hide it · press Enter to start a new line · to cut one line in two just press Enter in the middle of it (the <b>/</b> split is for the one-line editor, <kbd>E</kbd>). Applying the same edit twice changes nothing.</div>
    <div class="te-sum" id="te-sum">no changes</div>
    <div class="row"><button class="btn primary" id="te-go">Apply <kbd>⌘↵</kbd></button><button class="btn ghost" id="te-x">Cancel <kbd>Esc</kbd></button></div></div>`;
  const ta = $("#te-ta"); ta.focus(); ta.setSelectionRange(0, 0);
  const ops = () => diffCard(old, parseCardText(ta.value, fallback));
  const upd = () => { $("#te-sum").textContent = "Will change: " + diffSummary(ops()); };
  ta.addEventListener("input", upd);
  const close = () => { delete host.dataset.editing; host.innerHTML = html0; if (opts.onDone) return opts.onDone(true); polish ? openPolish(m.id) : (async () => { T.items[T.i] = await api(`/api/moment/${m.id}`); showCard(false); })(); };
  let applying = false;  // ⌘↵ twice / ⌘↵ + click: apply once
  const apply = async () => {
    if (applying) return; applying = true;
    const o = ops(); if (diffSummary(o) === "no changes") return close();
    if (parseCardText(ta.value, fallback).length === 0 && !confirm("That hides every line on the card. Continue?")) return;
    $("#te-go").disabled = true; $("#te-sum").textContent = "Applying…";
    try { await applyCardOps(m, o, polish); toast("Card updated — " + diffSummary(o)); } catch (e) { toast("Stopped: " + e.message); }
    close();
  };
  $("#te-go").onclick = apply;
  $("#te-x").onclick = () => { delete host.dataset.editing; host.innerHTML = html0; if (opts.onDone) return opts.onDone(false); polish ? openPolish(m.id) : showCard(false); };
  ta.addEventListener("keydown", (e) => {
    e.stopPropagation();
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") { e.preventDefault(); apply(); }
    else if (e.key === "Escape") { e.preventDefault(); $("#te-x").click(); }
  });
}


/* ------------------------------------------------------------------ ⚡ Quick-judge: her best line, one row each, ~3 s per card */
views.quick = async () => {
  const Q2 = { items: [], i: 0, seen: new Set(), undo: [], judged: 0, scored: 0, pending: 0, loading: false };
  const urlOf = (c) => `/api/clip?rec=${c.rec}&start=${c.clip.start.toFixed(2)}&end=${c.clip.end.toFixed(2)}`;
  const more = async () => {
    if (Q2.loading) return; Q2.loading = true;
    const r = await api("/api/quick", { limit: 30, exclude: [...Q2.seen] });
    r.items.forEach((c) => { Q2.seen.add(c.id); Q2.items.push(c); });
    Q2.scored = r.scored; Q2.pending = r.pending; Q2.loading = false;
  };
  const warm = () => Q2.items.slice(Q2.i + 1, Q2.i + 4).forEach((c) => fetch(urlOf(c)).catch(() => {}));
  const play = () => { const c = Q2.items[Q2.i]; if (c) audio.play(c.rec, c.clip.start, c.clip.end); };
  const row = (c, cur) => `<div class="qj ${cur ? "cur" : ""}" data-id="${c.id}">
    <div class="qj-meta"><span class="faint small">${esc(fmtDate(c.date).split(" · ")[0])}</span>${c.tag ? `<span class="pill">${esc(c.tag)}</span>` : `<span class="faint small" title="the overnight pass hasn't looked at this card yet — showing her longest line">✨ not scored yet</span>`}</div>
    ${c.setup ? `<div class="qj-setup">${EMOJI[c.setup.spk] || ""} ${esc(c.setup.t)}</div>` : ""}
    <div class="qj-punch">${EMOJI[c.punch.spk] || ""} ${esc(c.punch.t)} <button type="button" class="btn small ghost qj-play">▶</button></div></div>`;
  const draw = () => {
    if (Q2.items.length - Q2.i < 8) more().then(() => { if (current === "quick") draw(); });
    const rows = Q2.items.slice(Q2.i, Q2.i + 8);
    $("#view").innerHTML = `<div class="quick"><div class="row"><h2 style="margin:0">⚡ Quick-judge</h2><span class="spacer"></span>
      <span class="muted small">${Q2.judged} judged this session · ${Q2.pending.toLocaleString()} waiting · ${Q2.scored} have a punchline pick</span></div>
      <div class="keys small"><kbd>Y</kbd> keep · <kbd>N</kbd> skip · <kbd>L</kbd> log only · <kbd>O</kbd> open full card · <kbd>U</kbd> undo (as many as you like) · <kbd>R</kbd> replay · <kbd>Space</kbd> pause</div>
      ${rows.length ? rows.map((c, k) => row(c, k === 0)).join("") : `<p class="muted">${Q2.loading ? "Loading…" : "Nothing left."}</p>`}
      <div class="qbar"><button type="button" class="btn" data-q="undo">↶<small>Undo</small></button><button type="button" class="btn" data-q="play">▶<small>Replay</small></button><button type="button" class="btn" data-q="open">⤢<small>Full card</small></button>
        <button type="button" class="btn x" data-q="rejected">✕<small>Skip</small></button><button type="button" class="btn" data-q="log">📓<small>Log</small></button><button type="button" class="btn j" data-q="journal">✓<small>Keep</small></button></div></div>`;
    $$(".qbar [data-q]").forEach((b) => (b.onclick = () => { const q = b.dataset.q;
      if (q === "undo") undo(); else if (q === "play") play();
      else if (q === "open") { const c = Q2.items[Q2.i]; if (c) { T.pin = c.id; T.opts = { ...T.opts, stage: "triage" }; location.hash = "#triage"; } }
      else decide(q); }));
    $$(".qj-play").forEach((b) => (b.onclick = (e) => { e.stopPropagation(); play(); }));
    if (rows.length) { play(); warm(); }
  };
  const decide = (d) => {
    const c = Q2.items[Q2.i]; if (!c) return;
    Q2.undo.push({ id: c.id, i: Q2.i }); Q2.i++; Q2.judged++; Q2.pending = Math.max(0, Q2.pending - 1); draw();
    api(`/api/moment/${c.id}/triage`, { decision: d }).catch((e) => toast("Save failed: " + e.message)); refreshStatus();
  };
  const undo = () => {
    const u = Q2.undo.pop(); if (!u) return toast("Nothing to undo");
    Q2.i = u.i; Q2.judged = Math.max(0, Q2.judged - 1); Q2.pending++; draw();
    api(`/api/moment/${u.id}/triage`, { decision: "pending" }).catch(() => {}); refreshStatus(); toast("Undone");
  };
  document.onkeydown = (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    const k = e.key.toLowerCase();
    if (k === "y") decide("journal"); else if (k === "n") decide("rejected"); else if (k === "l") decide("log");
    else if (k === "u") undo(); else if (k === "r") play(); else if (k === " ") { e.preventDefault(); audio.toggle(); }
    else if (k === "o") { const c = Q2.items[Q2.i]; if (c) { T.pin = c.id; T.opts = { ...T.opts, stage: "triage" }; location.hash = "#triage"; } }
  };
  $("#view").innerHTML = `<p class="muted">Loading the cards…</p>`;
  await more(); draw();
};


/* ------------------------------------------------------------------ 📖 Journal (Library): the whole site, plus what the site hides */
views.journal = async () => {
  $("#view").innerHTML = `<p class="muted">Reading the journal…</p>`;
  const d = await api("/api/journal"), t = d.totals;
  const md = (txt) => (txt || "").split(/\n\s*\n/).map((p) => `<p>${esc(p.trim()).replace(/\*([^*]+)\*/g, "<em>$1</em>")}</p>`).join("");
  const MON = ["", "January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
  const label = (e) => e.heading || (e.undated ? "Undated" : e.date ? new Date(e.date + "T12:00:00").toLocaleDateString(undefined, { weekday: "long", month: "long", day: "numeric" }) : e.file);
  const priv = (m) => {
    if (!m) return `<div class="jn-priv faint small">🔒 (moment ${""}not found in the database)</div>`;
    const rows = [];
    if (m.ai_note) rows.push(`<div><b>AI note</b> (why it was picked): ${esc(m.ai_note)}</div>`);
    if (m.private_note) rows.push(`<div><b>Your private note:</b> ${esc(m.private_note)}</div>`);
    if (m.curly.length) rows.push(`<div><b>{Notes to yourself}</b> in the lines: ${m.curly.map((c) => `“${esc(c)}”`).join(" · ")}</div>`);
    if (m.hidden_lines.length) rows.push(`<details><summary>${m.hidden_lines.length} line(s) you left out / merged away</summary>${m.hidden_lines.map((l) => `<div class="faint small">${EMOJI[l.speaker] || ""} ${esc(l.text)}</div>`).join("")}</details>`);
    rows.push(`<div class="faint small">🎙 ${esc(m.rec.filename)} · ${esc(fmtDate(m.rec.recorded_at))}${m.rec.title ? " · " + esc(m.rec.title) : ""} · moment ${m.id}${m.star ? " · ⭐" : ""}${m.voice_on_site ? " · 🔊 her voice is on the site" : ""}
      ${m.clip ? `<button type="button" class="btn small ghost jn-play" data-rec="${m.rec.id}" data-a="${m.clip.start}" data-b="${m.clip.end}">▶ play the recording</button>` : ""}</div>`);
    return `<div class="jn-priv">🔒 <b>Private</b> — not shown on the site${rows.map((r) => `<div class="jn-pr">${r}</div>`).join("")}</div>`;
  };
  const entryHtml = (e) => `<section class="jn-e" data-q="${esc((label(e) + " " + e.blocks.map((b) => b.text + (b.moment ? " " + [b.moment.ai_note, b.moment.private_note, ...(b.moment.curly || [])].join(" ") : "")).join(" ")).toLowerCase())}" data-y="${e.year}">
    <h3>${esc(label(e))} <span class="faint small">${e.date && !e.undated ? e.date : ""} ${e.on_site ? "" : "· <b>not on the site</b>"}</span></h3>
    ${e.blocks.map((b) => b.kind === "moment" ? `<div class="jn-b">${md(b.text)}${priv(b.moment)}</div>` : `<div class="jn-b imp">${md(b.text)}<div class="faint small">imported from the diary document — no private context stored</div></div>`).join("")}</section>`;
  const site = d.entries.filter((e) => e.on_site).sort((a, b) => (b.date || "").localeCompare(a.date || "") || b.file.localeCompare(a.file));
  const off = d.entries.filter((e) => !e.on_site);
  const years = [...new Set(site.map((e) => e.year))].sort().reverse();
  $("#view").innerHTML = `<div class="journal"><div class="row"><h2 style="margin:0">📖 Journal</h2><span class="spacer"></span>
    <input type="search" id="jn-q" placeholder="search the journal and the private notes…" style="width:300px"></div>
    <p class="muted small">Everything the site shows — <b>${t.on_site}</b> entries, <b>${t.moments_from_app}</b> quote blocks from this app (with their private notes) and <b>${t.imported_blocks}</b> imported from the diary document —
      read straight from the site's source files, so it's always complete. ${t.ready_to_publish} ready to publish · ${t.in_polish} in Polish · ${t.private_only} private-only.</p>
    <div class="row small"><label><input type="checkbox" id="jn-priv" checked> show private context</label>
      ${years.map((y) => `<button type="button" class="btn ghost small jn-y" data-y="${y}">${y}</button>`).join(" ")}<button type="button" class="btn ghost small jn-y" data-y="">all</button><span class="spacer"></span><span class="muted small" id="jn-n"></span></div>
    <div id="jn-list">${site.map(entryHtml).join("")}</div>
    ${off.length || d.private_files.length ? `<h2>🔒 Not on the site</h2>${off.map(entryHtml).join("")}${d.private_files.map((f) => `<section class="jn-e"><h3>${esc(f.file)} <span class="faint small">· private folder (never built)</span></h3><div class="jn-b">${md(f.text)}</div></section>`).join("")}` : ""}</div>`;
  const apply = () => {
    const q = $("#jn-q").value.trim().toLowerCase(), y = $("#jn-list").dataset.y || ""; let n = 0;
    $$("#jn-list .jn-e").forEach((el) => { const ok = (!q || el.dataset.q.includes(q)) && (!y || el.dataset.y === y); el.hidden = !ok; n += ok; });
    $("#jn-n").textContent = `${n} entries shown`;
  };
  $("#jn-q").oninput = apply;
  $("#jn-priv").onchange = (e) => $(".journal").classList.toggle("no-priv", !e.target.checked);
  $$(".jn-y").forEach((b) => (b.onclick = () => { $("#jn-list").dataset.y = b.dataset.y; apply(); }));
  $$(".jn-play").forEach((b) => (b.onclick = () => audio.play(b.dataset.rec, +b.dataset.a, +b.dataset.b)));
  apply();
};

/* ------------------------------------------------------------------ 📖 Reader: one recording, one continuous conversation */
views.reader = async () => (HP.get("rec") ? openReader(HP.get("rec"), HP.get("seg")) : readerList());

async function readerList() {
  const month = HP.get("month") || "", kind = HP.get("kind") || "", q = HP.get("q") || "";
  $("#view").innerHTML = `<div class="reader"><div class="row"><h2 style="margin:0">📖 Reader</h2><span class="muted small" style="margin-left:12px">pick a recording — read it as one conversation, no card boundaries</span><span class="spacer"></span>
    <input type="month" id="rl-m" value="${esc(month)}"> <select id="rl-k"><option value="">any kind</option>${["family archive", "work meeting", "personal note", "TV-media", "noise-empty"].map((k) => `<option ${k === kind ? "selected" : ""}>${k}</option>`).join("")}</select>
    <input type="search" id="rl-q" placeholder="title / summary…" value="${esc(q)}"></div><div id="rl-list" class="muted">Loading…</div></div>`;
  const go = () => { const p = new URLSearchParams(); if ($("#rl-m").value) p.set("month", $("#rl-m").value); if ($("#rl-k").value) p.set("kind", $("#rl-k").value); if ($("#rl-q").value.trim()) p.set("q", $("#rl-q").value.trim()); location.hash = "#reader" + (p.toString() ? "?" + p : ""); };
  $("#rl-m").onchange = go; $("#rl-k").onchange = go; $("#rl-q").onkeydown = (e) => e.key === "Enter" && go();
  const r = await api("/api/reader/list?" + new URLSearchParams({ month, kind, q }));
  $("#rl-list").innerHTML = r.items.length ? r.items.map((x) => `<a class="rl-row" href="#reader?rec=${x.id}"><span class="rl-d">${esc(fmtDate(x.recorded_at).split(" · ").join(" "))}</span>
    <span class="rl-t">${esc(x.title || x.filename)} ${x.kind ? `<span class="pill">${esc(x.kind)}</span>` : ""}</span>
    <span class="faint small">${fmtT(x.duration || 0)} · ${x.category || ""} · her ${x.her_lines} lines</span>
    <span class="rl-c">${x.waiting ? `⏳${x.waiting} ` : ""}${x.kept ? `★${x.kept} ` : ""}${x.punch ? `✨${x.punch}` : ""}</span></a>`).join("") : "No recordings match.";
}

const RD = { id: null, segs: [], vis: [], rows: [], moms: [], sel: null, anchor: null, only: false, targets: [], cur: -1, focus: -1, undo: [], playing: -1, auto: true, dur: 0, hooked: false, cards: {} };
const spkOrder = ["kid", "dad", "mom", "other"];
const bucketOf = (st) => ({ pending: "waiting", later: "waiting", journal: "kept", ready: "kept", published: "kept", log: "log" }[st] || "skipped");

async function openReader(recId, focusSeg) {
  $("#view").innerHTML = `<p class="muted">Loading the recording…</p>`;
  const d = await api(`/api/reader/${recId}`);
  RD.id = recId; RD.rec = d.rec; RD.segs = d.segs = d.segments; RD.moms = d.moments; RD.summary = d.summary; RD.dur = d.rec.duration || 0; RD.sel = null; RD.anchor = null; RD.cards = {};
  buildReader(); drawReader(focusSeg ? +focusSeg : null);
  if (!RD.hooked) { RD.hooked = true;
    player.addEventListener("timeupdate", readerTick);
    player.addEventListener("ended", () => { if (current === "reader" && RD.auto && audio.end < RD.dur - 1) readerPlayFrom(audio.end); });
  }
  document.onkeydown = readerKeys;
}

function buildReader() {
  const vis = RD.segs.filter((s) => ["", "media"].includes(s.f)); RD.vis = vis; vis.forEach((s, i) => (s._i = i));
  const cover = (idx) => RD.moms.filter((m) => idx >= m.seg_from && idx <= m.seg_to);
  vis.forEach((s) => {
    s.cards = cover(s.idx);
    s.reviewed = s.cards.length > 0 && !s.cards.some((c) => ["pending", "later"].includes(c.status));
    s.uncovered = s.cards.length === 0;
    s.punch = RD.moms.find((m) => m.punch_seg === s.id);
  });
  // unreviewed her-stretches: runs of uncovered lines in which she says something
  RD.stretchStart = new Set(); let run = [];
  const flush = () => { if (run.some((x) => x.sp === "kid" && (x.t || "").length >= 8)) RD.stretchStart.add(run[0].id); run = []; };
  vis.forEach((s) => { if (s.uncovered) run.push(s); else flush(); }); flush();
  RD.targets = vis.map((s, i) => [s, i]).filter(([s]) => s.punch || RD.stretchStart.has(s.id) || s.cards.some((c) => ["pending", "later"].includes(c.status) && c.seg_from === s.idx)).map(([, i]) => i);
}

function drawReader(focusId) {
  const keepY = window.scrollY, vis = RD.vis;
  const rows = []; let prev = null, hid = [], rev = 0;
  const flushHid = () => { if (hid.length) { rows.push(`<div class="rd-hid" data-hid="${hid.map((h) => h.id).join(",")}">▸ ${hid.length} hidden line(s) (${[...new Set(hid.map((h) => h.f))].join(", ")})</div>`); hid = []; } };
  const flushRev = () => { if (rev) { rows.push(`<div class="rd-hid" data-showrev="1">▸ ${rev} line(s) already reviewed — click to show</div>`); rev = 0; } };
  RD.segs.forEach((s) => {
    if (!["", "media"].includes(s.f)) { if (!RD.only) hid.push(s); return; }
    flushHid();
    const i = s._i;
    if (RD.only && s.reviewed && !RD.showRev) { rev++; prev = s; return; }
    flushRev();
    if (prev && s.s - prev.e > 5) rows.push(`<div class="rd-gap">· ${Math.round(s.s - prev.e)} s ·</div>`);
    const her = s.sp === "kid";
    const bars = s.cards.slice(0, 3).map((c) => `<i class="rd-bar ${bucketOf(c.status)}" title="card #${c.id} · ${c.status}${c.punch_tag ? " · " + esc(c.punch_tag) : ""}"></i>`).join("") || (RD.stretchStart.has(s.id) || (s.uncovered && her) ? `<i class="rd-bar open" title="not in any card"></i>` : "");
    const starts = s.cards.filter((c) => c.seg_from === s.idx).map((c) => `<a class="rd-chip ${bucketOf(c.status)}" data-card="${c.id}" title="open this card">#${c.id} ${bucketOf(c.status)}</a>`).join("");
    rows.push(`<div class="rd-l ${her ? "her" : "adult"} ${s.src === "manual" || s.x ? "edited" : ""}" data-i="${i}" data-id="${s.id}" id="rd-${s.id}">
      <div class="rd-gut"><span class="rd-t">${fmtT(s.s)}</span>${bars}</div>
      <div class="rd-main">${starts}<span class="spk ${s.sp} ${s.conf ? "" : "guess"} rd-spk" data-k="spk" title="click to change speaker">${EMOJI[s.sp] || ""}</span>
        <span class="rd-txt">${esc(s.t)}</span>${s.punch ? ` <span class="rd-punch" title="the overnight pass's pick">✨ ${esc(s.punch.punch_tag || "best line")}</span>` : ""}${s.f === "media" ? ` <span class="faint small">media</span>` : ""}
        <span class="rd-tools"><button type="button" data-a="edit" title="edit this line (type / between turns to split)">✎</button><button type="button" data-a="echo" title="she (or you) repeated this — copy it below as the other person">↻</button><button type="button" data-a="hide" title="hide this line">🙈</button></span></div></div>`);
    prev = s;
  });
  flushHid(); flushRev();
  const sm = RD.summary;
  $("#view").innerHTML = `<div class="reader"><div class="rd-top"><a href="#reader${RD.rec.recorded_at ? "?month=" + RD.rec.recorded_at.slice(0, 7) : ""}">← recordings</a>
      <b>${esc(fmtDate(RD.rec.recorded_at))}</b> <span class="muted small">${esc(RD.rec.title || "")} · ${fmtT(RD.dur)} · ${esc(RD.rec.category || "")}</span><span class="spacer"></span>
      <label class="small"><input type="checkbox" id="rd-only" ${RD.only ? "checked" : ""}> only stretches I haven't reviewed</label>
      <span class="small faint">${RD.targets.length} highlights</span></div>
    <div class="bar-audio" id="abar"></div>
    ${sm ? `<div class="rd-sum small"><b>${esc(sm.kind || "")}</b> — ${esc(sm.summary || "")}</div>` : ""}
    <div class="keys small faint"><kbd>J</kbd>/<kbd>K</kbd> next / previous highlight or unreviewed stretch · click a line to play from it · shift-click or drag the time gutter to select · <kbd>Space</kbd> play/pause · selection: <kbd>Y</kbd> keep · <kbd>L</kbd> log · <kbd>X</kbd> skip stretch · <kbd>E</kbd> edit as text · <kbd>O</kbd> open as card · <kbd>Esc</kbd> clear · <kbd>U</kbd> undo</div>
    <div id="rd-body">${rows.join("")}</div><div id="rd-bar" class="rd-actions" hidden></div><div id="rd-edit"></div></div>`;
  $("#rd-only").onchange = (e) => { RD.only = e.target.checked; RD.showRev = false; drawReader(); };
  renderAudioBar();
  wireReader();
  paintSel();
  if (focusId) { const el = $(`#rd-${focusId}`); if (el) { el.scrollIntoView({ block: "center" }); el.classList.add("focus"); } } else window.scrollTo(0, keepY);
}

function wireReader() {
  const body = $("#rd-body");
  $$(".rd-hid", body).forEach((el) => (el.onclick = () => {
    if (el.dataset.showrev) { RD.showRev = true; return drawReader(); }
    const ids = el.dataset.hid.split(",").map(Number); el.outerHTML = ids.map((id) => { const s = RD.segs.find((x) => x.id === id); return `<div class="rd-l hidden-line"><div class="rd-gut"><span class="rd-t">${fmtT(s.s)}</span></div><div class="rd-main"><span class="rd-txt faint">${esc(s.t)}</span> <span class="faint small">${esc(s.f)}</span></div></div>`; }).join("");
  }));
  $$(".rd-l[data-i]", body).forEach((el) => {
    const i = +el.dataset.i, s = RD.vis[i];
    el.onclick = (e) => {
      if (e.target.closest(".rd-tools,.rd-chip,.rd-spk,input")) return;
      if (e.shiftKey && RD.anchor != null) { RD.sel = [Math.min(RD.anchor, i), Math.max(RD.anchor, i)]; paintSel(); return; }
      RD.anchor = i; RD.sel = [i, i]; RD.focus = i; paintSel(); readerPlayFrom(s.s);
    };
    $(".rd-gut", el).onmousedown = (e) => { e.preventDefault(); RD.anchor = e.shiftKey && RD.anchor != null ? RD.anchor : i; RD.sel = [Math.min(RD.anchor, i), Math.max(RD.anchor, i)]; RD.drag = true; paintSel(); };
    el.onmouseenter = () => { if (RD.drag) { RD.sel = [Math.min(RD.anchor, i), Math.max(RD.anchor, i)]; paintSel(); } };
    $(".rd-spk", el).onclick = async (e) => { e.stopPropagation(); const nx = spkOrder[(spkOrder.indexOf(s.sp) + 1) % 4]; s.sp = nx; s.conf = 1; await cardFor(s.id, s.id).then((mid) => api(`/api/moment/${mid}/segment`, { seg_id: s.id, speaker: nx, via: "reader" })); drawReader(); };
    $$(".rd-tools button", el).forEach((b) => (b.onclick = (e) => { e.stopPropagation(); lineTool(b.dataset.a, s, el); }));
    $$(".rd-chip", el).forEach((c) => (c.onclick = (e) => { e.stopPropagation(); T.pin = +c.dataset.card; T.opts = { ...T.opts, stage: "triage" }; location.hash = "#triage"; }));
  });
  document.onmouseup = () => { RD.drag = false; };
}

const cardFor = async (a, b) => { const k = a + "-" + b; return RD.cards[k] || (RD.cards[k] = (await api("/api/reader/card", { rec: RD.id, a, b })).id); };
async function reloadReader(keepSel) {
  const d = await api(`/api/reader/${RD.id}`); RD.segs = d.segments; RD.moms = d.moments; RD.cards = {}; buildReader(); if (!keepSel) { RD.sel = null; } drawReader();
}
async function lineTool(a, s, el) {
  if (a === "hide") { const mid = await cardFor(s.id, s.id); await api(`/api/moment/${mid}/hide`, { seg_id: s.id, via: "reader" }); toast("Hidden ✓"); return reloadReader(true); }
  if (a === "echo") { const mid = await cardFor(s.id, s.id); const other = s.sp === "kid" ? "dad" : "kid"; await api(`/api/moment/${mid}/addline`, { text: s.t, speaker: other, after_seg_id: s.id, via: "echo" }); toast("Echo added ↻"); return reloadReader(true); }
  if (a === "edit") {
    const box = $(".rd-txt", el); const inp = document.createElement("input"); inp.type = "text"; inp.value = s.t; inp.className = "rd-inp"; box.replaceWith(inp); inp.focus();
    const done = async (save) => {
      const val = inp.value.trim();
      if (save && val && val !== s.t) {
        const mid = await cardFor(s.id, s.id), parts = splitParts(val, s.sp, s.sp === "mom" ? "mom" : "dad");
        if (parts) await api(`/api/moment/${mid}/split`, { seg_id: s.id, parts: parts.map((p) => ({ text: p.text, speaker: p.speaker })), via: "reader" });
        else await api(`/api/moment/${mid}/segment`, { seg_id: s.id, text: val, via: "reader" });
        toast(parts ? `Split into ${parts.length} lines` : "Saved"); return reloadReader(true);
      }
      drawReader();
    };
    inp.onkeydown = (e) => { e.stopPropagation(); if (e.key === "Enter" && !e.isComposing) done(true); if (e.key === "Escape") done(false); };
    inp.onblur = () => setTimeout(() => { if (document.body.contains(inp)) done(false); }, 150);
  }
}

function paintSel() {
  $$("#rd-body .rd-l.sel").forEach((e) => e.classList.remove("sel"));
  const bar = $("#rd-bar"); if (!bar) return;
  if (!RD.sel) { bar.hidden = true; return; }
  const [a, b] = RD.sel; for (let i = a; i <= b; i++) { const el = $(`#rd-${RD.vis[i].id}`); if (el) el.classList.add("sel"); }
  const first = RD.vis[a], last = RD.vis[b], overlap = RD.moms.filter((m) => ["pending", "later"].includes(m.status) && m.seg_from <= last.idx && m.seg_to >= first.idx);
  bar.hidden = false;
  bar.innerHTML = `<b>${b - a + 1} line(s)</b> · ${fmtT(first.s)}–${fmtT(last.e)}${overlap.length ? ` · overlaps ${overlap.length} waiting card(s)` : ""}
    <button class="btn primary" data-sa="keep">⭐ Keep <kbd>Y</kbd></button><button class="btn" data-sa="log">Log <kbd>L</kbd></button><button class="btn" data-sa="skip">Skip stretch <kbd>X</kbd></button>
    <button class="btn ghost" data-sa="edit">✎ edit as text <kbd>E</kbd></button><button class="btn ghost" data-sa="open">open as card <kbd>O</kbd></button><button class="btn ghost" data-sa="clear">✕ <kbd>Esc</kbd></button>`;
  $$("[data-sa]", bar).forEach((x) => (x.onclick = () => selAction(x.dataset.sa)));
}

async function selAction(a) {
  if (!RD.sel) return; const [i, j] = RD.sel, A = RD.vis[i].id, B = RD.vis[j].id;
  if (a === "clear") { RD.sel = null; RD.anchor = null; return paintSel(); }
  if (a === "open") { T.pin = await cardFor(A, B); T.opts = { ...T.opts, stage: "triage" }; location.hash = "#triage"; return; }
  if (a === "edit") {
    const mid = await cardFor(A, B), m = await api(`/api/moment/${mid}`);
    return openTextEditor(m, false, { host: $("#rd-edit"), onDone: (changed) => { $("#rd-edit").innerHTML = ""; if (changed) reloadReader(true); } });
  }
  const u = await api("/api/reader/span", { rec: RD.id, a: A, b: B, action: a });
  RD.undo.push(u); const n = u.superseded.length;
  toast(a === "keep" ? `Kept ⭐ as card #${u.new.id}${n ? ` — replaced ${n} waiting card(s)` : ""}` : a === "log" ? `Logged as card #${u.new.id}${n ? ` — replaced ${n}` : ""}` : `Skipped${n ? ` — ${n} waiting card(s) rejected` : ""}`);
  RD.sel = null; RD.anchor = null; refreshStatus(); reloadReader();
}

function readerPlayFrom(t) {
  RD.playT = t; audio.play(RD.id, Math.max(0, t - 0.15), Math.min(t + 45, RD.dur || t + 45));
}
function readerTick() {
  if (current !== "reader" || !RD.vis.length) return;
  const t = audio.start + (player.currentTime || 0);
  let lo = 0, hi = RD.vis.length - 1, k = -1;
  while (lo <= hi) { const mid = (lo + hi) >> 1; if (RD.vis[mid].s <= t + 0.05) { k = mid; lo = mid + 1; } else hi = mid - 1; }
  if (k === RD.playing) return; RD.playing = k;
  $$("#rd-body .rd-l.playing").forEach((e) => e.classList.remove("playing"));
  const el = k >= 0 && $(`#rd-${RD.vis[k].id}`); if (!el) return;
  el.classList.add("playing");
  const r = el.getBoundingClientRect(); if (r.top < 90 || r.bottom > innerHeight - 90) el.scrollIntoView({ block: "center", behavior: "smooth" });
}
function jumpTarget(dir) {
  if (!RD.targets.length) return toast("No highlights in this recording");
  const here = RD.focus >= 0 ? RD.focus : (RD.playing >= 0 ? RD.playing : -1);
  const cand = dir > 0 ? RD.targets.find((i) => i > here) : [...RD.targets].reverse().find((i) => i < here);
  const i = cand ?? (dir > 0 ? RD.targets[0] : RD.targets[RD.targets.length - 1]);
  RD.focus = i; const s = RD.vis[i], el = $(`#rd-${s.id}`);
  $$("#rd-body .rd-l.focus").forEach((e) => e.classList.remove("focus"));
  if (el) { el.classList.add("focus"); el.scrollIntoView({ block: "center", behavior: "smooth" }); }
  toast(s.punch ? `✨ ${s.punch.punch_tag || "best line"}` : RD.stretchStart.has(s.id) ? "unreviewed stretch" : "waiting card");
}
function readerKeys(e) {
  if (e.metaKey || e.ctrlKey || e.altKey || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName) || e.target.isContentEditable) return;
  const k = e.key.toLowerCase();
  if (k === "j") jumpTarget(1); else if (k === "k") jumpTarget(-1);
  else if (k === " ") { e.preventDefault(); if (audio.src && !player.paused) player.pause(); else if (audio.src && player.currentTime > 0 && !player.ended) player.play(); else { const s = RD.vis[RD.focus >= 0 ? RD.focus : 0]; if (s) readerPlayFrom(s.s); } }
  else if (k === ",") audio.slower();
  else if (k === "escape") selAction("clear");
  else if (RD.sel && k === "y") selAction("keep"); else if (RD.sel && k === "l") selAction("log"); else if (RD.sel && k === "x") selAction("skip");
  else if (RD.sel && k === "e") { e.preventDefault(); selAction("edit"); } else if (RD.sel && k === "o") selAction("open");
  else if (k === "u") { const u = RD.undo.pop(); if (!u) return toast("Nothing to undo"); api("/api/reader/span/undo", u).then(() => { toast("Undone"); refreshStatus(); reloadReader(); }); }
}


/* ------------------------------------------------------------------ 🧹 Sort the rest: recordings that aren't about her */
views.sort = async () => {
  const S = { kind: "", unsorted: true, noOnly: false, sel: new Set(), view: "rows", tag: "" };
  const CH = [["family archive", "🏠 Family archive"], ["work meeting", "💼 Work"], ["personal note", "📝 Personal"], ["delete candidate", "🗑 Delete candidate"]];
  const load = async () => {
    const r = await api(`/api/sort/list?${new URLSearchParams({ kind: S.kind, unsorted: S.unsorted ? 1 : "" })}`);
    let items = r.items; if (S.noOnly) items = items.filter((x) => x.keep === "no");
    const st = r.status;
    $("#view").innerHTML = `<div class="sortv"><div class="row"><h2 style="margin:0">🧹 Sort the rest</h2><span class="spacer"></span>
      <span class="muted small">${st.in_scope} recordings that aren't about her · ${st.summarised} summarised${st.in_scope > st.summarised ? ` — <code>./vjs.sh sort --summarize</code> fills the rest` : ""} · ${st.delete_candidates} delete candidate(s)</span></div>
      <p class="muted small">Nothing is ever deleted: <b>Delete candidate</b> moves the file to <code>~/Documents/Recordings/_to_delete/</code> (undo any time; you empty that folder yourself). Family archive / Work / Personal keep the recording, transcribe it for the Reader and Search, and never mine it for moments.</p>
      <div class="row small"><button class="btn small ${S.view === "rows" ? "primary" : "ghost"}" id="so-vr">Recordings</button><button class="btn small ${S.view === "places" ? "primary" : "ghost"}" id="so-vp">📍 Places &amp; inventory</button>
        <button class="btn small ghost" id="so-csv" title="download everything on this screen as a spreadsheet (stays on your Mac)">⬇ export CSV</button></div>
      <div class="row small"><select id="so-k"><option value="">all kinds</option>${["family archive", "work meeting", "personal note", "TV-media", "noise-empty", "delete candidate"].map((k) => `<option ${k === S.kind ? "selected" : ""}>${k}</option>`).join("")}</select>
        <label><input type="checkbox" id="so-un" ${S.unsorted ? "checked" : ""}> not sorted yet</label> <label><input type="checkbox" id="so-no" ${S.noOnly ? "checked" : ""}> "worth keeping? no" only</label>
        <span class="spacer"></span><button class="btn small ghost" id="so-all">select all shown</button><span id="so-bulk"></span></div>
      <div id="so-list">${items.map((x) => `<div class="so-row" data-id="${x.id}"><label class="so-c"><input type="checkbox" class="so-sel" ${S.sel.has(x.id) ? "checked" : ""}></label>
        <div class="so-m"><b>${esc(fmtDate(x.recorded_at).split(" · ")[0])}</b><div class="faint small">${fmtT(x.duration || 0)}${x.title ? " · " + esc(x.title) : ""}</div>
          <button class="btn small ghost so-play" data-s="${x.sample_start}" title="30 s from the densest speech">▶ sample</button></div>
        <div class="so-s">${x.kind || x.user_kind ? `<span class="pill">${esc(x.user_kind || x.kind)}</span> ${x.keep ? `<span class="pill ${x.keep}">keep? ${x.keep}</span>` : ""}` : `<span class="faint small">no summary yet</span>`}
          <div>${esc(x.summary || "")}</div><div class="faint small">${x.speakers ? esc(x.speakers) + " · " : ""}${(x.topics || []).map(esc).join(" · ")}${x.keep_reason ? ` — ${esc(x.keep_reason)}` : ""}</div>
          <div>${labelChips(x.label)} <button class="btn small ghost so-lab">🏷 label</button></div></div>
        <div class="so-b">${x.status === "delete_candidate" ? `<span class="faint small">in _to_delete</span> <button class="btn small" data-restore="${x.id}">↩ restore</button>` : CH.map(([v, l]) => `<button class="btn small ${x.user_kind === v ? "primary" : "ghost"}" data-ch="${v}">${l}</button>`).join("")}</div></div>`).join("") || `<p class="faint">Nothing here.</p>`}</div></div>`;
    $("#so-vr").onclick = () => { S.view = "rows"; load(); }; $("#so-vp").onclick = () => { S.view = "places"; load(); };
    $("#so-csv").onclick = () => { const q = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`; const hdr = ["date", "minutes", "place/title", "kind", "keep?", "label tags", "label note", "summary", "status"];
      const csv = [hdr.join(",")].concat(r.items.map((x) => [x.recorded_at.slice(0, 16).replace("T", " "), Math.round((x.duration || 0) / 60), x.title || x.filename, x.user_kind || x.kind, x.keep, (x.label.tags || []).join("; "), x.label.note, x.summary, x.status].map(q).join(","))).join("\n");
      const a = document.createElement("a"); a.href = URL.createObjectURL(new Blob([csv], { type: "text/csv" })); a.download = "recordings-inventory.csv"; a.click(); };
    $("#so-k").onchange = (e) => { S.kind = e.target.value; load(); }; $("#so-un").onchange = (e) => { S.unsorted = e.target.checked; load(); }; $("#so-no").onchange = (e) => { S.noOnly = e.target.checked; load(); };
    const bulk = () => { $("#so-bulk").innerHTML = S.sel.size ? `<b>${S.sel.size} selected:</b> ${CH.map(([v, l]) => `<button class="btn small" data-bulk="${v}">${l}</button>`).join(" ")}` : ""; $$("[data-bulk]").forEach((b) => (b.onclick = () => act([...S.sel], b.dataset.bulk))); };
    const act = async (ids, choice) => {
      if (choice === "delete candidate" && ids.length > 1 && !confirm(`Move ${ids.length} recordings to _to_delete/? (nothing is deleted; you can restore them)`)) return;
      const r = await api("/api/sort/choose", { ids, choice }); S.sel.clear();
      toast(r.errors && r.errors.length ? "Some failed: " + r.errors[0] : choice === "delete candidate" ? `Moved ${r.moved} to _to_delete/` : `Kept ${r.changed} as ${choice}`); load();
    };
    if (S.view === "places") {
      const pr = await api("/api/places"), tot = pr.places.reduce((a, p) => ({ n: a.n + p.n, h: a.h + p.hours }), { n: 0, h: 0 });
      $("#so-list").innerHTML = `<p class="muted small">${pr.places.length} places · ${tot.n} recordings · ${tot.h.toFixed(0)} h that aren't about her. A place is the Voice Memos location title; label or decide a whole place at once.</p>` +
        pr.places.map((p) => `<div class="so-row pl" data-place="${esc(p.place)}"><div class="so-m"><b>${esc(p.place)}</b><div class="faint small">${p.n} rec · ${p.hours} h · ${p.first.slice(0, 7)}${p.last.slice(0, 7) !== p.first.slice(0, 7) ? " → " + p.last.slice(0, 7) : ""}</div></div>
          <div class="so-s">${Object.entries(p.kinds).map(([k, n]) => `<span class="pill">${esc(k)} ${n}</span>`).join(" ")} ${p.delete_candidates ? `<span class="pill no">${p.delete_candidates} in _to_delete</span>` : ""}<div>${labelChips({ note: p.notes[0], tags: p.tags })}</div></div>
          <div class="so-b"><button class="btn small" data-pl="label">🏷 label all</button>${CH.map(([v, l]) => `<button class="btn small ghost" data-pc="${v}">${l}</button>`).join("")}</div></div>`).join("");
      $$("#so-list .pl").forEach((row) => { const p = pr.places.find((x) => x.place === row.dataset.place);
        $("[data-pl]", row).onclick = () => labelDialog({ recIds: p.ids, title: `${p.n} recordings at “${p.place}”` }, load);
        $$("[data-pc]", row).forEach((b) => (b.onclick = async () => { if (!confirm(`${b.dataset.pc === "delete candidate" ? "Move" : "Mark"} all ${p.n} recordings at “${p.place}” as ${b.dataset.pc}?${b.dataset.pc === "delete candidate" ? " (nothing is deleted; they move to _to_delete/ and can be restored)" : ""}`)) return;
          const r = await api("/api/sort/choose", { ids: p.ids, choice: b.dataset.pc }); toast(r.errors && r.errors.length ? "Some failed: " + r.errors[0] : `Done: ${r.changed + r.moved} recordings`); load(); })); });
      $("#so-bulk").innerHTML = ""; return;
    }
    $$(".so-row").forEach((row) => {
      const id = row.dataset.id, x = items.find((i) => i.id === id);
      $(".so-sel", row).onchange = (e) => { e.target.checked ? S.sel.add(id) : S.sel.delete(id); bulk(); };
      $(".so-play", row).onclick = () => audio.play(id, +$(".so-play", row).dataset.s, +$(".so-play", row).dataset.s + 30);
      $$("[data-ch]", row).forEach((b) => (b.onclick = () => act([id], b.dataset.ch)));
      $(".so-lab", row).onclick = async () => { const pl = (await api(`/api/places?all=1`)).places.find((p) => p.place === x.place); labelDialog({ recId: id, title: `${x.title || x.filename} · ${fmtDate(x.recorded_at).split(" · ")[0]}`, place: x.place, nPlace: pl ? pl.n : 1, label: x.label }, load); };
      const rs = $("[data-restore]", row); if (rs) rs.onclick = async () => { const r = await api("/api/sort/restore", { id }); toast(r.errors.length ? r.errors[0] : "Restored"); load(); };
    });
    $("#so-all").onclick = () => { items.forEach((x) => S.sel.add(x.id)); $$(".so-sel").forEach((c) => (c.checked = true)); bulk(); };
    bulk();
  };
  document.onkeydown = (e) => { if (e.key === " " && !/INPUT|TEXTAREA|SELECT|BUTTON/.test(e.target.tagName)) { e.preventDefault(); audio.toggle(); } };
  $("#view").innerHTML = `<p class="muted">Loading…</p>`; await load();
};


/* ------------------------------------------------------------------ 🏷 labels & notes on recordings */
const labelChips = (lab) => lab && (lab.note || (lab.tags || []).length) ? `<span class="lab-chips">${(lab.tags || []).map((t) => `<span class="pill lab">${esc(t)}</span>`).join(" ")}${lab.note ? `<span class="faint small" title="${esc(lab.note)}"> 📝 ${esc(lab.note.length > 60 ? lab.note.slice(0, 58) + "…" : lab.note)}</span>` : ""}</span>` : "";
async function labelDialog({ recId, recIds, title, place, nPlace, label }, onSaved) {
  const known = (await api("/api/places")).tags;
  const old = $("#lab-modal"); if (old) old.remove();
  const el = document.createElement("div"); el.id = "lab-modal"; el.className = "modal-bg";
  el.innerHTML = `<div class="modal"><h3 style="margin-top:0">🏷 Label ${recIds ? recIds.length + " recordings" : "this recording"}</h3>
    <div class="faint small">${esc(title || place || "")}</div>
    <label class="f">Note — what / where / who (e.g. “Nepal trip — hanging out with Robin”)</label><textarea id="lab-note" rows="3" style="width:100%">${esc((label && label.note) || "")}</textarea>
    <label class="f">Tags (comma separated)</label><input id="lab-tags" list="lab-known" style="width:100%" value="${esc(((label && label.tags) || []).join(", "))}" placeholder="Nepal, Robin, trip">
    <datalist id="lab-known">${known.map((t) => `<option value="${esc(t)}">`).join("")}</datalist>
    ${nPlace > 1 ? `<label class="small" style="display:block;margin-top:8px"><input type="checkbox" id="lab-all"> also label all <b>${nPlace}</b> recordings at “${esc(place)}” (adds these tags; fills the note where there is none)</label>` : ""}
    <div class="row" style="margin-top:14px"><button class="btn primary" id="lab-save">Save <kbd>⌘↵</kbd></button><button class="btn ghost" id="lab-x">Cancel <kbd>Esc</kbd></button></div></div>`;
  document.body.appendChild(el); $("#lab-note").focus();
  const close = () => { el.remove(); };
  const save = async () => {
    const tags = $("#lab-tags").value.split(",").map((x) => x.trim()).filter(Boolean), note = $("#lab-note").value.trim(), all = $("#lab-all") && $("#lab-all").checked;
    const body = recIds ? { rec_ids: recIds, note, tags, mode: "add" } : all ? { rec: recId, place_all: true, note, tags, mode: "add" } : { rec: recId, note, tags, mode: "set" };
    const r = await api("/api/label", body); toast(`Labelled ${r.labelled} recording(s)`); close(); if (onSaved) onSaved();
  };
  $("#lab-save").onclick = save; $("#lab-x").onclick = close;
  el.addEventListener("keydown", (e) => { e.stopPropagation(); if (e.key === "Escape") close(); if ((e.metaKey || e.ctrlKey) && e.key === "Enter") save(); });
  el.onclick = (e) => { if (e.target === el) close(); };
}


/* ------------------------------------------------------------------ 📥 Inbox preview: look at what's queued, skip what isn't worth processing */
views.inbox = async () => {
  const I = { sel: new Set(), timer: null, desc: {} };
  const bar = (p) => { const n = Math.round((p || 0) / 10); return "█".repeat(n) + "░".repeat(10 - n); };
  const load = async () => {
    if (current !== "inbox") return clearInterval(I.timer);
    const r = await api("/api/inbox");
    const items = r.items, queued = items.filter((x) => ["new", "analyzed", "transcribed"].includes(x.status));
    const hours = queued.reduce((a, x) => a + (x.duration || 0), 0) / 3600;
    $("#view").innerHTML = `<div class="inboxv"><div class="row"><h2 style="margin:0">📥 Inbox</h2><span class="spacer"></span>
      <span class="muted small">${queued.length} queued · ${hours.toFixed(1)} h of audio${r.running ? ` · ● processing now: <b>${esc(r.current || "")}</b>` : ""}</span></div>
      <p class="muted small">Look before the machine does: ▶ plays 30 s from the densest speech. <b>Skip</b> keeps the file but doesn't process it (undo any time); <b>Delete candidate</b> moves it to <code>_to_delete/</code>. The speech bar shows how much of the recording is voices rather than silence or noise.</p>
      <div class="row small"><button class="btn small ghost" id="ib-all">select all queued</button><span id="ib-bulk"></span></div>
      <div id="ib-list">${items.map((x) => `<div class="ib-row ${x.status}" data-id="${x.id}">
        <label class="so-c"><input type="checkbox" class="ib-sel" ${I.sel.has(x.id) ? "checked" : ""} ${["new", "analyzed", "transcribed"].includes(x.status) ? "" : "disabled"}></label>
        <div class="ib-m"><b>${esc(fmtDate(x.recorded_at))}</b><div class="faint small">${fmtT(x.duration || 0)} · ${x.mb ?? "?"} MB${x.title ? " · " + esc(x.title) : ""}</div></div>
        <div class="ib-s"><code class="ib-bar" title="share of the recording that is speech">${x.speech_pct == null ? "measuring…" : bar(x.speech_pct) + " " + Math.round(x.speech_pct) + "%"}</code>
          <span class="pill ${x.status === "skipped" ? "no" : ""}">${{ new: "queued", analyzed: "processing", transcribed: "processing", skipped: "skipped", delete_candidate: "in _to_delete" }[x.status] || x.status}${r.current && x.filename === r.current ? " ●" : ""}</span>
          ${x.kind ? `<span class="pill">${esc(x.kind)}</span>` : ""}<div class="small">${esc((I.desc[x.id] && I.desc[x.id].summary) || x.summary || "")}</div></div>
        <div class="ib-b"><button class="btn small ghost ib-play" data-s="${x.sample_start || 0}">▶ sample</button>
          ${["new", "analyzed", "transcribed"].includes(x.status) ? `<button class="btn small ghost ib-what">🔍 what is it?</button><button class="btn small" data-m="skip">⏭ Skip</button><button class="btn small ghost" data-m="delete">🗑</button>` : `<button class="btn small primary" data-m="restore">↩ Restore</button>`}</div></div>`).join("") || `<p class="faint">Nothing queued.</p>`}</div></div>`;
    const bulk = () => { $("#ib-bulk").innerHTML = I.sel.size ? `<b>${I.sel.size} selected:</b> <button class="btn small" data-bm="skip">⏭ Skip all</button> <button class="btn small ghost" data-bm="delete">🗑 Delete candidates</button>` : ""; $$("[data-bm]").forEach((b) => (b.onclick = () => act([...I.sel], b.dataset.bm))); };
    const act = async (ids, mode) => { if (mode === "delete" && !confirm(`Move ${ids.length} recording(s) to _to_delete/? (nothing is deleted; restore any time)`)) return; const res = await api("/api/inbox/set", { ids, mode }); I.sel.clear(); toast(res.errors.length ? "Some failed: " + res.errors[0] : `${mode === "skip" ? "Skipped" : mode === "restore" ? "Restored" : "Moved"} ${res.changed}`); load(); };
    $("#ib-all").onclick = () => { queued.forEach((x) => I.sel.add(x.id)); $$(".ib-sel:not(:disabled)").forEach((c) => (c.checked = true)); bulk(); };
    $$(".ib-row").forEach((row) => { const id = row.dataset.id;
      $(".ib-sel", row).onchange = (e) => { e.target.checked ? I.sel.add(id) : I.sel.delete(id); bulk(); };
      $(".ib-play", row).onclick = () => audio.play(id, +$(".ib-play", row).dataset.s, +$(".ib-play", row).dataset.s + 30);
      $$("[data-m]", row).forEach((b) => (b.onclick = () => act([id], b.dataset.m)));
      const w = $(".ib-what", row); if (w) w.onclick = async () => { w.disabled = true; w.textContent = "listening…"; try { I.desc[id] = await api("/api/inbox/describe", { id }); } catch (e) { toast("Couldn't summarise: " + e.message); } load(); };
    });
    bulk();
    // measure speech share for rows that don't have it yet (no model, a second or two each)
    for (const x of queued.filter((q) => q.speech_pct == null)) { try { await api("/api/inbox/measure", { id: x.id }); } catch {} if (current !== "inbox") return; }
    if (queued.some((x) => x.speech_pct == null)) load();
  };
  document.onkeydown = (e) => { if (e.key === " " && !/INPUT|TEXTAREA|SELECT|BUTTON/.test(e.target.tagName)) { e.preventDefault(); audio.toggle(); } };
  await load(); I.timer = setInterval(() => { if (current === "inbox" && !I.sel.size) load(); }, 20000);
};
