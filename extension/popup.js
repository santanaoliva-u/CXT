const $ = (id) => document.getElementById(id);
const FIELDS = ["server", "token", "manager", "mgrToken", "sint", "scount", "stopic", "stext",
  "purl", "pexpect", "pfilter", "plimit", "ptext", "pdry", "pfast", "pcomposer",
  "gfilter", "glimit", "gtext", "gdry",
  "curl", "cmode", "ctext", "cint", "ccount", "cdry",
  "surl", "s_main", "ssel", "sf_md", "sf_pdf", "sf_csv", "sf_txt"];
const JOB = { pub: "scheduler", sharepost: "sharepost", share: "share", com: "comment", grp: "groupscan", auto: "autopost" };

// Nav de dos filas: cada pestaña pertenece a un área (fila superior) y la fila
// inferior solo muestra las pestañas de esa área. `scraping` no tiene sub-pestaña.
const AREA_OF = { estado: "estado", publicar: "facebook", compartir: "facebook", comentar: "facebook", auto: "facebook", grupos: "facebook", scraping: "scraping", ajustes: "ajustes" };
const DEFAULT_TAB = { estado: "estado", facebook: "publicar", scraping: "scraping", ajustes: "ajustes" };

let tab = "estado";
let mode = "post";
let form = {};
let busy = false;
let pubLoading = false;
let pubWaitAt = 0;
let pubNowLoading = false;
let pubNowAt = 0;
let pubNowWaitAt = 0;
let autoMsg = "";
let scrapeBusy = false;

// Selectores de nav SIEMPRE con sufijo: nunca `nav button` a secas (la fila
// superior también son botones y no deben recibir el click de pestaña).
// Convencion de secciones: id = "t_" + tab (p. ej. "t_scraping", "t_grupos", "t_ajustes", "t_estado").
const navTabs = () => document.querySelectorAll("nav button[data-tab]");
const navAreas = () => document.querySelectorAll("nav button[data-area]");

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
    // Simulacion: opt-in por accion, NUNCA se restaura encendida desde el form
    // guardado — publicar de verdad es el default (evita el no-op silencioso).
    if (el.type === "checkbox") el.checked = f.endsWith("dry") ? false : (form[f] == null ? el.defaultChecked : !!form[f]);
    else if (form[f] !== undefined && form[f] !== null) el.value = form[f];
    const ev = (el.type === "checkbox" || el.tagName === "SELECT") ? "change" : "input";
    el.addEventListener(ev, () => { form[f] = el.type === "checkbox" ? el.checked : el.value; saveForm(); });
  });
}

function applyNav(t) {
  const area = AREA_OF[t] || "estado";
  navAreas().forEach((b) => b.classList.toggle("on", b.dataset.area === area));
  navTabs().forEach((b) => {
    b.classList.toggle("on", b.dataset.tab === t);
    b.hidden = AREA_OF[b.dataset.tab] !== area;
  });
  // En scraping no hay sub-pestañas: se oculta la fila inferior entera.
  const sr = document.querySelector("nav .subrow");
  if (sr) sr.hidden = (area === "scraping");
  document.querySelectorAll("main section").forEach((s) => s.classList.toggle("on", s.id === "t_" + t));
}

function showTab(t) {
  tab = t;
  applyNav(t);
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
  const d = el.closest("details");
  if (d && running) d.open = true;
  if (d && !d.open) return; // log cerrado: no gastes lecturas ni re-render (evita lag)
  const r = await send("logGet", undefined, { job, n: 120 });
  if (r && r.lines) {
    const txt = r.lines.length ? r.lines.join("\n") : "(sin datos)";
    if (el.textContent !== txt) { el.textContent = txt; el.scrollTop = el.scrollHeight; }
  }
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

// ── Perfil de Chrome · gestor profile_manager (127.0.0.1:8798) ─────────────
// El gestor es la fuente de verdad (perfil activo, puerto, ranking y última
// ejecución). Esta capa SOLO lee y, cuando el usuario pulsa un perfil, pide
// POST /select. Nunca cambia de perfil por su cuenta ni escribe estado.
const PM_REFRESH_MS = 15000; // el tick de 2s no martilla al gestor
const PM_TIMEOUT_MS = 2500;  // gestor caído = error rápido; el popup no se cuelga

const pm = {
  loading: false, again: false, at: 0, sig: "",
  state: "idle", health: null, error: "",
  profiles: [], current: "", select: "", msg: null, jobActive: false,
  lastAt: 0, lastFor: "", lastState: "idle", last: null, lastError: ""
};

// Base del gestor: campo `#manager`, o el host del servidor cxtd con puerto 8798.
function managerBase() {
  const explicit = (($("manager") && $("manager").value) || form.manager || "").trim();
  const raw = explicit || (($("server") && $("server").value) || form.server || "http://127.0.0.1:8799").trim();
  const withScheme = /^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : "http://" + raw;
  try {
    const u = new URL(withScheme);
    if (!explicit) { u.port = "8798"; u.pathname = ""; u.search = ""; u.hash = ""; }
    return u.origin;
  } catch (e) {
    return "http://127.0.0.1:8798";
  }
}

function managerServer() {
  const raw = (($("server") && $("server").value) || form.server || "http://127.0.0.1:8799").trim();
  return /^[a-z][a-z0-9+.-]*:\/\//i.test(raw) ? raw : "http://" + raw;
}

function serverPort() {
  try { return +new URL(managerServer()).port || 0; } catch (e) { return 0; }
}

// El token del gestor NO se hardcodea: campo en Ajustes (chrome.storage.local).
function managerToken() {
  return ((($("mgrToken") && $("mgrToken").value) || form.mgrToken || "") + "").trim();
}

async function mgrFetch(path, opts) {
  const o = opts || {};
  const ctl = new AbortController();
  const timeout = +o.timeout > 0 ? +o.timeout : PM_TIMEOUT_MS;
  const timer = setTimeout(() => ctl.abort(), timeout);
  const headers = { Accept: "application/json" };
  const tok = managerToken();
  if (tok) headers["X-CXT-Token"] = tok;
  if (o.body) headers["Content-Type"] = "application/json";
  try {
    const res = await fetch(managerBase() + path, {
      method: o.method || "GET", body: o.body, headers, cache: "no-store", signal: ctl.signal
    });
    let body = null;
    try { body = await res.json(); } catch (e) { body = null; } // cuerpo vacío/no JSON = sin cuerpo
    return { status: res.status, ok: res.ok, body };
  } catch (e) {
    const why = e && e.name === "AbortError" ? "tiempo de espera agotado" : String((e && e.message) || e);
    return { status: 0, ok: false, body: null, error: why };
  } finally {
    clearTimeout(timer);
  }
}

function pmSay(kind, text, action) {
  pm.msg = text ? { kind, text, action: action || null } : null;
}

function pmProfileName(dir) {
  const row = (pm.profiles || []).find((p) => p && p.profile_dir === dir);
  return row ? (row.name || row.profile_dir) : dir;
}

function pmGroupLabel(text) {
  const el = document.createElement("p");
  el.className = "pm-g";
  el.textContent = text;
  return el;
}

function pmRow(p) {
  const dir = String(p.profile_dir || "");
  const cur = pm.current ? dir === pm.current : p.group === "actual";
  const working = pm.select === dir;
  const b = document.createElement("button");
  b.type = "button";
  b.className = "pm-row" + (cur ? " cur" : "");
  b.dataset.dir = dir;
  if (cur) b.setAttribute("aria-current", "true");
  b.disabled = cur || !!pm.select || pm.jobActive;
  if (!cur) b.title = pm.jobActive ? "Hay un trabajo en curso; espera para cambiar de perfil."
    : (p.ready ? "Cambiar a " : "Iniciar el daemon de ") + (p.name || dir);

  const main = document.createElement("span");
  main.className = "pm-main";
  const name = document.createElement("span");
  name.className = "pm-name";
  name.textContent = p.name || dir;
  if (p.recommended) {
    const tag = document.createElement("span");
    tag.className = "pm-tag";
    tag.textContent = "Recomendado";
    name.appendChild(tag);
  }
  const sub = document.createElement("span");
  sub.className = "pm-sub";
  const bits = [];
  if (p.email) bits.push(String(p.email));
  bits.push(p.ready ? "listo" : (p.status || "no listo"));
  if (p.port) bits.push("puerto " + p.port);
  if (+p.use_count > 0) bits.push("usado " + (+p.use_count) + "×");
  if (!p.ready && p.reason) bits.push(String(p.reason));
  sub.textContent = bits.join(" · ");
  main.append(name, sub);

  const st = document.createElement("span");
  st.className = "pm-st";
  st.textContent = cur ? "Actual" : working ? "cambiando…" : pm.jobActive ? "en curso" : p.ready ? "cambiar" : "iniciar";
  b.append(main, st);
  b.addEventListener("click", () => { selectProfile(p); });
  return b;
}

function renderPmMsg() {
  const el = $("pm_msg");
  if (!el) return;
  const m = pm.msg;
  el.className = "pm-msg" + (m ? " " + m.kind : "");
  el.hidden = !m;
  el.textContent = "";
  if (!m) return;
  const t = document.createElement("span");
  t.className = "pm-msg-t";
  t.textContent = m.text;
  el.appendChild(t);
  if (m.action) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "picks-btn";
    b.textContent = m.action.label;
    b.addEventListener("click", () => { m.action.run(); });
    el.appendChild(b);
  }
}

