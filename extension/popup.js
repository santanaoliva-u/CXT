const $ = (id) => document.getElementById(id);
const DEF = { server: "http://127.0.0.1:8799", token: "", enabled: true };
const FIELDS = [
  "server", "token", "sint", "scount", "stopic", "stext",
  "purl", "pexpect", "pfilter", "plimit", "ptext", "pdry",
  "gfilter", "glimit", "gtext", "gdry",
  "curl", "cmode", "ctext", "cint", "ccount", "cdry",
];
let enabled = true;
let busy = false;
let tick = 0;
let activeTab = "estado";

const send = (m) => chrome.runtime.sendMessage(m).catch(() => null);

function tabOf() {
  const b = document.querySelector("nav button.active");
  return b ? b.dataset.tab : "estado";
}

function saveForm() {
  const form = {};
  for (const id of FIELDS) {
    const el = $(id);
    if (!el) continue;
    form[id] = el.type === "checkbox" ? el.checked : el.value;
  }
  chrome.storage.local.set({ form, tab: tabOf() });
}

function paint() {
  const b = $("power");
  b.className = enabled ? "on" : "off";
  b.textContent = enabled ? "● ACTIVADA" : "● DESACTIVADA";
}

async function load() {
  const c = await chrome.storage.local.get({ ...DEF, form: {}, tab: "estado" });
  enabled = c.enabled;
  const f = c.form || {};
  for (const id of FIELDS) {
    const el = $(id);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = f[id] != null ? f[id] : el.checked;
    else if (f[id] != null) el.value = f[id];
  }
  paint();
  showTab(c.tab || "estado");
  await refreshAll();
}

function showTab(t) {
  activeTab = t;
  document.querySelectorAll("nav button").forEach((x) => x.classList.toggle("active", x.dataset.tab === t));
  document.querySelectorAll("main section").forEach((s) => s.classList.toggle("active", s.id === "t_" + t));
  if (t === "publicar") renderSched();
  else if (t === "compartir") { renderSharePost(); renderShare(); }
  else if (t === "comentar") renderComment();
  else if (t === "ajustes") renderGroups();
}

function renderResults(el, items, fmt) {
  if (!items || !items.length) { el.innerHTML = ""; return; }
  const shown = items.slice(-12);
  el.innerHTML = shown
    .map((it) => {
      const ok = it.ok !== false;
      return `<div><span class="${ok ? "ok" : "bad"}">${ok ? "✓" : "✗"}</span> ${fmt(it)}</div>`;
    })
    .join("");
}

const esc = (s) => String(s || "").replace(/[<>&]/g, (c) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c]));

/* ---------- estado ---------- */
async function render() {
  const s = await send({ type: "status" });
  const last = (await chrome.storage.local.get({ last: null })).last;
  const dot = $("dot");
  dot.className = "dot" + (s && s.running ? " on-dot" : enabled ? "" : " off-dot");
  if (s) { enabled = s.enabled; paint(); }
  $("conn").textContent = s ? (s.running ? "en línea" : "worker dormido") : "sin worker";
  let t = s
    ? `polling=${s.running} · activo=${s.enabled}`
    : "sin respuesta del worker";
  if (last) t += `\núltimo: ${last.op} ${last.ok ? "OK" : "ERR " + (last.error || "")} @ ${new Date(last.at).toLocaleTimeString()}`;
  $("status").textContent = t;
}

/* ---------- publicar ---------- */
async function renderSched() {
  const s = await send({ type: "schedGet" });
  const el = $("sstatus");
  $("b_pub").classList.toggle("show", !!(s && s.enabled));
  if (!s || s.error) { el.textContent = s && s.error ? "error: " + s.error : "sin datos"; return; }
  let t = `${s.enabled ? "PUBLICANDO" : "detenido"} · quedan ${s.remaining != null ? s.remaining : "?"} · ${s.alive ? "motor OK" : "motor PARADO"}`;
  if (s.enabled && s.next_at) t += `\npróxima: ${new Date(s.next_at).toLocaleTimeString()}`;
  if (s.last) t += `\núltima: ${s.last.ok ? "OK" : "ERR " + (s.last.error || "")} @ ${new Date(s.last.at).toLocaleTimeString()}`;
  el.textContent = t;
}

/* ---------- compartir texto nuevo ---------- */
async function renderShare() {
  const s = await send({ type: "shareGet" });
  const el = $("gstatus");
  if (!s || s.error) { el.textContent = s && s.error ? "error: " + s.error : "sin datos"; return; }
  if (!s.total) { el.textContent = "sin ejecuciones"; renderResults($("gresults"), [], () => ""); return; }
  let t = `${s.running ? "COMPARTIENDO" : "terminado"} ${s.done || 0}/${s.total} · OK ${s.published || 0}`;
  if (s.dry) t += " · SIMULACIÓN";
  if (s.current) t += `\nactual: ${s.current}`;
  el.textContent = t;
  renderResults($("gresults"), s.results, (r) => esc(r.name || r.group || ""));
}

