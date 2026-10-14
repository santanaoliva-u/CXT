const $ = (id) => document.getElementById(id);
const FIELDS = ["server", "token", "sint", "scount", "stopic", "stext",
  "purl", "pexpect", "pfilter", "plimit", "ptext", "pdry",
  "gfilter", "glimit", "gtext", "gdry",
  "curl", "cmode", "ctext", "cint", "ccount", "cdry"];
const JOB = { pub: "scheduler", sharepost: "sharepost", share: "share", com: "comment", grp: "groupscan", auto: "autopost" };

let tab = "estado";
let mode = "post";
let form = {};
let busy = false;
let autoMsg = "";

function send(type, value, query) {
  return new Promise((res) => {
    try {
      chrome.runtime.sendMessage({ type, value, query }, (r) => {
        if (chrome.runtime.lastError) res({ ok: false, error: chrome.runtime.lastError.message });
        else res(r || { ok: false, error: "sin respuesta" });
      });
    } catch (e) { res({ ok: false, error: String(e) }); }
  });
}

function saveForm() { chrome.storage.local.set({ form, tab }); }

function bindFields() {
  FIELDS.forEach((f) => {
    const el = $(f);
    if (!el) return;
    if (el.type === "checkbox") el.checked = !!form[f];
    else if (form[f] !== undefined && form[f] !== null) el.value = form[f];
    const ev = (el.type === "checkbox" || el.tagName === "SELECT") ? "change" : "input";
    el.addEventListener(ev, () => { form[f] = el.type === "checkbox" ? el.checked : el.value; saveForm(); });
  });
}

function showTab(t) {
  tab = t;
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  document.querySelectorAll("main section").forEach((s) => s.classList.toggle("on", s.id === "t_" + t));
  saveForm();
  tick();
}

function setBar(id, done, total) {
  const bar = $(id);
  if (!bar) return;
  if (total > 0) {
    bar.classList.add("show");
    bar.firstElementChild.style.width = (Math.max(0, Math.min(1, done / total)) * 100) + "%";
  } else {
    bar.classList.remove("show");
  }
}

function setBadge(id, show, text) {
  const b = $(id);
  if (!b) return;
  b.textContent = text || "●";
  b.classList.toggle("show", !!show);
}

function setProgress(id, s, verb, done) {
  $(id).textContent = s.total
    ? (s.running ? verb : "terminado") + " · " + done + "/" + s.total + (s.current ? "\nahora: " + s.current : "")
    : "sin ejecutar";
}

function renderList(id, arr, fmt) {
  const el = $(id);
  if (!el) return;
  el.innerHTML = "";
  (arr || []).forEach((it) => {
    const [ok, text] = fmt(it);
    const li = document.createElement("li");
    const ic = document.createElement("span");
    ic.className = "ic " + (ok === true ? "ok" : ok === false ? "bad" : "warn");
    ic.textContent = ok === true ? "✓" : ok === false ? "✗" : "•";
    const sp = document.createElement("span");
    sp.textContent = text;
    li.appendChild(ic);
    li.appendChild(sp);
    el.appendChild(li);
  });
}

async function setLog(job, id, running) {
  const el = $(id);
  if (!el) return;
  const r = await send("logGet", undefined, { job, n: 120 });
  if (r && r.lines) {
    el.textContent = r.lines.length ? r.lines.join("\n") : "(sin datos)";
    el.scrollTop = el.scrollHeight;
  }
  const d = el.closest("details");
  if (d && running) d.open = true;
}

function fmtTime(ms) {
  if (!ms) return "";
  try { return new Date(ms).toLocaleTimeString(); } catch (e) { return ""; }
}

function fmtEta(ms) {
  if (!ms) return "—";
  const s = Math.round((ms - Date.now()) / 1000);
  if (s <= 0) return "ahora";
  const m = Math.floor(s / 60);
  return m ? m + " min " + (s % 60) + " s" : s + " s";
}