function renderProfile() {
  const curEl = $("pm_current");
  const healthEl = $("pm_health");
  const listEl = $("pm_list");
  if (!curEl || !healthEl || !listEl) return;

  let healthText = "consultando gestor…";
  let healthCls = "";
  if (pm.state === "down") {
    healthText = "Gestor no disponible en " + managerBase() + ". El resto del popup sigue funcionando.";
    healthCls = "bad";
  } else if (pm.state === "auth") {
    healthText = "El gestor exige token: Ajustes → Token del gestor.";
    healthCls = "warn";
  } else if (pm.state === "error") {
    healthText = "El gestor respondió con un error" + (pm.error ? ": " + pm.error : "");
    healthCls = "bad";
  } else if (pm.state === "ok") {
    const port = pm.health && pm.health.active_port;
    healthText = "Gestor en línea" + (port ? " · daemon en el puerto " + port : " · sin perfil activo") +
      (pm.jobActive ? " · hay trabajo en curso" : "");
    healthCls = pm.jobActive ? "warn" : "ok";
  }

  const sig = JSON.stringify([
    pm.state, healthText, healthCls, pm.current, pm.jobActive, pm.select, pm.loading,
    (pm.profiles || []).map((p) => [p.profile_dir, p.name, p.group, p.recommended, p.ready, p.status, p.port, p.use_count, p.email]),
    pm.msg && [pm.msg.kind, pm.msg.text, pm.msg.action && pm.msg.action.label]
  ]);
  if (sig === pm.sig) return;
  pm.sig = sig;

  // Conserva el foco de teclado si estaba dentro de la lista (repintado idempotente).
  const focused = document.activeElement && document.activeElement.dataset && document.activeElement.dataset.dir &&
    document.activeElement.closest && document.activeElement.closest("#pm_list")
    ? document.activeElement.dataset.dir : "";

  healthEl.textContent = healthText;
  healthEl.className = "pm-health" + (healthCls ? " " + healthCls : "");
  const rf = $("pm_refresh");
  if (rf) rf.disabled = !!pm.loading;

  const who = pm.current ? pmProfileName(pm.current) : "";
  curEl.textContent = who || (pm.state === "ok" ? "sin perfil activo" : "—");
  curEl.title = pm.current || "";

  renderPmMsg();
  listEl.textContent = "";

  const rows = Array.isArray(pm.profiles) ? pm.profiles : [];
  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "pm-empty" + (pm.error && pm.state === "ok" ? " bad" : "");
    empty.textContent = pm.state === "down" ? "Sin datos: el gestor no responde."
      : pm.state === "auth" ? "Sin datos: falta el token del gestor."
      : pm.state === "error" ? "Sin datos: " + (pm.error || "error del gestor.")
      : (pm.error || "No se encontraron perfiles de Chrome.");
    listEl.appendChild(empty);
  } else if (rows.some((p) => p && p.group)) {
    [["actual", "Actual"], ["usados", "Usados"], ["otros", "Otros"]].forEach(([key, label]) => {
      const group = rows.filter((p) => p && p.group === key);
      if (!group.length) return;
      listEl.appendChild(pmGroupLabel(label));
      group.forEach((p) => listEl.appendChild(pmRow(p)));
    });
    // Un `group` desconocido (API futura) no debe desaparecer de la UI.
    const known = ["actual", "usados", "otros"];
    const rest = rows.filter((p) => p && known.indexOf(p.group) === -1);
    if (rest.length) {
      listEl.appendChild(pmGroupLabel("Perfiles"));
      rest.forEach((p) => listEl.appendChild(pmRow(p)));
    }
  } else {
    rows.forEach((p) => listEl.appendChild(pmRow(p)));
  }

  if (focused) {
    const sel = window.CSS && CSS.escape ? '[data-dir="' + CSS.escape(focused) + '"]' : '[data-dir="' + focused.replace(/"/g, '\\"') + '"]';
    const again = listEl.querySelector(sel);
    if (again && !again.disabled) again.focus();
  }
}