/* ---------- compartir publicación ---------- */
async function renderSharePost() {
  const s = await send({ type: "sharePostGet" });
  const el = $("pstatus");
  $("b_share").classList.toggle("show", !!((s && s.running) || (s && s.total && s.running)));
  if (!s || s.error) { el.textContent = s && s.error ? "error: " + s.error : "sin datos"; return; }
  if (!s.total) { el.textContent = s.running ? "iniciando…" : "sin ejecuciones"; renderResults($("presults"), [], () => ""); return; }
  let t = `${s.running ? "COMPARTIENDO" : "terminado"} ${s.done || 0}/${s.total} · OK ${s.published || 0}`;
  if (s.dry) t += " · SIMULACIÓN";
  if (s.error) t += `\n${s.error}`;
  if (s.current) t += `\nactual: ${s.current}`;
  el.textContent = t;
  renderResults($("presults"), s.results, (r) => esc(r.group || "") + (r.stage ? ` <span class="hint">(${esc(r.stage)})</span>` : ""));
}

/* ---------- comentar ---------- */
async function renderComment() {
  const s = await send({ type: "commentGet" });
  const el = $("cstatus");
  if (!s || s.error) { el.textContent = s && s.error ? "error: " + s.error : "sin datos"; return; }
  if (!s.total) { el.textContent = s.running ? "iniciando…" : "sin ejecuciones"; renderResults($("cresults"), [], () => ""); return; }
  let t = `${s.running ? "COMENTANDO" : "terminado"} ${s.done || 0}/${s.total} · OK ${s.posted || 0}`;
  if (s.dry) t += " · SIMULACIÓN";
  if (s.error) t += `\n${s.error}`;
  if (s.current) t += `\n${s.current}`;
  el.textContent = t;
  renderResults($("cresults"), s.results, (r) => `#${r.i} ${esc((r.text || r.typed || "").slice(0, 60))}`);
}

/* ---------- grupos ---------- */
async function renderGroups() {
  const g = await send({ type: "groupscanGet" });
  const el = $("grstatus");
  if (!g || g.error) { el.textContent = g && g.error ? "error: " + g.error : "sin datos"; return; }
  if (g.alive) { $("b_grp").classList.add("show"); el.textContent = "escaneando… (tarda unos minutos)"; return; }
  $("b_grp").classList.remove("show");
  if (!g.count) { el.textContent = "sin grupos; pulsa ESCANEAR"; return; }
  const c = g.categories || {};
  const cats = Object.keys(c).map((k) => `${k}:${c[k]}`).join(" · ");
  el.textContent = `${g.count} grupos\n${cats}`;
}

/* ---------- acciones ---------- */
$("power").addEventListener("click", async () => {
  const next = !enabled;
  const r = await send({ type: "setEnabled", value: next });
  enabled = r && typeof r.enabled === "boolean" ? r.enabled : next;
  paint();
  await render();
});

async function probe() {
  const r = await send({ type: "test" });
  $("status").textContent = r && r.ok ? "conexión OK con cxtd" : "fallo: " + (r && (r.error || r.status));
}
$("test").addEventListener("click", probe);
$("test2").addEventListener("click", probe);

$("stopall").addEventListener("click", async () => {
  saveForm();
  await Promise.all([
    send({ type: "shareSet", value: { stop: true } }),
    send({ type: "sharePostSet", value: { stop: true } }),
    send({ type: "commentSet", value: { stop: true } }),
    send({ type: "schedSet", value: { enabled: false } }),
  ]);
  $("status").textContent = "deteniendo todo…";
  setTimeout(refreshAll, 800);
});

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    server: $("server").value.trim() || DEF.server,
    token: $("token").value.trim(),
  });
  saveForm();
  await send({ type: "start" });
  $("status").textContent = "guardado.";
  setTimeout(refreshAll, 300);
});

$("sstart").addEventListener("click", async () => {
  const interval = parseFloat($("sint").value) || 5;
  const count = Math.max(1, parseInt($("scount").value) || 1);
  const topic = $("stopic").value === "fixed" ? ($("stext").value.trim() || "auto") : "auto";
  if (!confirm(`¿Publicar ${count} vez/veces cada ${interval} min en tu muro?`)) return;
  saveForm();
  await send({ type: "schedSet", value: { enabled: true, interval_min: interval, count, topic } });
  $("sstatus").textContent = "iniciando…";
  setTimeout(renderSched, 700);
});

$("sstop").addEventListener("click", async () => {
  await send({ type: "schedSet", value: { enabled: false } });
  $("sstatus").textContent = "deteniendo…";
  setTimeout(renderSched, 700);
});