function renderHeader(h) {
  if (!h || (h.ok === false && h.error)) {
    $("conn").textContent = "sin conexión";
    $("dot").style.background = "var(--bad)";
    return;
  }
  const on = !!h.extension_online;
  $("dot").style.background = on ? "var(--ok)" : "var(--bad)";
  $("conn").textContent = on ? (h.enabled ? "en línea" : "en línea · pausada") : "sin conexión";
  const p = $("power");
  p.textContent = h.enabled ? "● ACTIVADA" : "● DESACTIVADA";
  p.classList.toggle("off", !h.enabled);
}

async function renderEstado() {
  const h = await send("test");
  const jobs = [];
  if (h.sched_alive) jobs.push("publicar");
  if (h.share_alive) jobs.push("grupos");
  if (h.share_post_alive) jobs.push("compartir post");
  if (h.comment_alive) jobs.push("comentar");
  if (h.groupscan_alive) jobs.push("escanear");
  $("status").textContent =
    "Conexión: " + (h.extension_online ? "OK" : "sin extensión") + " · scripts: " + (h.scripts_ok ? "OK" : "FALTAN") + "\n" +
    "Trabajos activos: " + (jobs.length ? jobs.join(", ") : "ninguno") +
    (h.enabled ? "" : "\n⚠ Interruptor en DESACTIVADA");
  const [a, b, c, d] = await Promise.all([send("schedGet"), send("shareGet"), send("sharePostGet"), send("commentGet")]);
  const lines = [];
  if (a && a.last) lines.push("Publicar: " + (a.last.ok ? "✓" : "✗") + " " + fmtTime(a.last.at) + (a.last.error ? " · " + a.last.error : ""));
  if (b && b.total) lines.push("Grupos: " + (b.published || 0) + "/" + b.total + (b.running ? " (en curso)" : ""));
  if (c && c.total) lines.push("Compartir post: " + (c.published || 0) + "/" + c.total + (c.running ? " (en curso)" : ""));
  if (d && d.total) lines.push("Comentar: " + (d.posted || 0) + "/" + d.total + (d.running ? " (en curso)" : ""));
  const e = await send("autopostState");
  if (e && e.ok !== false && (+e.pending || 0) > 0) lines.push("Automatización: " + e.pending + " pendientes" + (e.pausado ? " (en pausa)" : ""));
  $("acts").textContent = lines.length ? lines.join("\n") : "sin actividad";
}

async function renderPub() {
  const s = await send("schedGet");
  const total = +s.count || 0;
  const done = Math.max(0, total - (+s.remaining || 0));
  setBar("bar_pub", s.enabled ? done : total, total);
  let t = s.enabled ? "PUBLICANDO · quedan " + s.remaining + "/" + s.count : "detenido";
  if (s.alive === false) t += " · motor PARADO";
  if (s.enabled && s.next_at) t += " · próximo " + fmtTime(s.next_at);
  if (s.last && s.last.error) t += "\núltimo error: " + s.last.error;
  $("sstatus").textContent = t;
  setBadge("b_pub", !!s.enabled, s.enabled ? done + "/" + total : "");
  await setLog(JOB.pub, "log_pub", !!s.enabled);
}

async function renderSharePost() {
  const s = await send("sharePostGet");
  setBar("bar_sharepost", s.done || 0, s.total || 0);
  setProgress("pstatus", s, "compartiendo…", s.published || 0);
  renderList("presults", s.results, (r) => [r.ok, (r.group || "") + (r.stage && !r.ok ? "  (" + r.stage + ")" : "") + (r.dry ? "  [simulacro]" : "")]);
  setBadge("b_share", s.running || (s.total && (s.published || 0) < s.total), s.total ? (s.done || 0) + "/" + s.total : "");
  await setLog(JOB.sharepost, "log_sharepost", !!s.running);
}