function fmtWhen(ms) {
  const n = +ms || 0;
  if (!n) return "";
  try {
    return new Date(n).toLocaleString("es", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  } catch (e) {
    return "";
  }
}

function pmMode(m) {
  if (m === "dry") return "simulación";
  if (m === "fast") return "rápida";
  if (m === "normal") return "normal";
  return m ? String(m) : "—";
}

function renderLastRun() {
  const el = $("pm_last");
  if (!el) return;
  const lines = [];
  if (pm.lastState === "loading") lines.push("cargando…");
  else if (pm.lastState === "none") lines.push("sin perfil seleccionado");
  else if (pm.lastState === "auth") lines.push("el gestor exige token (Ajustes → Token del gestor)");
  else if (pm.lastState === "down") lines.push("gestor no disponible" + (pm.lastError ? ": " + pm.lastError : ""));
  else if (pm.lastState === "error") lines.push("no se pudo leer: " + (pm.lastError || "error del gestor"));
  else if (pm.lastState === "empty") lines.push("este perfil todavía no tiene ejecuciones");
  else if (pm.lastState === "ok" && pm.last) {
    const lr = pm.last;
    lines.push((lr.ok ? "✓ " : "✗ ") + (lr.message || (lr.ok ? "Ejecución correcta." : "Ejecución con alertas.")));
    lines.push("compartidos: " + (+lr.shared || 0) + " · con alerta: " + (+lr.failed || 0) + " · en espera: " + (+lr.skipped || 0));
    const when = fmtWhen(lr.finished_at || lr.started_at);
    const who = lr.profile_name || (pm.current ? pmProfileName(pm.current) : "");
    lines.push("modo: " + pmMode(lr.mode) + (when ? " · " + when : "") + (who ? " · " + who : ""));
    const dur = lr.metrics && +lr.metrics.duration_ms;
    if (dur > 0) lines.push("duración: " + Math.round(dur / 1000) + " s");
  } else lines.push("sin datos");
  const txt = lines.join("\n");
  if (el.textContent !== txt) el.textContent = txt;
}

async function refreshLastRun(force) {
  const dir = pm.current || "";
  if (!dir) {
    pm.lastState = pm.state === "down" ? "down" : pm.state === "auth" ? "auth" : "none";
    pm.last = null; pm.lastFor = ""; pm.lastError = "";
    renderLastRun();
    return;
  }
  if (pm.state === "down" || pm.state === "auth" || pm.state === "error") {
    pm.lastState = pm.state; pm.last = null; pm.lastFor = dir; pm.lastError = pm.error;
    renderLastRun();
    return;
  }
  const now = Date.now();
  if (!force && pm.lastFor === dir && now - pm.lastAt < PM_REFRESH_MS) { renderLastRun(); return; }
  pm.lastAt = now; pm.lastFor = dir; pm.lastState = "loading"; pm.last = null; pm.lastError = "";
  renderLastRun();
  const r = await mgrFetch("/last_run?profile_dir=" + encodeURIComponent(dir), { timeout: 5000 });
  if (pm.lastFor !== dir) return; // respuesta tardía de otro perfil: se descarta
  if (r.status === 0) { pm.lastState = "down"; pm.lastError = r.error || ""; }
  else if (r.status === 401) { pm.lastState = "auth"; }
  else if (r.status === 400) { pm.lastState = "none"; }
  else if (!r.ok) { pm.lastState = "error"; pm.lastError = (r.body && r.body.error) || ("HTTP " + r.status); }
  else {
    const lr = r.body && r.body.last_run;
    if (!lr || typeof lr !== "object" || !Object.keys(lr).length) { pm.lastState = "empty"; pm.last = null; }
    else { pm.lastState = "ok"; pm.last = lr; }
  }
  renderLastRun();
}

async function refreshProfile(force, jobActive) {
  if (typeof jobActive === "boolean") pm.jobActive = jobActive;
  const now = Date.now();
  if (!force && now - pm.at < PM_REFRESH_MS) { renderProfile(); return; }
  if (pm.loading) { if (force) pm.again = true; return; }
  pm.loading = true;
  pm.at = now;
  pm.sig = "";
  renderProfile();
  try {
    const h = await mgrFetch("/health");
    if (h.status === 0) {
      pm.state = "down"; pm.health = null; pm.profiles = []; pm.current = ""; pm.error = h.error || "";
    } else if (h.status === 401) {
      pm.state = "auth"; pm.health = null; pm.profiles = []; pm.current = ""; pm.error = "";
    } else if (!h.ok || !h.body || h.body.ok === false) {
      pm.state = "error"; pm.health = null; pm.profiles = []; pm.current = "";
      pm.error = (h.body && h.body.error) || ("HTTP " + h.status);
    } else {
      pm.health = h.body;
      pm.current = h.body.active_profile_dir || pm.current || "";
      pm.state = "ok";
      pm.error = "";
      const activePort = +h.body.active_port || 0;
      const ownPort = serverPort();
      if (!pm.msg && activePort && ownPort && activePort !== ownPort) {
        pmSay("warn", "El perfil activo usa el puerto " + activePort + "; este popup apunta al " + ownPort + ".",
          { label: "Usar el puerto " + activePort, run: () => adoptPort(activePort) });
      }
      const q = pm.current ? "?current_profile_dir=" + encodeURIComponent(pm.current) : "";
      const r = await mgrFetch("/profiles" + q, { timeout: 6000 });
      if (r.status === 0) { pm.state = "down"; pm.profiles = []; pm.error = r.error || ""; }
      else if (r.status === 401) { pm.state = "auth"; pm.profiles = []; }
      else if (!r.ok || !r.body) {
        pm.state = "error"; pm.profiles = [];
        pm.error = (r.body && r.body.error) || ("HTTP " + r.status);
      } else {
        pm.profiles = (Array.isArray(r.body.profiles) ? r.body.profiles : [])
          .filter((p) => p && typeof p.profile_dir === "string");
        pm.error = r.body.error || "";
        if (!pm.current) {
          const cur = pm.profiles.find((p) => p.group === "actual");
          if (cur) pm.current = cur.profile_dir;
        }
      }
    }
  } finally {
    pm.loading = false;
  }
  pm.sig = "";
  renderProfile();
  if (pm.again) {
    pm.again = false;
    pm.at = 0;
    refreshProfile(true, pm.jobActive);
    return;
  }
  await refreshLastRun(force);
}

// Selección explícita: un clic = POST /select con {profile_dir}. Sin auto-switch.
async function selectProfile(p) {
  if (!p || pm.select) return;
  const dir = String(p.profile_dir || "");
  if (!dir || dir === pm.current || p.group === "actual") return;
  if (pm.jobActive) {
    pmSay("warn", "No se cambió de perfil: hay un trabajo en curso. Espera a que termine.");
    pm.sig = "";
    renderProfile();
    return;
  }
  pm.select = dir;
  pmSay(null, "");
  pm.sig = "";
  renderProfile();
  const r = await mgrFetch("/select", { method: "POST", body: JSON.stringify({ profile_dir: dir }), timeout: 25000 });
  pm.select = "";
  pm.sig = "";
  if (r.status === 0) {
    pmSay("bad", "No se pudo conectar con el gestor (" + managerBase() + "). " + (r.error || ""));
  } else if (r.status === 401) {
    pmSay("bad", "El gestor exige token. Añádelo en Ajustes → Token del gestor.");
  } else if (r.status === 404) {
    pmSay("bad", "Ese perfil ya no existe. Pulsa Actualizar para recargar la lista.");
  } else if (r.status === 409) {
    const code = r.body && r.body.code;
    const why = (r.body && r.body.error) || "el perfil no está disponible";
    pmSay(code === "busy" ? "warn" : "bad", "No se cambió de perfil: " + why);
  } else if (!r.ok) {
    pmSay("bad", "No se cambió de perfil: " + ((r.body && r.body.error) || ("HTTP " + r.status)));
  } else if (!r.body || r.body.ok !== true) {
    pmSay("bad", "No se cambió de perfil: respuesta inesperada del gestor.");
  } else {
    const name = r.body.profile_name || p.name || dir;
    const port = +r.body.port || 0;
    pm.current = dir;
    let text = "Perfil activo: " + name + (port ? " · puerto " + port : "") + ".";
    const srvPort = serverPort();
    let action = null;
    if (port && srvPort && port !== srvPort) {
      text += " Este popup apunta al puerto " + srvPort + ".";
      action = { label: "Usar el puerto " + port, run: () => adoptPort(port) };
    }
    pmSay("ok", text, action);
    await refreshProfile(true, pmJobActive(null));
    return;
  }
  renderProfile();
}

// Cambio explícito del servidor cxtd al puerto del perfil recién activado.
async function adoptPort(port) {
  const n = +port || 0;
  if (!n) return;
  let host = "127.0.0.1";
  try { host = new URL(managerServer()).hostname || host; } catch (e) { host = "127.0.0.1"; }
  const value = "http://" + host + ":" + n;
  form.server = value;
  if ($("server")) $("server").value = value;
  await chrome.storage.local.set({ server: value, form });
  await send("start");
  pmSay("ok", "Popup apuntando al puerto " + n + ".");
  pm.sig = "";
  renderProfile();
  await refreshProfile(true, pmJobActive(null));
}

// Un trabajo activo bloquea el cambio (el gestor también responde 409 busy).
function pmJobActive(h, auto) {
  if (h && h.ok !== false) {
    if (h.sched_enabled) return true;
    if (h.share_alive || h.share_post_alive || h.comment_alive || h.groupscan_alive) return true;
  }
  if (auto && auto.ok !== false) {
    if (auto.running || +auto.pending > 0) return true;
  }
  return false;
}

async function renderEstado(h0) {
  const h = h0 || await send("test");
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
  return { auto: e };
}

function pubHint(mode) {
  if (mode === "editorial") return "Investiga fuentes y solo habilita la publicación con sello verde.";
  if (mode === "ai") return "La IA redactará el texto.";
  if (mode === "fixed") return "Se publicará tal cual.";
  return "Se usará un titular del día.";
}

function normPubText(value) {
  return String(value || "").trim().replace(/\s+/g, " ");
}

async function renderPub() {
  const s = await send("schedGet");
  const sched = $("spub_sched");
  if (sched && s.enabled) sched.open = true;
  const total = +s.count || 0;
  const done = Math.max(0, total - (+s.remaining || 0));
  setBar("bar_pub", s.enabled ? done : total, total);
  const pv = s.preview;
  if (pubLoading && ((pv && +pv.at >= pubWaitAt) || Date.now() - pubWaitAt > 70000)) pubLoading = false;
  if (pubNowLoading && ((s.last && +s.last.at > pubNowAt) || Date.now() - pubNowWaitAt > 90000)) pubNowLoading = false;
  const mode = $("stopic").value;
  const text = $("stext").value;
  const box = $("spreview");
  if (pv && pv.text) {
    box.textContent = pv.text;
    box.classList.add("ok");
    box.classList.remove("bad");
  } else if (pv && pv.error) {
    box.textContent = "No se pudo generar: " + pv.error;
    box.classList.add("bad");
    box.classList.remove("ok");
  } else {
    box.textContent = "Sin vista previa. Genera una para revisar el texto antes de publicar.";
    box.classList.remove("ok", "bad");
  }
  $("stextcnt").textContent = text.length + " / 2000";
  $("spubhint").textContent = pubHint(mode);
  const meta = pv && pv.meta;
  const sm = $("spmeta");
  if (sm) {
    if (meta && meta.stamp_color) {
      const color = meta.stamp_color === "green" ? "verde"
        : meta.stamp_color === "amber" ? "ámbar"
        : meta.stamp_color === "red" ? "rojo" : meta.stamp_color;
      let line = "Sello " + color + " · " + (meta.publishable ? "publicable" : "no publicable");
      if (meta.attempt) line += " · intento " + meta.attempt;
      if (meta.run_id) line += " · run " + String(meta.run_id).slice(0, 10);
      sm.textContent = line;
      sm.className = "smeta " + (meta.stamp_color === "green" ? "ok" : meta.stamp_color === "amber" ? "warn" : "bad");
      sm.hidden = false;
    } else {
      sm.hidden = true;
      sm.textContent = "";
      sm.className = "smeta";
    }
  }
  const fresh = !!(pv && pv.text && pv.source && pv.source.topic === mode && normPubText(pv.source.text) === normPubText(text));
  const alreadyPublished = !!(pv && pv.meta && pv.meta.run_id && s.last && s.last.ok
    && String(s.last.run_id || "") === String(pv.meta.run_id));
  $("spregen").disabled = pubLoading || (mode !== "auto" && mode !== "editorial" && !text.trim());
  $("spnow").disabled = pubLoading || pubNowLoading || !fresh || alreadyPublished;
  let t;
  if (s.enabled) t = "PUBLICANDO · quedan " + s.remaining + "/" + s.count;
  else if (pv && pv.text) t = "Vista previa lista · sin programación activa";
  else t = "Sin programación activa";
  if (s.alive === false) t += " · motor PARADO";
  if (s.enabled && s.next_at) t += " · próximo " + fmtTime(s.next_at);
  if (s.last && s.last.error) t += "\núltimo intento: " + s.last.error;
  else if (s.last && s.last.text) t += "\núltimo texto: " + String(s.last.text).slice(0, 180);
  if (alreadyPublished) t += "\nesta vista previa ya se publicó";
  if (pubLoading) t = "Generando vista previa… puede tardar unos segundos.";
  else if (pubNowLoading) t = "Publicando ahora… la programación sigue activa.";
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
  renderList("gresults", s.results, (r) => [r.ok, (r.name || "") + (r.stage && !r.ok ? "  (" + r.stage + ")" : "") + (r.dry ? "  [simulacro]" : "")]);
  await setLog(JOB.share, "log_share", !!s.running);
}

async function renderComment() {
  const s = await send("commentGet");
  setBar("bar_com", s.done || 0, s.total || 0);
  setProgress("cstatus", s, "comentando…", s.posted || 0);
  renderList("cresults", s.results, (r) => [r.ok, "#" + r.i + " " + (r.text || "") + (r.stage && !r.ok ? "  (" + r.stage + ")" : "") + (r.dry ? "  [simulacro]" : "")]);
  setBadge("b_com", !!s.running, s.total ? (s.done || 0) + "/" + s.total : "");
  await setLog(JOB.com, "log_com", !!s.running);
}

let itemsSig = "", resSig = "";

// Identidad del post para cruzar items de la cola con results (mismo criterio
// que el worker: fbid si existe; si no, la ruta sin tracking). Solo lectura.
function autoPostId(u) {
  const s = String(u || "").split("#")[0];
  const m = /[?&]fbid=([^&]+)/i.exec(s);
  if (m) return "fbid:" + m[1];
  const p = /^[^?#]+/.exec(s);
  return (p ? p[0] : s).replace(/\/+$/, "").toLowerCase();
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
    "Hechas: " + (+s.done || 0) + (s.done_today ? " · hoy " + s.done_today : ""),
    "Próximo: " + (!on || paused || !s.next_at ? "—" : fmtEta(s.next_at)),
    "Ritmo: " + (+s.interval_min ? s.interval_min + " min" : "automático") + (s.interval_override ? " (forzado)" : "")
  ];
  if (autoMsg) { lines.push(autoMsg); autoMsg = ""; }
  $("astatus").textContent = lines.join("\n");

  const e = (s.error || "").trim();
  const ae = $("aerr");
  if (ae) { ae.textContent = e ? "⚠ " + e : ""; ae.style.display = e ? "" : "none"; }

  const sel = $("aintv");
  if (sel && document.activeElement !== sel) sel.value = s.interval_override ? String(s.interval_override) : "";

  const items = Array.isArray(s.items) ? s.items : [];
  // Items ya "hecho" registrados en results: un item pendiente cuyo post ya
  // figura hecho se omitira por duplicado; la fila debe decirlo.
  const doneIds = {};
  (Array.isArray(s.results) ? s.results : []).forEach((r) => {
    if (r && r.state === "hecho" && r.url) doneIds[autoPostId(r.url)] = r;
  });
  const sig = JSON.stringify(items.map((i) => [i.url, i.state, i.published, i.tries, i.error, i.dry, i.filter, doneIds[autoPostId(i.url)] ? 1 : 0]));
  const ulI = $("aitems");
  if (ulI && sig !== itemsSig) {
    itemsSig = sig;
    ulI.innerHTML = "";
    if (!items.length) ulI.innerHTML = '<li class="hint">(cola vacía)</li>';
    items.forEach((it, i) => {
      const stt = it.state || "pendiente";
      const li = document.createElement("li");
      const ic = document.createElement("span");
      ic.className = "ic " + (stt === "error" ? "bad" : stt === "hecho" ? "ok" : "warn");
      ic.textContent = stt === "hecho" ? "✓" : stt === "error" ? "✗" : "•";
      const sp = document.createElement("span");
      const who = it.filter ? "[" + it.filter + "] " : "";
      const nm = it.text ? String(it.text).slice(0, 36) : (it.url || "");
      // Estado real de la fila, siempre explicito y sin cifras inventadas:
      // hecho -> ya publicado; dry -> SIMULACION; pendiente con results hecho -> se omitira.
      let tail = "";
      if (stt === "hecho") {
        tail = it.dry ? " · SIMULACIÓN completada (no publicó)"
          : (+it.published > 0 ? " · ya publicado (" + (+it.published) + " grupos)" : " · ya publicado");
      } else if (stt !== "error" && doneIds[autoPostId(it.url)]) {
        tail = " · se omitirá: ya publicado (sin grupos nuevos)" + (it.dry ? " · SIMULACIÓN (no publica)" : "");
      } else if (it.dry) {
        tail = " · SIMULACIÓN (no publica)" + (stt === "error" && it.tries ? " · intento " + it.tries : "");
      } else {
        tail = stt === "error" ? " · error" + (it.tries ? " · intento " + it.tries : "") : "";
      }
      if (it.composer) tail += " · compositor (texto)";
      sp.textContent = who + nm + tail;
      sp.title = (it.error ? it.error + "\n" : "") + (it.url || "");
      li.append(ic, sp);
      if (stt !== "hecho") {
        const b1 = document.createElement("button");
        b1.className = "btn sec"; b1.textContent = "↻"; b1.title = "Reintentar este";
        b1.addEventListener("click", async () => { await send("autopostSet", { action: "retry", url: it.url }); autoMsg = "✓ reintento pedido"; itemsSig = ""; tick(); });
        const b2 = document.createElement("button");
        b2.className = "btn stop"; b2.textContent = "✕"; b2.title = "Quitar de la cola";
        b2.addEventListener("click", async () => { await send("autopostSet", { action: "del", url: it.url }); autoMsg = "✓ quitado de la cola"; itemsSig = ""; tick(); });
        const b3 = document.createElement("button");
        b3.className = "btn sec"; b3.textContent = "✎";
        b3.title = it.composer ? "Publicar con el compositor del grupo (activado): pulsa para volver a compartir" : "Publicar con el compositor del grupo (solo texto)";
        b3.addEventListener("click", async () => {
          const r = await send("autopostSet", { action: "composer", url: it.url, value: !it.composer });
          autoMsg = r && r.ok === false ? "✗ no se pudo cambiar el modo" : (it.composer ? "✓ vuelve a compartir" : "✓ publicará con el compositor");
          itemsSig = ""; tick();
        });
        li.append(b1, b2, b3);
      }
      ulI.appendChild(li);
    });
  }

  const res = Array.isArray(s.results) ? s.results.slice().reverse().slice(0, 12) : [];
  const rsig = JSON.stringify(res.map((r) => [r.url, r.ok, r.at, r.error, r.text]));
  const ulR = $("ares");
  if (ulR && rsig !== resSig) {
    resSig = rsig;
    ulR.innerHTML = "";
    if (!res.length) ulR.innerHTML = '<li class="hint">(sin resultados)</li>';
    res.forEach((r) => {
      const li = document.createElement("li");
      const ic = document.createElement("span");
      ic.className = "ic " + (r.ok ? "ok" : "bad");
      ic.textContent = r.ok ? "✓" : "✗";
      const sp = document.createElement("span");
      sp.textContent = (r.at ? fmtTime(r.at) + " · " : "") + (r.error || r.text || r.url || "");
      sp.title = r.error || r.url || "";
      li.append(ic, sp);
      ulR.appendChild(li);
    });
  }

  setBadge("b_auto", pending > 0, pending > 0 ? String(pending) : "");
  await setLog(JOB.auto, "log_auto", on && !paused);
}

async function renderGroups() {
  const g = await send("groupscanGet");
  const parts = [];
  if (g.count) parts.push(g.count + " grupos");
  if (g.count && g.scanned_at) parts.push(g.scanned_at);
  if (typeof g.labels_total === "number") parts.push(g.labels_total + " etiquetas");
  if (g.labels_generated_at && String(g.labels_generated_at).slice(0, 10) !== String(g.scanned_at || "")) {
    parts.push(String(g.labels_generated_at).slice(0, 10));
  }
  $("grstatus").textContent = g.alive
    ? "escaneando…"
    : (parts.length ? parts.join(" · ") : "sin escanear");
  setBadge("b_grp", !!g.alive, "…");
  await setLog(JOB.grp, "log_grp", !!g.alive);
}

// ── Scraping ────────────────────────────────────────────────────────────
// Nombre de fichero: slug del titulo + marca local YYYYMMDD-HHMM, compartido
// por TODA la exportacion (los cuatro formatos salen con el mismo nombre base).
function slugify(s) {
  const base = String(s == null ? "" : s)
    .normalize("NFD").replace(/[\u0300-\u036f]/g, "")
    .toLowerCase().replace(/[^a-z0-9]+/g, "-")
    .replace(/-+/g, "-").replace(/^-+|-+$/g, "")
    .slice(0, 60);
  return base || "pagina";
}

function stampLocal(d) {
  const p = (n) => String(n).padStart(2, "0");
  return "" + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + "-" + p(d.getHours()) + p(d.getMinutes());
}

// Misma serializacion que tables_to_csv de exporters.py: caption como fila de
// una celda, CRLF y comillas dobladas si el valor trae , " CR o LF.
function csvCell(v) {
  const s = v == null ? "" : String(v);
  return /[",\r\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function tablesToCsv(tables) {
  const out = [];
  (Array.isArray(tables) ? tables : []).forEach((t, i) => {
    if (i > 0) out.push("");
    if (t && t.caption) out.push(csvCell(t.caption));
    if (t && Array.isArray(t.headers) && t.headers.length) out.push(t.headers.map(csvCell).join(","));
    const rows = t && Array.isArray(t.rows) ? t.rows : [];
    rows.forEach((row) => out.push((Array.isArray(row) ? row : []).map(csvCell).join(",")));
  });
  return out.length ? out.join("\r\n") + "\r\n" : "";
}

function downloadBlob(name, blob) {
  const u = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = u;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(u), 1000);
}

// Pestaña destino: la activa de esta ventana; `#surl` navega antes de extraer.
async function scrapeTarget() {
  const tr = await send("runOp", { op: "tabs", args: {} });
  if (!tr || tr.ok !== true) return { error: (tr && tr.error) || "tabs" };
  const win = await chrome.windows.getCurrent();
  const list = Array.isArray(tr.data) ? tr.data : ((tr.data && tr.data.tabs) || []);
  const t = list.find((x) => x.active && x.windowId === win.id) || list.find((x) => x.active);
  if (!t) return { error: "sin pestaña activa" };
  const url = $("surl").value.trim();
  if (url && url !== t.url) {
    const nr = await send("runOp", { op: "navigate", args: { tabId: t.id, url } });
    if (!nr || nr.ok !== true) return { error: (nr && nr.error) || "navigate" };
  }
  return { tabId: t.id, title: t.title || "" };
}

// Salida unica: ni un throw ni un return temprano pueden dejar botones colgados.
function endScrape() {
  scrapeBusy = false;
  $("s_run").disabled = false;
  $("s_exp").disabled = false;
}

async function extraer() {
  if (scrapeBusy) return;
  scrapeBusy = true;
  $("s_run").disabled = true;
  $("s_exp").disabled = true;
  try {
    const tg = await scrapeTarget();
    if (tg.error) { await renderScraping([[false, tg.error]]); return; }
    setBar("bar_scraping", 1, 2);
    const args = { tabId: tg.tabId, main: $("s_main").checked };
    if ($("ssel").value.trim()) args.selector = $("ssel").value.trim();
    const r = await send("runOp", { op: "scrape.page", args });
    setBar("bar_scraping", r && r.ok ? 2 : 0, 2);
    await renderScraping(r && r.ok === false ? [[false, r.error]] : []);
  } finally {
    endScrape();
  }
}

async function exportar() {
  if (scrapeBusy) return;
  scrapeBusy = true;
  $("s_run").disabled = true;
  $("s_exp").disabled = true;
  const extra = [];
  const salidas = [];
  let titulo = "";
  try {
    const tg = await scrapeTarget();
    if (tg.error) { await renderScraping([[false, tg.error]]); return; }
    const args = { tabId: tg.tabId, main: $("s_main").checked };
    if ($("ssel").value.trim()) args.selector = $("ssel").value.trim();
    const total = ["sf_md", "sf_txt", "sf_csv", "sf_pdf"].filter((id) => $(id).checked).length;
    if (!total) { await renderScraping([[null, "marca al menos un formato"]]); return; }
    let paso = 0;
    const marca = () => { paso += 1; setBar("bar_scraping", paso, total); };

    if ($("sf_md").checked) {
      const r = await send("runOp", { op: "scrape.markdown", args });
      marca();
      if (!r || r.ok !== true) extra.push([false, "md: " + ((r && r.error) || "error")]);
      else if (!r.data || typeof r.data.markdown !== "string") extra.push([false, "md: sin datos"]);
      else {
        if (!titulo) titulo = r.data.title || "";
        salidas.push({ ext: "md", blob: new Blob([r.data.markdown], { type: "text/markdown;charset=utf-8" }) });
      }
    }
    if ($("sf_txt").checked) {
      const r = await send("runOp", { op: "scrape.page", args });
      marca();
      if (!r || r.ok !== true) extra.push([false, "txt: " + ((r && r.error) || "error")]);
      else if (!r.data || typeof r.data.text !== "string") extra.push([false, "txt: sin datos"]);
      else {
        if (!titulo) titulo = r.data.title || "";
        salidas.push({ ext: "txt", blob: new Blob([r.data.text], { type: "text/plain;charset=utf-8" }) });
      }
    }
    if ($("sf_csv").checked) {
      const r = await send("runOp", { op: "scrape.tables", args });
      marca();
      if (!r || r.ok !== true) extra.push([false, "csv: " + ((r && r.error) || "error")]);
      else if (!r.data) extra.push([false, "csv: sin datos"]);
      else {
        if (!titulo) titulo = r.data.title || "";
        salidas.push({ ext: "csv", blob: new Blob([tablesToCsv(r.data.tables)], { type: "text/csv;charset=utf-8" }) });
      }
    }
    if ($("sf_pdf").checked) {
      const r = await send("runOp", { op: "cdp", args: { tabId: tg.tabId, method: "Page.printToPDF", params: { printBackground: true } } });
      marca();
      if (!r || r.ok !== true) {
        const err = (r && r.error) || "error";
        extra.push([false, "pdf: " + (err === "PDF_TOO_BIG" ? "PDF_TOO_BIG (demasiado grande)" : err)]);
      } else {
        const b64 = r.data && r.data.result && r.data.result.data;
        if (!b64) extra.push([false, "pdf: sin datos"]);
        else {
          try {
            const bin = atob(b64);
            const bytes = new Uint8Array(bin.length);
            for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
            salidas.push({ ext: "pdf", blob: new Blob([bytes], { type: "application/pdf" }) });
          } catch (e) {
            extra.push([false, "pdf: " + String(e)]);
          }
        }
      }
    }

    if (!salidas.length) {
      setBar("bar_scraping", 0, 0);
      await renderScraping(extra);
      return;
    }
    const base = slugify(titulo || tg.title || "pagina") + "-" + stampLocal(new Date());
    const nombres = [];
    salidas.forEach((f) => {
      const n = base + "." + f.ext;
      nombres.push(n);
      downloadBlob(n, f.blob);
    });
    setBar("bar_scraping", total, total);
    extra.push([true, "descargados: " + nombres.join(", ")]);
    await renderScraping(extra);
  } finally {
    endScrape();
  }
}

async function renderScraping(extra) {
  const st = await chrome.storage.local.get(["scrapingLog", "scrapingResult"]);
  const log = Array.isArray(st.scrapingLog) ? st.scrapingLog : [];
  const el = $("log_scraping");
  el.textContent = log.length ? log.join("\n") : "(sin datos)";
  el.scrollTop = el.scrollHeight;
  const r = st.scrapingResult;
  setBadge("b_scraping", !!r, r && r.ok === false ? "!" : "");
  const lineas = [];
  if (!r) lineas.push([true, "(sin extraer)"]);
  else if (r.ok === false) lineas.push([false, r.op + " · " + r.error]);
  else if (r.op === "cdp") lineas.push([true, "pdf listo · " + (r.bytes || 0) + " bytes"]);
  else {
    const d = r.data || {};
    lineas.push([true, r.op + " · " + (d.url || "")]);
    lineas.push([true, d.title || "(sin título)"]);
    if (r.op === "scrape.page" || r.op === "scrape.markdown") lineas.push([true, "texto: " + (d.chars || 0) + " chars"]);
    else if (r.op === "scrape.tables") lineas.push([true, "tablas: " + ((d.tables || []).length) + (d.truncated ? " (truncado)" : "")]);
    else if (r.op === "scrape.links") lineas.push([true, "enlaces: " + ((d.links || []).length) + (d.truncated ? " (truncado)" : "")]);
    if (r.op === "scrape.page" && d.frames > 0) lineas.push([true, "frames: " + d.frames + " (no accesibles)"]);
  }
  (extra || []).forEach((x) => lineas.push(x));
  renderList("scraping_res", lineas, (x) => x);
}

let picksLabels = null; // null = daemon sin `labels` · [] = sin etiquetas · array = catalogo
let picksNorm = {};     // label normalizada -> canonica
let picksLast;
let picksFetchedAt = 0; // ms del ultimo groupscanGet real (throttle del reintento)
let picksHas = false;   // ese fetch dejo etiquetas utilizables
let picksBuiltSig = "";
let picksSelSig = "";
let picksQuery = "";
let picksExpanded = false;

function picksNorm1(s) {
  return String(s || "").trim().toLowerCase().normalize("NFD").replace(/[\u0300-\u036f]/g, "");
}

function picksField() { return mode === "post" ? $("pfilter") : $("gfilter"); }

function picksTokens() {
  const el = picksField();
  return el ? el.value.split(",").map((t) => t.trim()).filter(Boolean) : [];
}

function picksIsLabel(tok) { return Object.prototype.hasOwnProperty.call(picksNorm, picksNorm1(tok)); }

function picksSelection() {
  const sel = {};
  picksTokens().forEach((t) => { if (picksIsLabel(t)) sel[picksNorm1(t)] = true; });
  return sel;
}

function picksWrite(sel) {
  const free = picksTokens().filter((t) => !picksIsLabel(t));
  const kept = (picksLabels || []).filter((l) => sel[picksNorm1(l.label)]).map((l) => l.label);
  const out = free.concat(kept).join(", ");
  const el = picksField();
  if (el) el.value = out;
  form[mode === "post" ? "pfilter" : "gfilter"] = out; // `.value` programatico no dispara `input`: sincroniza a mano
  saveForm();
}

async function renderPicks(g) {
  const wrap = $("picks");
  if (!wrap) return;
  // Refresco sin cache negativo: con etiquetas se re-pide en cada invocacion (el tick
  // de 2s las mantiene frescas); si el ultimo fetch no dejo etiquetas se reintenta
  // como maximo 1 vez cada 5s — nunca queda en vacio permanente ni martilla al daemon.
  let fetched = false;
  if (g === undefined) {
    if (picksHas || Date.now() - picksFetchedAt >= 5000) {
      g = await send("groupscanGet");
      picksFetchedAt = Date.now();
      fetched = true;
    } else {
      g = picksLast;
    }
  }
  picksLast = g;
  const has = !!(g && typeof g === "object" && "labels" in g && Array.isArray(g.labels));
  picksLabels = has ? g.labels.filter((l) => l && typeof l.label === "string" && l.label.trim()) : null;
  picksNorm = {};
  if (picksLabels) {
    const seen = {};
    picksLabels = picksLabels.filter((l) => {
      const n = picksNorm1(l.label);
      if (seen[n]) return false;
      seen[n] = true;
      picksNorm[n] = l.label;
      return true;
    });
  }
  if (fetched) picksHas = !!(picksLabels && picksLabels.length);

  const meta = $("picksmeta");
  const total = has && typeof g.labels_total === "number" ? g.labels_total : (picksLabels ? picksLabels.length : 0);
  const at = has && g.labels_generated_at ? String(g.labels_generated_at).slice(0, 10) : "";
  if (meta) {
    if (!has) meta.textContent = "sin etiquetas";
    else if (!picksLabels.length) meta.textContent = "sin etiquetas — escanea grupos";
    else meta.textContent = total + (total === 1 ? " etiqueta" : " etiquetas") + (at ? " · " + at : "");
  }
  wrap.classList.toggle("empty", !(picksLabels && picksLabels.length));

  const all = $("picksall");
  if (all) {
    all.hidden = !(picksLabels && picksLabels.length > 12);
    all.textContent = picksExpanded ? "ver menos" : "ver todas";
  }
  wrap.classList.toggle("expanded", picksExpanded);

  const q = picksNorm1(picksQuery);
  const vis = (picksLabels || []).filter((l) => !q || picksNorm1(l.label).indexOf(q) !== -1);
  const shown = picksExpanded ? vis : vis.slice(0, 12);
  const sig = JSON.stringify([picksQuery, picksExpanded, shown.map((l) => [l.label, +l.count || 0])]);
  const row = $("picksrow");
  if (row && sig !== picksBuiltSig) {
    picksBuiltSig = sig;
    picksSelSig = "";
    row.innerHTML = "";
    if (!shown.length && picksLabels && picksLabels.length) {
      const none = document.createElement("span");
      none.className = "picks-none";
      none.textContent = "sin coincidencias";
      row.appendChild(none);
    }
    shown.forEach((l) => {
      const b = document.createElement("button");
      b.type = "button";
      b.className = "chip";
      b.dataset.label = l.label;
      b.title = l.label;
      const sp = document.createElement("span");
      sp.className = "chip-l";
      sp.textContent = l.label;
      const c = document.createElement("b");
      c.className = "chip-c";
      c.textContent = String(+l.count || 0);
      b.append(sp, c);
      b.addEventListener("click", () => {
        const sel = picksSelection();
        const n = picksNorm1(l.label);
        if (sel[n]) delete sel[n]; else sel[n] = true;
        picksWrite(sel);
        renderPicks(picksLast);
      });
      row.appendChild(b);
    });
  }

  const field = picksField();
  const selSig = mode + "\u0000" + (field ? field.value : "");
  if (selSig !== picksSelSig) {
    picksSelSig = selSig;
    const sel = picksSelection();
    wrap.querySelectorAll(".chip").forEach((c) => c.classList.toggle("on", !!sel[picksNorm1(c.dataset.label)]));
  }
  wrap.classList.toggle("has-sel", Object.keys(picksSelection()).length > 0);
}

async function tick() {
  if (busy) return;
  busy = true;
  try {
    const h = await send("test");
    renderHeader(h);
    let autoState = null;
    if (tab === "estado") { const st = await renderEstado(h); autoState = st && st.auto; }
    else if (tab === "publicar") await renderPub();
    else if (tab === "compartir") { if (mode === "post") await renderSharePost(); else await renderShare(); await renderPicks(); }
    else if (tab === "comentar") await renderComment();
    else if (tab === "auto") await renderAuto();
    else if (tab === "grupos") await renderGroups();
    else if (tab === "scraping") await renderScraping();
    if (tab === "estado") await refreshProfile(false, pmJobActive(h, autoState));
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
  navTabs().forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  navAreas().forEach((b) => b.addEventListener("click", () => {
    const a = b.dataset.area;
    showTab(AREA_OF[tab] === a ? tab : DEFAULT_TAB[a]);
  }));

  document.querySelectorAll("#segshare button").forEach((b) => b.addEventListener("click", () => {
    mode = b.dataset.mode;
    document.querySelectorAll("#segshare button").forEach((x) => x.classList.toggle("on", x === b));
    $("m_post").style.display = mode === "post" ? "" : "none";
    $("m_text").style.display = mode === "text" ? "" : "none";
    renderPicks(picksLast);
    saveForm();
    tick();
  }));

  const pq = $("picksq");
  if (pq) pq.addEventListener("input", () => { picksQuery = pq.value; renderPicks(picksLast); });
  const pa = $("picksall");
  if (pa) pa.addEventListener("click", () => { picksExpanded = !picksExpanded; renderPicks(picksLast); });
  const pc = $("picksclear");
  if (pc) pc.addEventListener("click", () => { picksWrite({}); renderPicks(picksLast); });
  ["pfilter", "gfilter"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("input", () => renderPicks(picksLast));
  });

  $("power").addEventListener("click", async () => {
    const h = await send("test");
    if (!h || h.ok === false || typeof h.enabled !== "boolean") { alert("Sin conexion con cxtd"); return; }
    const r = await send("setEnabled", !h.enabled);
    if (r && r.error) alert("Error: " + r.error);
    tick();
  });

  $("test").addEventListener("click", doTest);
  $("test2").addEventListener("click", doTest);

  const pmr = $("pm_refresh");
  if (pmr) pmr.addEventListener("click", async () => {
    pmSay(null, "");
    pm.sig = "";
    await refreshProfile(true, pm.jobActive);
  });

  $("save").addEventListener("click", async () => {
    form.server = $("server").value;
    form.token = $("token").value;
    form.manager = $("manager") ? $("manager").value : "";
    form.mgrToken = $("mgrToken") ? $("mgrToken").value : "";
    await chrome.storage.local.set({
      server: form.server, token: form.token,
      manager: form.manager, mgrToken: form.mgrToken, form
    });
    await send("start");
    await refreshProfile(true, pm.jobActive);
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

  $("stext").addEventListener("input", () => {
    $("stextcnt").textContent = $("stext").value.length + " / 2000";
    // Si el usuario escribe una instrucción, el modo "auto" la ignoraría:
    // pasar a editorial evita generar un titular y perder el texto escrito.
    if ($("stext").value.trim() && $("stopic").value === "auto") {
      $("stopic").value = "editorial";
      $("spubhint").textContent = pubHint("editorial");
    }
    $("spnow").disabled = true;
  });
  $("stopic").addEventListener("change", () => {
    $("spubhint").textContent = pubHint($("stopic").value);
    $("spnow").disabled = true;
    tick();
  });
  $("spregen").addEventListener("click", async () => {
    let topic = $("stopic").value;
    const text = $("stext").value.trim();
    if (topic === "auto" && text) {
      topic = "editorial";
      $("stopic").value = "editorial";
      $("spubhint").textContent = pubHint("editorial");
    }
    if (topic !== "auto" && topic !== "editorial" && !text) { alert("Escribe el texto o la instrucción primero."); return; }
    pubLoading = true;
    pubWaitAt = Date.now();
    tick();
    const r = await send("schedSet", { action: "preview", topic, text });
    if (r && r.ok === false) { pubLoading = false; alert("No se pudo generar: " + (r.error || "error")); }
    tick();
  });
  $("spnow").addEventListener("click", async () => {
    const s = await send("schedGet");
    const pv = s && s.preview;
    if (!pv || !pv.text) { alert("Genera la vista previa primero."); return; }
    const mode = $("stopic").value;
    const text = $("stext").value.trim();
    if (!pv.source || pv.source.topic !== mode || normPubText(pv.source.text) !== normPubText(text)) {
      alert("La vista previa no coincide con el modo/texto actual. Genera una nueva.");
      return;
    }
    if (s.last && s.last.ok && pv.meta && pv.meta.run_id
      && String(s.last.run_id || "") === String(pv.meta.run_id)) {
      alert("Esta vista previa ya se publicó.");
      return;
    }
    if (!confirm("Esto publicará AHORA en Facebook y mantendrá la programación activa:\n\n" + String(pv.text).slice(0, 350) + "\n\n¿Continuar?")) return;
    const beforeAt = s.last && +s.last.at || 0;
    const r = await send("schedSet", { action: "publish_now", topic: mode, text: pv.text, meta: pv.meta || {} });
    if (r && r.ok === false) { alert("No se pudo publicar: " + (r.error || "error")); return; }
    pubNowAt = beforeAt;
    pubNowWaitAt = Date.now();
    pubNowLoading = true;
    pubLoading = false;
    $("sstatus").textContent = "Publicando ahora… la programación sigue activa.";
    tick();
  });
  $("sstart").addEventListener("click", async () => {
    pubLoading = false;
    let topic = $("stopic").value;
    const text = $("stext").value;
    if (topic === "auto" && text.trim()) {
      topic = "editorial";
      $("stopic").value = "editorial";
      $("spubhint").textContent = pubHint("editorial");
    }
    await send("schedSet", { enabled: true, interval_min: +$("sint").value, count: +$("scount").value || 1, topic, text });
    tick();
  });
  $("sstop").addEventListener("click", async () => { await send("schedSet", { enabled: false }); tick(); });
  $("sdesk").addEventListener("click", () => {
    const text = $("stext").value.trim();
    const url = chrome.runtime.getURL("editorial.html") + (text ? "?instruction=" + encodeURIComponent(text) : "");
    if (chrome.tabs && chrome.tabs.create) chrome.tabs.create({ url });
    else window.open(url, "_blank", "noopener");
  });

  $("pstart").addEventListener("click", async () => {
    const dry = $("pdry").checked;
    if (needConfirm(dry)) return;
    const value = { url: $("purl").value.trim(), expect: $("pexpect").value, filter: $("pfilter").value.trim(), limit: +$("plimit").value || 0, text: $("ptext").value, dry, fast: $("pfast").checked, delay: 8,
      composer: $("pcomposer").checked };
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
    itemsSig = "";
    tick();
  });

  const arety = $("arety");
  if (arety) arety.addEventListener("click", async () => {
    const s = await send("autopostState");
    const errored = (s && Array.isArray(s.items) ? s.items : []).filter((it) => it.state === "error" && !it.dry);
    if (!errored.length) { autoMsg = "• no hay fallidos que reintentar"; tick(); return; }
    if (!confirm("¿Reintentar " + errored.length + " publicación(es) fallida(s)?")) return;
    for (const it of errored) await send("autopostSet", { action: "retry", url: it.url });
    autoMsg = "✓ " + errored.length + " reintento(s) pedido(s)";
    itemsSig = "";
    tick();
  });

  $("anext").addEventListener("click", async () => {
    if (!confirm("¿Forzar el envío del siguiente ahora (ignora el intervalo de espera)?")) return;
    const r = await send("autopostSet", {action:"next"});
    autoMsg = r && (r.ok === false || r.error) ? "✗ " + (r.error || "no se pudo") : "✓ siguiente ya";
    tick();
  });

  $("aintv").addEventListener("change", async () => {
    const v = +$("aintv").value || 0;
    const r = await send("autopostSet", {action:"interval", value:v});
    autoMsg = r && (r.ok === false || r.error) ? "✗ " + (r.error || "no se pudo") : (v ? "✓ ritmo " + v + " min" : "✓ ritmo automático");
    tick();
  });

  $("alog").addEventListener("click", async () => {
    if (!confirm("¿Borrar el log de automatización?")) return;
    const r = await send("logClear", {job:"autopost", clear:true});
    autoMsg = r && (r.ok === false || r.error) ? "✗ " + (r.error || "no se pudo") : "✓ log borrado";
    tick();
  });

  const lc = $("logclear");
  if (lc) lc.addEventListener("click", async () => {
    if (!confirm("¿Borrar TODOS los logs?")) return;
    const r = await send("logClear", {all:true, clear:true});
    autoMsg = r && (r.ok === false || r.error) ? "✗ " + (r.error || "no se pudo") : "✓ logs borrados";
    tick();
  });

  $("grscan").addEventListener("click", async () => {
    // Se muestra el resultado del POST: antes se descartaba y un fallo
    // ("ya hay un escaneo en curso", sin conexion…) parecia un boton muerto.
    const r = await send("groupscanSet", {});
    if (!r || r.ok === false || r.error) {
      $("grstatus").textContent = "✗ " + ((r && (r.error || r.msg)) || "no se pudo escanear");
      return;
    }
    $("grstatus").textContent = "escaneando…";
    tick();
  });

  $("s_run").addEventListener("click", extraer);
  $("s_exp").addEventListener("click", exportar);

  document.addEventListener("visibilitychange", () => { if (!document.hidden) tick(); });
}

async function load() {
  const st = await chrome.storage.local.get(["form", "tab", "server", "token", "manager", "mgrToken", "editorial_default_v1"]);
  form = st.form || {};
  if (st.server && form.server === undefined) form.server = st.server;
  if (st.token && form.token === undefined) form.token = st.token;
  if (st.manager && form.manager === undefined) form.manager = st.manager;
  if (st.mgrToken && form.mgrToken === undefined) form.mgrToken = st.mgrToken;
  if (st.tab) tab = st.tab;
  if (!AREA_OF[tab]) tab = "estado";
  bindFields();
  // Un texto guardado junto con "auto" es un estado engañoso: auto ignora el
  // texto. Al cargar, si hay instrucción, el modo correcto es editorial.
  if ($("stext").value.trim() && $("stopic").value === "auto") {
    $("stopic").value = "editorial";
    form.stopic = "editorial";
    saveForm();
  }
  if (!st.editorial_default_v1) {
    if ($("stopic").value === "ai") {
      $("stopic").value = "editorial";
      form.stopic = "editorial";
      saveForm();
    }
    await chrome.storage.local.set({ editorial_default_v1: true });
  }
  $("spubhint").textContent = pubHint($("stopic").value);
  applyNav(tab);
  wire();
  tick();
  renderPicks();
  setInterval(tick, 2000);
  refreshProfile(true, false);
}

load();