$("gstart").addEventListener("click", async () => {
  const filter = $("gfilter").value.trim();
  const limit = Math.max(0, parseInt($("glimit").value) || 0);
  const text = $("gtext").value.trim();
  const dry = $("gdry").checked;
  if (!text) { $("gstatus").textContent = "escribe el texto a compartir"; return; }
  if (!filter && limit === 0) { $("gstatus").textContent = "pon un filtro de grupos o un límite > 0"; return; }
  if (!dry && !confirm(`¿Publicar este texto en los grupos (${filter || "todos"})?`)) return;
  saveForm();
  await send({ type: "shareSet", value: { text, filter, limit, dry, interval_sec: 8 } });
  $("gstatus").textContent = "iniciando…";
  setTimeout(renderShare, 900);
});

$("gstop").addEventListener("click", async () => {
  await send({ type: "shareSet", value: { stop: true } });
  $("gstatus").textContent = "deteniendo…";
  setTimeout(renderShare, 900);
});

$("pstart").addEventListener("click", async () => {
  const url = $("purl").value.trim();
  const expect = $("pexpect").value.trim();
  const filter = $("pfilter").value.trim();
  const limit = Math.max(0, parseInt($("plimit").value) || 0);
  const text = $("ptext").value.trim();
  const dry = $("pdry").checked;
  if (!filter && limit === 0) { $("pstatus").textContent = "pon un filtro de grupos o un límite > 0"; return; }
  if (!dry && !confirm(`¿Compartir la publicación en los grupos (${filter || "todos"})?`)) return;
  saveForm();
  await send({ type: "sharePostSet", value: { url, expect, filter, limit, dry, text, delay: 8 } });
  $("pstatus").textContent = "iniciando…";
  setTimeout(renderSharePost, 900);
});

$("pstop").addEventListener("click", async () => {
  await send({ type: "sharePostSet", value: { stop: true } });
  $("pstatus").textContent = "deteniendo…";
  setTimeout(renderSharePost, 900);
});

$("cstart").addEventListener("click", async () => {
  const url = $("curl").value.trim();
  const fixed = $("cmode").value === "fixed";
  const text = fixed ? $("ctext").value.trim() : "";
  const auto = !fixed;
  const interval = parseFloat($("cint").value) || 0;
  const count = Math.max(1, parseInt($("ccount").value) || 1);
  const dry = $("cdry").checked;
  if (fixed && !text) { $("cstatus").textContent = "escribe el comentario"; return; }
  if (!dry && !confirm(`¿Comentar ${count} vez/veces?`)) return;
  saveForm();
  await send({ type: "commentSet", value: { url, text, auto, count, interval_min: interval, dry } });
  $("cstatus").textContent = "iniciando…";
  setTimeout(renderComment, 900);
});

$("cstop").addEventListener("click", async () => {
  await send({ type: "commentSet", value: { stop: true } });
  $("cstatus").textContent = "deteniendo…";
  setTimeout(renderComment, 900);
});

$("grscan").addEventListener("click", async () => {
  await send({ type: "groupscanSet", value: {} });
  $("grstatus").textContent = "escaneando…";
  setTimeout(renderGroups, 1200);
});

/* ---------- subtabs compartir ---------- */
function showShareMode(mode) {
  document.querySelectorAll("#segshare button").forEach((b) => b.classList.toggle("active", b.dataset.mode === mode));
  $("m_post").style.display = mode === "post" ? "block" : "none";
  $("m_text").style.display = mode === "text" ? "block" : "none";
}
document.querySelectorAll("#segshare button").forEach((b) =>
  b.addEventListener("click", () => showShareMode(b.dataset.mode))
);

/* ---------- tabs ---------- */
document.querySelectorAll("nav button").forEach((btn) =>
  btn.addEventListener("click", () => { showTab(btn.dataset.tab); saveForm(); })
);

/* ---------- persistencia ---------- */
for (const id of FIELDS) {
  const el = $(id);
  if (!el) continue;
  el.addEventListener("change", saveForm);
  if (el.tagName === "TEXTAREA" || el.type === "text") el.addEventListener("blur", saveForm);
}

/* ---------- refresco ---------- */
async function refreshAll() {
  await render();
  await renderSched();
  await renderSharePost();
  await renderShare();
  await renderComment();
  await renderGroups();
}

async function tickFn() {
  if (document.hidden || busy) return;
  busy = true;
  try {
    await render();
    const t = tabOf();
    if (t === "publicar") await renderSched();
    else if (t === "compartir") { await renderSharePost(); await renderShare(); }
    else if (t === "comentar") await renderComment();
    else if (t === "ajustes") await renderGroups();
    tick++;
    if (tick % 4 === 0) {
      if (t !== "publicar") await renderSched();
      if (t !== "compartir") { await renderSharePost(); await renderShare(); }
      if (t !== "comentar") await renderComment();
    }
    if (tick % 12 === 0) await renderGroups();
  } finally {
    busy = false;
  }
}

load();
setInterval(tickFn, 1500);