async function renderShare() {
  const s = await send("shareGet");
  setBar("bar_share", s.done || 0, s.total || 0);
  setProgress("gstatus", s, "publicando…", s.published || 0);
  renderList("gresults", s.results, (r) => [r.ok, (r.name || "") + (r.stage && !r.ok ? "  (" + r.stage + ")" : "")]);
  await setLog(JOB.share, "log_share", !!s.running);
}

async function renderComment() {
  const s = await send("commentGet");
  setBar("bar_com", s.done || 0, s.total || 0);
  setProgress("cstatus", s, "comentando…", s.posted || 0);
  renderList("cresults", s.results, (r) => [r.ok, "#" + r.i + " " + (r.text || "") + (r.stage && !r.ok ? "  (" + r.stage + ")" : "")]);
  setBadge("b_com", !!s.running, s.total ? (s.done || 0) + "/" + s.total : "");
  await setLog(JOB.com, "log_com", !!s.running);
}

async function renderAuto() {
  const s = await send("autopostState");
  if (!s || s.ok === false) {
    $("astatus").textContent = "sin conexión con el daemon";
    setBadge("b_auto", false);
    return;
  }
  const on = !!(s.running || s.alive);
  const paused = !!s.pausado;
  const pending = +s.pending || 0;
  $("apause").textContent = paused ? "REANUDAR" : "PAUSAR";
  const lines = [
    "Estado: " + (!on ? "APAGADA (worker parado)" : paused ? "ACTIVADA · en pausa" : "ACTIVADA"),
    "Pendientes: " + pending,
    "Actual: " + (s.current || "—"),
    "Hechas: " + (+s.done || 0),
    "Próximo: " + (!on || paused || !s.next_at ? "—" : fmtEta(s.next_at))
  ];
  if (autoMsg) { lines.push(autoMsg); autoMsg = ""; }
  $("astatus").textContent = lines.join("\n");
  setBadge("b_auto", pending > 0, pending > 0 ? String(pending) : "");
  await setLog(JOB.auto, "log_auto", on && !paused);
}

async function renderGroups() {
  const g = await send("groupscanGet");
  $("grstatus").textContent = g.count ? (g.count + " grupos · " + (g.scanned_at || "")) : "sin escanear";
  setBadge("b_grp", !!g.alive, "…");
  await setLog(JOB.grp, "log_grp", !!g.alive);
}

async function tick() {
  if (busy) return;
  busy = true;
  try {
    const h = await send("test");
    renderHeader(h);
    if (tab === "estado") await renderEstado();
    else if (tab === "publicar") await renderPub();
    else if (tab === "compartir") { if (mode === "post") await renderSharePost(); else await renderShare(); }
    else if (tab === "comentar") await renderComment();
    else if (tab === "auto") await renderAuto();
    else if (tab === "ajustes") await renderGroups();
  } finally {
    busy = false;
  }
}

function needConfirm(dry) {
  return !dry && !confirm("Esto publicará de verdad en Facebook. ¿Continuar?");
}

async function doTest() {
  const h = await send("test");
  $("status").textContent = (h.extension_online ? "✓ conexión OK" : "✗ sin extensión") + "\n" + JSON.stringify(h).slice(0, 240);
  if (tab !== "estado") showTab("estado");
}

