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

load();
setInterval(() => { render(); renderSched(); }, 2500);
