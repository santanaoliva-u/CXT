const $ = (id) => document.getElementById(id);
let enabled = true;

async function load() {
  const c = await chrome.storage.local.get({ server: "http://127.0.0.1:8799", token: "", enabled: true });
  $("server").value = c.server;
  $("token").value = c.token;
  enabled = c.enabled;
  paint();
  render();
  renderSched();
  renderShare();
}

function paint() {
  const b = $("power");
  b.className = enabled ? "on" : "off";
  b.textContent = enabled ? "● ACTIVADA" : "● DESACTIVADA";
}

async function render() {
  const s = await chrome.runtime.sendMessage({ type: "status" }).catch(() => null);
  const last = (await chrome.storage.local.get({ last: null })).last;
  const dot = $("dot");
  dot.className = s && s.running ? "dot on-dot" : "dot";
  if (s) {
    enabled = s.enabled;
    paint();
  }
  $("status").textContent =
    (s ? `polling=${s.running} activo=${s.enabled}\n` : "sin respuesta del worker\n") +
    (last ? `último: ${last.op} ${last.ok ? "OK" : "ERR " + last.error} @ ${new Date(last.at).toLocaleTimeString()}` : "sin actividad");
}

$("power").addEventListener("click", async () => {
  const next = !enabled;
  const r = await chrome.runtime.sendMessage({ type: "setEnabled", value: next }).catch(() => null);
  enabled = r && typeof r.enabled === "boolean" ? r.enabled : next;
  paint();
  await render();
});

$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({
    server: $("server").value.trim() || "http://127.0.0.1:8799",
    token: $("token").value.trim(),
  });
  await chrome.runtime.sendMessage({ type: "start" }).catch(() => {});
  $("status").textContent = "guardado.";
  setTimeout(render, 300);
});

$("test").addEventListener("click", async () => {
  const r = await chrome.runtime.sendMessage({ type: "test" }).catch((e) => ({ ok: false, error: String(e) }));
  $("status").textContent = r && r.ok ? "conexión OK con cxtd" : "fallo: " + (r && (r.error || r.status));
});

async function renderSched() {
  const s = await chrome.runtime.sendMessage({ type: "schedGet" }).catch(() => null);
  const el = $("sstatus");
  if (!s || s.error) {
    el.textContent = s && s.error ? "error: " + s.error : "sin datos";
    return;
  }
  const alive = s.alive ? "motor OK" : "motor PARADO";
  let t = `${s.enabled ? "PUBLICANDO" : "detenido"} · quedan ${s.remaining != null ? s.remaining : "?"} · ${alive}`;
  if (s.enabled && s.next_at) t += `\npróxima: ${new Date(s.next_at).toLocaleTimeString()}`;
  if (s.last) t += `\núltima: ${s.last.ok ? "OK" : "ERR " + (s.last.error || "")} @ ${new Date(s.last.at).toLocaleTimeString()}`;
  el.textContent = t;
}

$("sstart").addEventListener("click", async () => {
  const interval = parseFloat($("sint").value) || 5;
  const count = Math.max(1, parseInt($("scount").value) || 1);
  const topic = $("stopic").value === "fixed" ? ($("stext").value.trim() || "auto") : "auto";
  await chrome.runtime.sendMessage({ type: "schedSet", value: { enabled: true, interval_min: interval, count, topic } }).catch(() => {});
  $("sstatus").textContent = "iniciando…";
  setTimeout(renderSched, 700);
});

$("sstop").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "schedSet", value: { enabled: false } }).catch(() => {});
  $("sstatus").textContent = "deteniendo…";
  setTimeout(renderSched, 700);
});

async function renderShare() {
  const s = await chrome.runtime.sendMessage({ type: "shareGet" }).catch(() => null);
  const el = $("gstatus");
  if (!s || s.error) {
    el.textContent = s && s.error ? "error: " + s.error : "sin datos";
    return;
  }
  if (!s.total) {
    el.textContent = "sin ejecuciones";
    return;
  }
  let t = `${s.running ? "COMPARTIENDO" : "terminado"} ${s.done || 0}/${s.total} · OK ${s.published || 0}`;
  if (s.dry) t += " · SIMULACIÓN";
  if (s.current) t += `\nactual: ${s.current}`;
  el.textContent = t;
}

$("gstart").addEventListener("click", async () => {
  const filter = $("gfilter").value.trim();
  const limit = Math.max(0, parseInt($("glimit").value) || 0);
  const text = $("gtext").value.trim();
  const dry = $("gdry").checked;
  if (!filter) { $("gstatus").textContent = "escribe un filtro de grupos"; return; }
  if (!text) { $("gstatus").textContent = "escribe el texto a compartir"; return; }
  await chrome.runtime.sendMessage({ type: "shareSet", value: { text, filter, limit, dry, interval_sec: 8 } }).catch(() => {});
  $("gstatus").textContent = "iniciando…";
  setTimeout(renderShare, 900);
});

$("gstop").addEventListener("click", async () => {
  await chrome.runtime.sendMessage({ type: "shareSet", value: { stop: true } }).catch(() => {});
  $("gstatus").textContent = "deteniendo…";
  setTimeout(renderShare, 900);
});

load();
setInterval(() => { render(); renderSched(); renderShare(); }, 2500);