function wire() {
  document.querySelectorAll("nav button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

  document.querySelectorAll("#segshare button").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll("#segshare button").forEach((x) => x.classList.toggle("on", x === b));
    $("m_post").style.display = mode === "post" ? "" : "none";
    $("m_text").style.display = mode === "text" ? "" : "none";
    saveForm();
    tick();
  }));

  $("power").addEventListener("click", async () => {
    const h = await send("test");
    if (!h || h.ok === false || typeof h.enabled !== "boolean") { alert("Sin conexion con cxtd"); return; }
    const r = await send("setEnabled", !h.enabled);
    if (r && r.error) alert("Error: " + r.error);
    tick();
  });

  $("test").addEventListener("click", doTest);
  $("test2").addEventListener("click", doTest);

  $("save").addEventListener("click", async () => {
    form.server = $("server").value;
    form.token = $("token").value;
    await chrome.storage.local.set({ server: form.server, token: form.token, form });
    await send("start");
    tick();
  });

  async function stopAll() {
    if (!confirm("¿Detener publicar, compartir y comentar?")) return;
    await send("schedSet", { enabled: false });
    await send("shareSet", { stop: true });
    await send("sharePostSet", { stop: true });
    await send("commentSet", { stop: true });
    tick();
  }
  $("stopall").addEventListener("click", stopAll);
  $("stopall2").addEventListener("click", stopAll);

  $("sstart").addEventListener("click", async () => {
    await send("schedSet", { enabled: true, interval_min: +$("sint").value, count: +$("scount").value || 1, topic: $("stopic").value, text: $("stext").value });
    tick();
  });
  $("sstop").addEventListener("click", async () => { await send("schedSet", { enabled: false }); tick(); });

  $("pstart").addEventListener("click", async () => {
    const dry = $("pdry").checked;
    if (needConfirm(dry)) return;
    const value = { url: $("purl").value.trim(), expect: $("pexpect").value, filter: $("pfilter").value.trim(), limit: +$("plimit").value || 0, text: $("ptext").value, dry, delay: 8 };
    if (!value.filter && !value.ids) {
      alert("Indica los grupos (filtro o ids) antes de iniciar");
      return;
    }
    await send("sharePostSet", value);
    tick();
  });
  $("pstop").addEventListener("click", async () => { await send("sharePostSet", { stop: true }); tick(); });

  $("gstart").addEventListener("click", async () => {
    const dry = $("gdry").checked;
    if (needConfirm(dry)) return;
    await send("shareSet", { filter: $("gfilter").value, limit: +$("glimit").value || 0, text: $("gtext").value, dry, interval_sec: 8 });
    tick();
  });
  $("gstop").addEventListener("click", async () => { await send("shareSet", { stop: true }); tick(); });

  $("cstart").addEventListener("click", async () => {
    const dry = $("cdry").checked;
    if (needConfirm(dry)) return;
    const fixed = $("cmode").value === "fixed";
    await send("commentSet", { url: $("curl").value, text: fixed ? $("ctext").value : "", auto: !fixed, count: +$("ccount").value || 1, interval_min: +$("cint").value || 0, dry });
    tick();
  });
  $("cstop").addEventListener("click", async () => { await send("commentSet", { stop: true }); tick(); });

  $("apause").addEventListener("click", async () => {
    const s = await send("autopostState");
    if (!s || s.ok === false) { autoMsg = "✗ sin conexión"; tick(); return; }
    const value = !s.pausado;
    const r = await send("autopostSet", {action:"pause", value});
    autoMsg = r && (r.ok === false || r.error) ? "✗ " + (r.error || "no se pudo") : (value ? "✓ pausada" : "✓ reanudada");
    tick();
  });

  $("aclear").addEventListener("click", async () => {
    if (!confirm("¿Vaciar la cola de automatización?")) return;
    const r = await send("autopostSet", {action:"clear"});
    autoMsg = r && (r.ok === false || r.error) ? "✗ " + (r.error || "no se pudo") : "✓ cola vaciada";
    tick();
  });

  $("grscan").addEventListener("click", async () => { await send("groupscanSet", {}); tick(); });

  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
}

async function load() {
  const st = await chrome.storage.local.get(["form", "tab", "server", "token"]);
  form = st.form || {};
  if (st.server && form.server === undefined) form.server = st.server;
  if (st.token && form.token === undefined) form.token = st.token;
  if (st.tab) tab = st.tab;
  bindFields();
  document.querySelectorAll("nav button").forEach((b) => b.classList.toggle("on", b.dataset.tab === tab));
  document.querySelectorAll("main section").forEach((s) => s.classList.toggle("on", s.id === "t_" + tab));
  wire();
  tick();
  setInterval(tick, 1500);
}

load();
