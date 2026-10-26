// CXT background service worker (MV3). Classic script, no imports.
const DEFAULTS = { server: "http://127.0.0.1:8799", token: "", enabled: true, maxHops: 0 };
let running = false;
let runningSince = 0;
let lastStore = 0;
let pollCtl = null;
let currentLoop = null;
let autoStatsCache = null;
let runOpBusy = false;

const processedIds = new Set();
const PROCESSED_MAX = 100;
function markProcessed(id) {
  if (id == null) return;
  processedIds.add(id);
  while (processedIds.size > PROCESSED_MAX) {
    processedIds.delete(processedIds.values().next().value);
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function cfg() {
  return await chrome.storage.local.get(DEFAULTS);
}

function qs(server, token) {
  return server + "/next" + (token ? "?token=" + encodeURIComponent(token) : "");
}

async function post(server, path, token, obj) {
  const url = server + path + (token ? "?token=" + encodeURIComponent(token) : "");
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(obj),
  });
  return res.ok;
}

async function daemonJson(server, path, token, body) {
  const url = server + path + (token ? "?token=" + encodeURIComponent(token) : "");
  const opts =
    body === undefined
      ? { cache: "no-store" }
      : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const res = await withTimeout(fetch(url, opts), 15000, path);
  return await res.json();
}

let dlgHooked = false;
function hookDialogs() {
  if (dlgHooked) return;
  dlgHooked = true;
  chrome.debugger.onEvent.addListener((src, method) => {
    if (method === "Page.javascriptDialogOpening") {
      chrome.debugger
        .sendCommand({ tabId: src.tabId }, "Page.handleJavaScriptDialog", { accept: true })
        .catch(() => {});
    }
  });
}

const pageEnabled = new Set();

const OP_TIMEOUT_MS = 25000;
const KMAP = { Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Home: 36, End: 35, PageUp: 33, PageDown: 34, " ": 32 };

function opTimeout(op, args) {
  const explicit = Number((args && args.timeoutMs) || 0);
  if (explicit > 0) return explicit;
  if (op === "navigate" || op === "back" || op === "forward" || op === "reload") {
    const t = Number((args && args.timeout) || 0);
    return t > 0 ? t + 15000 : 60000;
  }
  return OP_TIMEOUT_MS;
}

function withTimeout(p, ms, tag) {
  return new Promise((resolve, reject) => {
    let done = false;
    const to = setTimeout(() => {
      if (done) return;
      done = true;
      const err = new Error("op timeout " + ms + "ms: " + tag);
      err.timeout = true;
      reject(err);
    }, ms);
    Promise.resolve(p).then(
      (v) => {
        if (done) return;
        done = true;
        clearTimeout(to);
        resolve(v);
      },
      (e) => {
        if (done) return;
        done = true;
        clearTimeout(to);
        reject(e);
      }
    );
  });
}

async function dialog(tabId, mode) {
  if (mode === "arm") {
    try {
      await cdp(tabId, "Page.enable", {});
    } catch (e) {}
    return;
  }
  try {
    if (tabId == null) {
      const [t] = await chrome.tabs.query({ active: true, currentWindow: true });
      tabId = t && t.id;
    }
    if (tabId == null) return;
    try {
      await chrome.debugger.sendCommand({ tabId }, "Page.handleJavaScriptDialog", { accept: true });
    } catch (e) {
      try {
        await chrome.debugger.attach({ tabId }, "1.3");
        await chrome.debugger.sendCommand({ tabId }, "Page.handleJavaScriptDialog", { accept: true });
      } catch (e2) {}
    }
  } catch (e) {}
}
const detachTimers = new Map();

function scheduleDetach(tabId) {
  const prev = detachTimers.get(tabId);
  if (prev) clearTimeout(prev);
  detachTimers.set(
    tabId,
    setTimeout(() => {
      detachTimers.delete(tabId);
      try {
        chrome.debugger.detach({ tabId }).catch(() => {});
      } catch (e) {}
      pageEnabled.delete(tabId);
    }, 8000)
  );
}

// Un diálogo abierto ANTES de que el debugger se enganche no emite
// `Page.javascriptDialogOpening` (el listener aún no existía) y deja
// `Runtime.evaluate` colgado para siempre. Se descarta antes de evaluar.
async function dismissStaleDialog(tabId) {
  try {
    await chrome.debugger.sendCommand({ tabId }, "Page.handleJavaScriptDialog", { accept: true });
  } catch (e) {}
}

async function cdp(tabId, method, params) {
  hookDialogs();
  const prev = detachTimers.get(tabId);
  if (prev) {
    clearTimeout(prev);
    detachTimers.delete(tabId);
  }
  try {
    if (!pageEnabled.has(tabId)) {
      try {
        await chrome.debugger.sendCommand({ tabId }, "Page.enable", {});
        pageEnabled.add(tabId);
      } catch (e) {}
    }
    if (method === "Runtime.evaluate") await dismissStaleDialog(tabId);
    const r = await chrome.debugger.sendCommand({ tabId }, method, params || {});
    scheduleDetach(tabId);
    return r;
  } catch (e) {
    pageEnabled.delete(tabId);
    try {
      await chrome.debugger.attach({ tabId }, "1.3");
      await chrome.debugger.sendCommand({ tabId }, "Page.enable", {});
      pageEnabled.add(tabId);
    } catch (e2) {}
    if (method === "Runtime.evaluate") await dismissStaleDialog(tabId);
    const r = await chrome.debugger.sendCommand({ tabId }, method, params || {});
    scheduleDetach(tabId);
    return r;
  }
}

const CXT_UI_OFF = "(()=>{let s=document.getElementById('cxt-auto-suppress');" +
  "if(!s){s=document.createElement('style');s.id='cxt-auto-suppress';" +
  "s.textContent='[data-cxt-auto-ui]{pointer-events:none!important}';" +
  "document.documentElement.appendChild(s);}return true;})()";
const CXT_UI_ON = "(()=>{const s=document.getElementById('cxt-auto-suppress');" +
  "if(s)s.remove();return true;})()";

async function setCxtUi(tabId, on) {
  try {
    await cdp(tabId, "Runtime.evaluate", { expression: on ? CXT_UI_ON : CXT_UI_OFF, returnByValue: true });
  } catch (e) {}
}

async function clickAt(tabId, x, y) {
  // La barra flotante de CXT puede quedar encima del compositor/botones.
  await setCxtUi(tabId, false);
  try {
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none", buttons: 0 });
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", buttons: 1, clickCount: 1 });
    await sleep(60);
    await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", buttons: 1, clickCount: 1 });
  } finally {
    await setCxtUi(tabId, true);
  }
}

async function loop() {
  if (running) return;
  running = true;
  runningSince = Date.now();
  const myLoop = {};
  currentLoop = myLoop;
  if (pollCtl) {
    try {
      pollCtl.abort();
    } catch (e) {}
  }
  pollCtl = new AbortController();
  const ctl = pollCtl;
  try {
    while (true) {
      if (currentLoop !== myLoop) return;
      if (ctl.signal.aborted) return;
      let c;
      try {
        c = await cfg();
      } catch (e) {
        await sleep(1000);
        continue;
      }
      if (!c.enabled) {
        await reportState(c);
        return;
      }
      let cmd = null;
      try {
        const res = await fetch(qs(c.server, c.token), { cache: "no-store", signal: ctl.signal });
        if (res.status === 204) continue;
        if (!res.ok) {
          await sleep(1000);
          continue;
        }
        cmd = await res.json();
        if (cmd && cmd.id) {
          try {
            await post(c.server, "/ack", c.token, { id: cmd.id });
          } catch (e) {}
        }
      } catch (e) {
        await sleep(1000);
        continue;
      }
      if (!cmd || !cmd.id) continue;
      if (processedIds.has(cmd.id)) {
        try {
          await post(c.server, "/result", c.token, { id: cmd.id, ok: false, error: "duplicado ignorado" });
        } catch (e) {}
        continue;
      }
      markProcessed(cmd.id);
      let out;
      try {
        out = {
          id: cmd.id,
          ok: true,
          data: await withTimeout(exec(cmd.op, cmd.args || {}), opTimeout(cmd.op, cmd.args), cmd.op),
        };
      } catch (e) {
        out = { id: cmd.id, ok: false, error: String(e && e.message ? e.message : e) };
        if (e && e.timeout) await dialog(cmd.args && cmd.args.tabId, "accept");
      }
      if (Date.now() - lastStore > 1500) {
        lastStore = Date.now();
        try {
          await chrome.storage.local.set({
            last: { op: cmd.op, ok: out.ok, error: out.error || "", at: Date.now() },
          });
        } catch (e) {}
      }
      try {
        await post(c.server, "/result", c.token, out);
      } catch (e) {}
    }
  } finally {
    if (currentLoop === myLoop) {
      running = false;
      runningSince = 0;
    }
  }
}

function waitLoad(tabId, timeout) {
  return new Promise((resolve) => {
    let done = false;
    const to = setTimeout(() => {
      if (!done) {
        done = true;
        chrome.tabs.onUpdated.removeListener(l);
        resolve(false);
      }
    }, timeout);
    function l(id, info) {
      if (id === tabId && info.status === "complete") {
        if (!done) {
          done = true;
          clearTimeout(to);
          chrome.tabs.onUpdated.removeListener(l);
          resolve(true);
        }
      }
    }
    chrome.tabs.onUpdated.addListener(l);
  });
}

// Pestaña/ventana propia de automatizacion (no roba foco). El id puede morir al
// cerrar Chrome: por eso se valida con tabs.get y se recrea si esta obsoleto.
const AUTO_TAB_KEY = "cxt_auto_tab_id";
const AUTO_WINDOW_KEY = "cxt_auto_window_id";

async function clearAutoTabKeys() {
  try {
    await chrome.storage.local.remove([AUTO_TAB_KEY, AUTO_WINDOW_KEY]);
  } catch (e) {}
}

// Devuelve {tabId, windowId} si la pestaña marcada sigue viva y es de Facebook.
// Si no existe, esta cerrada o ya no es Facebook, limpia las claves y devuelve null.
async function getStoredAutoTab() {
  const s = await chrome.storage.local.get([AUTO_TAB_KEY, AUTO_WINDOW_KEY]);
  const tabId = Number(s && s[AUTO_TAB_KEY]);
  if (!Number.isInteger(tabId) || tabId <= 0) {
    await clearAutoTabKeys();
    return null;
  }
  let t;
  try {
    t = await chrome.tabs.get(tabId);
  } catch (e) {
    await clearAutoTabKeys();
    return null;
  }
  const url = String((t && t.url) || "");
  if (!url.toLowerCase().includes("facebook.com")) {
    await clearAutoTabKeys();
    return null;
  }
  const windowId = Number(t.windowId);
  if (!Number.isInteger(windowId) || windowId <= 0) {
    await clearAutoTabKeys();
    return null;
  }
  // La ventana pudo cambiar de id al reiniciar Chrome: se re-guarda el real.
  if (Number(s && s[AUTO_WINDOW_KEY]) !== windowId) {
    try {
      await chrome.storage.local.set({ [AUTO_WINDOW_KEY]: windowId });
    } catch (e) {}
  }
  return { tabId: t.id, windowId };
}

// Receta de medicion (no publica nada):
//   ./bin/cxt tab.ensure_auto '{}'          -> guarda tabId/windowId
//   hyprctl activewindow -j                -> la ventana activa NO debe ser la de automatizacion
//   ./bin/cxt eval '{"tabId":<id>,"expression":"({v:document.visibilityState,f:document.hasFocus()})"}'
// Esperado: v === "visible" (pestaña activa dentro de su propia ventana) y f === false
// cuando el usuario conserva el foco. En Wayland/Hyprland f puede salir true si el WM
// enfoca la ventana nueva: eso se mide, no se asume (ver plan 2.7).

async function resolveTab(args) {
  if (args && args.tabId != null) return await chrome.tabs.get(args.tabId);
  // Automatizacion: nunca usar la pestaña activa de la ventana del usuario.
  // Debe pasar un tabId explicito (normalmente el de tab.ensure_auto).
  if (args && args.automation === true) {
    throw new Error("resolveTab: automatizacion requiere tabId explicito (usa tab.ensure_auto)");
  }
  const [t] = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  if (!t) throw new Error("no hay pestana activa");
  return t;
}

async function exec(op, args) {
  const c = await cfg();
  if (!c.enabled) throw new Error("CXT DESACTIVADA (kill switch): ninguna funcion disponible");
  switch (op) {
    case "ping":
      return { pong: true, ts: Date.now() };
    case "tabs": {
      const ts = await chrome.tabs.query({});
      return ts.map((t) => ({
        id: t.id,
        url: t.url,
        title: t.title,
        active: t.active,
        windowId: t.windowId,
        index: t.index,
      }));
    }
    case "tab.new": {
      // Compatibilidad: conserva el default del CLI (active por defecto, roba foco).
      // La automatizacion debe usar tab.ensure_auto, no tab.new.
      const t = await chrome.tabs.create({ url: args.url || "about:blank", active: args.active !== false });
      if (args.wait !== false) await waitLoad(t.id, 30000);
      const nt = await chrome.tabs.get(t.id);
      return { id: nt.id, url: nt.url, title: nt.title };
    }
    case "tab.activate": {
      // Uso explicito del usuario/CLI: activa la pestaña Y enfoca su ventana.
      // La automatizacion NO debe usar esta op; usar tab.ensure_auto + tab.show.
      if (args.tabId == null) return { ok: false, error: "tab.activate: falta tabId" };
      let t;
      try {
        t = await withTimeout(chrome.tabs.update(args.tabId, { active: true }), 8000, "tab.activate");
      } catch (e) {
        await dialog(args.tabId, "accept");
        t = await withTimeout(chrome.tabs.update(args.tabId, { active: true }), 8000, "tab.activate");
      }
      if (t.windowId != null) {
        try {
          await withTimeout(chrome.windows.update(t.windowId, { focused: true }), 5000, "win.focus");
        } catch (e) {}
      }
      return { id: t.id, url: t.url };
    }
    case "tab.ensure_auto": {
      // Pestaña propia reutilizable para automatizacion. Nunca llama a windows.update.
      // En Wayland el foco lo decide el compositor: focused:false es solo una peticion.
      // Por eso el titulo inicial debe existir ANTES de que Hyprland mapee la ventana;
      // la regla cxt_auto_nofocus hace match por titulo. Abrimos una pagina puente con
      // <title>IA TRABAJANDO...</title> y solo despues navegamos.
      const a = args || {};
      if (a.reuse !== false) {
        const cur = await getStoredAutoTab();
        if (cur) return { tabId: cur.tabId, windowId: cur.windowId, created: false, focused: false };
      }
      const startUrl = chrome.runtime.getURL("auto-start.html");
      const win = await chrome.windows.create({ url: startUrl, type: "normal", focused: false });
      const tabs = await chrome.tabs.query({ windowId: win.id });
      const tab = tabs && tabs[0];
      if (!tab || tab.id == null) throw new Error("tab.ensure_auto: ventana sin pestaña");
      await waitLoad(tab.id, 5000);
      // Guardar ANTES de navegar: el content script de Facebook pregunta si esta
      // pestaña es la automática al arrancar; si el id aún no está guardado, la
      // respuesta sería "no" y el título/resumen no se aplicarían.
      await chrome.storage.local.set({ [AUTO_TAB_KEY]: tab.id, [AUTO_WINDOW_KEY]: win.id });
      await chrome.tabs.update(tab.id, { url: a.url || "https://www.facebook.com/", active: true });
      return { tabId: tab.id, windowId: win.id, created: true, focused: false };
    }
    case "tab.show": {
      // Solo selecciona la pestaña dentro de su propia ventana. No enfoca la ventana
      // (nada de windows.update); es el unico "mostrar" permitido a la automatizacion.
      if (args == null || args.tabId == null) return { ok: false, error: "tab.show: falta tabId" };
      const t = await chrome.tabs.update(args.tabId, { active: true });
      return { tabId: t.id, active: true, focused: false };
    }
    case "tab.close":
      if (args.tabId == null) return { ok: false, error: "tab.close: falta tabId" };
      await chrome.tabs.remove(args.tabId);
      return { closed: args.tabId };
    case "navigate": {
      if (!args.url) return { ok: false, error: "navigate: falta url" };
      const t = await resolveTab(args);
      await dialog(t.id, "arm");
      const nav = chrome.tabs.update(t.id, { url: args.url }).then(() => true, () => false);
      const committed = await Promise.race([nav, sleep(6000).then(() => false)]);
      if (!committed) {
        try {
          await cdp(t.id, "Page.navigate", { url: args.url });
        } catch (e) {}
        await dialog(t.id, "accept");
      }
      await waitLoad(t.id, args.timeout || 30000);
      const nt = await chrome.tabs.get(t.id);
      return { id: nt.id, url: nt.url, title: nt.title };
    }
    case "back": {
      const t = await resolveTab(args);
      await chrome.tabs.goBack(t.id);
      await waitLoad(t.id, 15000);
      return { url: (await chrome.tabs.get(t.id)).url };
    }
    case "forward": {
      const t = await resolveTab(args);
      await chrome.tabs.goForward(t.id);
      await waitLoad(t.id, 15000);
      return { url: (await chrome.tabs.get(t.id)).url };
    }
    case "reload": {
      const t = await resolveTab(args);
      await chrome.tabs.reload(t.id);
      await waitLoad(t.id, 20000);
      return { url: (await chrome.tabs.get(t.id)).url };
    }
    case "wait":
      await sleep(args.ms || 500);
      return { waited: args.ms || 500 };
    case "screenshot": {
      if (args == null || typeof args !== "object") return { ok: false, error: "screenshot: args invalidos" };
      if (args.format && args.format !== "png" && args.format !== "jpeg") return { ok: false, error: "screenshot: formato invalido" };
      const t = await resolveTab(args);
      await chrome.tabs.update(t.id, { active: true });
      if (t.windowId != null) await chrome.windows.update(t.windowId, { focused: true });
      await sleep(150);
      if (t.windowId == null) return { ok: false, error: "screenshot: sin windowId" };
      const dataUrl = await chrome.tabs.captureVisibleTab(t.windowId, { format: args.format || "png" });
      return { dataUrl };
    }
    case "cdp": {
      if (!args || typeof args.method !== "string" || !args.method) return { ok: false, error: "cdp: falta method" };
      return { result: await cdp((await resolveTab(args)).id, args.method, args.params) };
    }
    case "cdpclick": {
      if (!args || !Number.isFinite(Number(args.x)) || !Number.isFinite(Number(args.y))) return { ok: false, error: "cdpclick: faltan x e y numericos" };
      const t = await resolveTab(args);
      const x = args.x;
      const y = args.y;
      await clickAt(t.id, x, y);
      return { clicked: true, x, y };
    }
    case "cdptype": {
      const t = await resolveTab(args);
      await cdp(t.id, "Input.insertText", { text: args.value != null ? args.value : args.text || "" });
      return { typed: true };
    }
    case "cdpkey": {
      const t = await resolveTab(args);
      const key = args.key || "Enter";
      const code = KMAP[key] != null ? KMAP[key] : key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0;
      await cdp(t.id, "Input.dispatchKeyEvent", { type: "keyDown", key, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code });
      await cdp(t.id, "Input.dispatchKeyEvent", { type: "keyUp", key, windowsVirtualKeyCode: code, nativeVirtualKeyCode: code });
      return { pressed: true, key };
    }
    case "cdpclicktext": {
      const t = await resolveTab(args);
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: t.id },
        func: pageFn,
        args: ["deepfind", { selector: args.selector || "*", text: args.text, visibleOnly: true, limit: args.limit || 400, exact: !!args.exact }],
      });
      let items = (r && r.result && r.result.items) || [];
      if (args.minW) items = items.filter((x) => x.w >= args.minW);
      if (args.minH) items = items.filter((x) => x.h >= args.minH);
      if (args.maxH) items = items.filter((x) => x.h <= args.maxH);
      items.sort((a, b) => b.w * b.h - a.w * a.h);
      const p = items[0];
      if (!p) return { clicked: false, found: false, candidates: items.length };
      const x = p.x + Math.floor(p.w / 2);
      const y = p.y + Math.floor(p.h / 2);
      await clickAt(t.id, x, y);
      return { clicked: true, x, y, tag: p.tag, text: p.text, match_level: p.match || null, click_method: "cdp" };
    }
    case "cdpclickref": {
      if (!args || args.ref == null || args.ref === "") return { ok: false, error: "cdpclickref: falta ref" };
      const t = await resolveTab(args);
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: t.id },
        func: pageFn,
        args: ["refbox", { ref: args.ref }],
      });
      const b = r && r.result;
      if (!b || !b.found) return { clicked: false, found: false, ref: args.ref || null };
      const x = b.x + Math.floor(b.w / 2);
      const y = b.y + Math.floor(b.h / 2);
      await clickAt(t.id, x, y);
      return { clicked: true, ref: args.ref, x, y, tag: b.tag, match_level: "exact", click_method: "cdp" };
    }
    case "snapshot": {
      const t = await resolveTab(args);
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: t.id },
        func: pageFn,
        args: ["axsnap", args],
      });
      const res = (r && r.result) || { items: [], total: 0 };
      const items = res.items || [];
      if (args.text) {
        return { mode: "query", kept: items.length, text: snapText(items, args) };
      }
      return { mode: args.mode || "act", total: res.total, kept: items.length, text: snapText(items, args) };
    }
    case "probe": {
      const t = await resolveTab(args);
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: t.id },
        func: pageFn,
        args: ["probe", args],
      });
      return (r && r.result) || { ok: false, n: 0 };
    }
    case "batch": {
      const t = await resolveTab(args);
      const steps = args.steps || null;
      if (!steps || !steps.length) return { ok: false, error: "sin pasos" };
      const sub = (v) => (typeof v === "string" ? v.split("$TEXT").join(args.text || "") : v);
      const results = [];
      let ok = true;
      for (const st of steps) {
        const a = {};
        for (const k in st.args || {}) a[k] = sub(st.args[k]);
        a.tabId = t.id;
        let res;
        try {
          res = await exec(st.op, a);
        } catch (e) {
          results.push({ op: st.op, ok: false, error: String((e && e.message) || e) });
          ok = false;
          break;
        }
        const c = compact(st.op, res);
        results.push(c);
        if (st.require && c && (c.ok === false || c.clicked === false || c.typed === false || c.found === false)) {
          ok = false;
          break;
        }
      }
      return { ok, steps: results.length, results };
    }
    default: {
      if (typeof op !== "string" || !op) return { ok: false, error: "op invalida" };
      if (args == null || typeof args !== "object") return { ok: false, error: "args invalidos para " + op };
      const t = await resolveTab(args);
      if (op.startsWith("scrape.")) {
        const u = (t && t.url) || "";
        let bad = /^(chrome|chrome-extension|edge|about|view-source|devtools):/i.test(u);
        if (!bad) {
          try {
            const h = new URL(u).hostname;
            if (h === "chromewebstore.google.com" || h === "chrome.google.com") bad = true;
          } catch (e) {}
        }
        if (bad) return { error: "UNSUPPORTED_URL" };
        try {
          const [rr] = await chrome.scripting.executeScript({
            target: { tabId: t.id },
            func: pageFn,
            args: [op, args],
          });
          return rr ? rr.result : null;
        } catch (e) {
          return { error: "NO_TAB" };
        }
      }
      const [r] = await chrome.scripting.executeScript({
        target: { tabId: t.id },
        func: pageFn,
        args: [op, args],
      });
      return r ? r.result : null;
    }
  }
}

const RESULT_KEYS = {
  ping: ["pong"],
  click: ["clicked", "tag", "text", "match_level", "click_method"],
  cdpclicktext: ["clicked", "tag", "text", "match_level", "click_method"],
  cdpclickref: ["clicked", "ref", "tag", "click_method"],
  cdpclick: ["clicked", "x", "y"],
  cdptype: ["typed"],
  cdpkey: ["pressed", "key"],
  type: ["typed", "tag"],
  fill: ["typed", "tag"],
  key: ["pressed", "key"],
  scroll: ["scrolled", "x", "y"],
  wait: ["waited"],
  waitFor: ["found", "ms"],
  navigate: ["url", "title"],
  reload: ["url"],
  back: ["url"],
  forward: ["url"],
  probe: ["ok", "n", "ref"],
  snapshot: ["mode", "total", "kept"],
  read: ["url", "title", "chars"],
  eval: ["value", "error"],
  batch: ["ok", "steps"],
  "tab.ensure_auto": ["tabId", "windowId", "created", "focused"],
  "tab.show": ["tabId", "active", "focused"],
  "scrape.page": ["url","title","text","chars","mode","frames"],
  "scrape.markdown": ["url","title","markdown","chars"],
  "scrape.tables": ["url","tables","truncated"],
  "scrape.links": ["url","links","truncated"],
};

function compact(op, res) {
  if (res == null || typeof res !== "object") return res;
  const keys = RESULT_KEYS[op];
  if (!keys) return { ok: res.ok, error: res.error };
  const out = {};
  for (const k of keys) if (k in res) out[k] = res[k];
  return out;
}

function snapLine(it) {
  let s = "@" + it.ref + " " + it.role;
  if (it.name) s += ' "' + String(it.name).replace(/"/g, "'").slice(0, 48) + '"';
  return s;
}

function snapText(items, opts) {
  let body = items.map(snapLine).join("\n");
  const cap = (opts && opts.maxChars) || 6000;
  let trunc = false;
  if (body.length > cap) {
    body = body.slice(0, cap);
    trunc = true;
  }
  return body + (trunc ? "\n...[truncado; sube maxChars o usa query]" : "");
}

// Injected into the page. Must be fully self-contained (no outer references).
async function pageFn(op, args) {
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const NODE_CAP = 8000;
  const VIS = new WeakMap();
  const visible = (el) => {
    if (!el) return false;
    let v = VIS.get(el);
    if (v !== undefined) return v;
    v = false;
    try {
      if (el.checkVisibility) {
        v = el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
      } else {
        const r = el.getClientRects();
        if (r && r.length) {
          const s = getComputedStyle(el);
          v = s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0";
        }
      }
    } catch (e) {}
    VIS.set(el, v);
    return v;
  };
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const deepAll = (sel, cap) => {
    const limit = cap || NODE_CAP;
    const out = [];
    const add = (list) => {
      for (const el of list) {
        out.push(el);
        if (out.length >= limit) return true;
      }
      return false;
    };
    try {
      if (add(document.querySelectorAll(sel || "*"))) return out;
    } catch (e) {}
    if (out.length >= limit) return out;
    let hosts = [];
    try {
      hosts = document.querySelectorAll("*");
    } catch (e) {}
    const n = Math.min(hosts.length, NODE_CAP * 2);
    for (let i = 0; i < n; i++) {
      const sr = hosts[i].shadowRoot;
      if (!sr) continue;
      try {
        if (add(sr.querySelectorAll(sel || "*"))) break;
      } catch (e) {}
    }
    return out;
  };
  // Recorre el arbol y devuelve texto plano incluyendo shadowRoot abiertos.
  function textOf(root) {
    const out = [];
    let n = 0;
    const walk = (node) => {
      if (!node || n >= NODE_CAP) return;
      n++;
      if (node.nodeType === 3) {
        const t = (node.nodeValue || "").trim();
        if (t) out.push(t);
        return;
      }
      if (node.nodeType !== 1) return;
      const tag = String(node.tagName || "").toLowerCase();
      if (tag === "script" || tag === "style" || tag === "noscript" || tag === "template") return;
      try {
        if (node.shadowRoot) walk(node.shadowRoot);
      } catch (e) {}
      for (const ch of node.childNodes) walk(ch);
    };
    walk(root);
    return out.join("\n");
  }

  // Convierte un nodo a Markdown (GFM) sin salir de la pagina.
  function mdOf(root) {
    let n = 0;
    const cell = (s) => String(s == null ? "" : s).replace(/\r?\n+/g, " ").replace(/\|/g, "\\|").trim();
    const kids = (node) => {
      let s = "";
      try {
        if (node.shadowRoot) for (const ch of node.shadowRoot.childNodes) s += one(ch);
      } catch (e) {}
      for (const ch of node.childNodes) s += one(ch);
      return s;
    };
    const one = (node) => {
      if (!node || n >= NODE_CAP) return "";
      n++;
      if (node.nodeType === 3) return node.nodeValue || "";
      if (node.nodeType !== 1) return "";
      const tag = String(node.tagName || "").toLowerCase();
      if (tag === "script" || tag === "style" || tag === "noscript") return "";
      if (/^h[1-6]$/.test(tag)) return "#".repeat(+tag[1]) + " " + kids(node).trim() + "\n\n";
      if (tag === "p") return kids(node).trim() + "\n\n";
      if (tag === "strong" || tag === "b") return "**" + kids(node).trim() + "**";
      if (tag === "em" || tag === "i") return "*" + kids(node).trim() + "*";
      if (tag === "ul" || tag === "ol") {
        let i = 0;
        let s = "";
        for (const ch of node.children) {
          if (!ch || String(ch.tagName || "").toLowerCase() !== "li") continue;
          i++;
          s += (tag === "ol" ? i + ". " : "- ") + kids(ch).trim() + "\n";
        }
        return s + "\n";
      }
      if (tag === "blockquote") return kids(node).trim().split(/\n+/).map((l) => "> " + l).join("\n") + "\n\n";
      if (tag === "pre") return "```\n" + (node.textContent || "") + "\n```\n\n";
      if (tag === "code") return "`" + (node.textContent || "") + "`";
      if (tag === "a") {
        const href = node.getAttribute("href") || "";
        const txt = kids(node).trim();
        if (!href || /^javascript:/i.test(href)) return txt;
        return "[" + txt + "](" + href + ")";
      }
      if (tag === "img") return "![" + (node.getAttribute("alt") || "") + "](" + (node.getAttribute("src") || "") + ")";
      if (tag === "table") {
        const rows = [];
        for (const tr of node.querySelectorAll("tr")) {
          const cs = [];
          for (const c of tr.querySelectorAll("th,td")) cs.push(cell(c.textContent));
          if (cs.length) rows.push(cs);
        }
        if (!rows.length) return kids(node).trim() + "\n\n";
        const head = rows[0];
        let s = "| " + head.join(" | ") + " |\n| " + head.map(() => "---").join(" | ") + " |\n";
        for (let i = 1; i < rows.length; i++) s += "| " + rows[i].join(" | ") + " |\n";
        return s + "\n";
      }
      if (tag === "br") return "  \n";
      if (tag === "hr") return "---\n\n";
      return kids(node);
    };
    return one(root).replace(/\n{3,}/g, "\n\n").trim();
  }

  // Resuelve la raiz de scraping: selector -> main -> body.
  function scrapeRoot(a) {
    if (a && a.selector) {
      try {
        const el = document.querySelector(a.selector);
        if (el) return { root: el, mode: "selector" };
      } catch (e) {
        return { error: "SELECTOR_INVALID" };
      }
    }
    if (a && a.main !== false) {
      for (const el of document.querySelectorAll("main, article, [role=main]")) {
        if (el.innerText && el.innerText.length > 200) return { root: el, mode: "main" };
      }
    }
    return { root: document.body || document.documentElement, mode: "body" };
  }

  const all = () => Array.from(document.querySelectorAll(args.selector || "*"));
  const pick = () => {
    const els = all();
    if (args.nth != null) return els[args.nth];
    return els.find(visible) || els[0];
  };
  const matchLevel = (s, t, exact) => {
    if (!s || !t) return null;
    if (s === t) return "exact";
    if (!exact && s.includes(t)) return "stable";
    return null;
  };
  const byText = () => {
    const t = norm(args.text);
    if (!t) return null;
    const sel = args.selector || "button,a,[role=button],input[type=submit],div,span";
    const cands = deepAll(sel).filter((e) => args.force || visible(e));
    let best = null;
    let bestLen = Infinity;
    let level = null;
    for (const el of cands) {
      const s = norm(el.innerText || el.value || el.getAttribute("aria-label") || el.textContent);
      const lvl = matchLevel(s, t, !!args.exact);
      if (lvl && s.length < bestLen) {
        best = el;
        bestLen = s.length;
        level = lvl;
      }
    }
    byText.level = level;
    return best;
  };
  const setValue = (el, val) => {
    el.focus();
    if (el.isContentEditable) {
      try {
        document.execCommand("selectAll", false, null);
      } catch (e) {}
      let ok = false;
      try {
        ok = document.execCommand("insertText", false, val);
      } catch (e) {}
      if (!ok) {
        el.textContent = val;
        try {
          el.dispatchEvent(new InputEvent("input", { bubbles: true, data: val, inputType: "insertText" }));
        } catch (e) {
          el.dispatchEvent(new Event("input", { bubbles: true }));
        }
      }
      el.dispatchEvent(new Event("change", { bubbles: true }));
      return;
    }
    const desc = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value");
    if (desc && desc.set) desc.set.call(el, val);
    else el.value = val;
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const scrollTo = (el) => {
    try {
      el.scrollIntoView({ block: "center", inline: "center" });
    } catch (e) {}
  };
  const press = (el, key) => {
    const KMAP = { Enter: 13, Tab: 9, Escape: 27, Backspace: 8, Delete: 46, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40, Home: 36, End: 35, PageUp: 33, PageDown: 34, " ": 32 };
    const code = KMAP[key] != null ? KMAP[key] : key.length === 1 ? key.toUpperCase().charCodeAt(0) : 0;
    el.dispatchEvent(new KeyboardEvent("keydown", { key, keyCode: code, which: code, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keypress", { key, keyCode: code, which: code, bubbles: true }));
    el.dispatchEvent(new KeyboardEvent("keyup", { key, keyCode: code, which: code, bubbles: true }));
  };
  const realClick = (el) => {
    const o = { bubbles: true, cancelable: true, composed: true, view: window };
    try { el.dispatchEvent(new PointerEvent("pointerdown", o)); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent("mousedown", o)); } catch (e) {}
    try { el.dispatchEvent(new PointerEvent("pointerup", o)); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent("mouseup", o)); } catch (e) {}
    try { el.dispatchEvent(new MouseEvent("click", o)); } catch (e) {}
    try { el.click(); } catch (e) {}
  };

  switch (op) {
    case "read": {
      const max = args.max || 6000;
      let text = document.body ? document.body.innerText : "";
      if (args.mode === "main") {
        const m = document.querySelector("main, article, [role=main]");
        if (m && m.innerText && m.innerText.length > 200) text = m.innerText;
      }
      return { url: location.href, title: document.title, chars: text.length, text: text.slice(0, max) };
    }
    case "scrape.page": {
      if (!/html/i.test(document.contentType || "")) return { error: "NOT_HTML" };
      const sr = scrapeRoot(args);
      if (sr.error) return { error: sr.error };
      const text = textOf(sr.root);
      const max = args.max || 200000;
      return {
        url: location.href,
        title: document.title,
        text: text.slice(0, max),
        chars: text.length,
        mode: sr.mode,
        frames: document.querySelectorAll("iframe").length,
      };
    }
    case "scrape.markdown": {
      if (!/html/i.test(document.contentType || "")) return { error: "NOT_HTML" };
      const sr = scrapeRoot(args);
      if (sr.error) return { error: sr.error };
      const markdown = mdOf(sr.root);
      const max = args.max || 200000;
      if (markdown.length > max) return { error: "TOO_BIG", chars: markdown.length };
      return { url: location.href, title: document.title, markdown, chars: markdown.length };
    }
    case "scrape.tables": {
      if (!/html/i.test(document.contentType || "")) return { error: "NOT_HTML" };
      const tables = [];
      let truncated = false;
      let totalRows = 0;
      for (const tb of deepAll("table, [role=table]", NODE_CAP)) {
        if (totalRows >= 500) {
          truncated = true;
          break;
        }
        let caption = "";
        try {
          const cap = tb.querySelector("caption");
          caption = (cap && cap.textContent.trim()) || tb.getAttribute("aria-label") || "";
        } catch (e) {}
        const trs = Array.from(tb.querySelectorAll("tr, [role=row]"));
        let headerRow = null;
        for (const tr of trs) {
          if (tr.querySelector("th")) {
            headerRow = tr;
            break;
          }
        }
        const headers = [];
        if (headerRow) {
          const hs = Array.from(headerRow.querySelectorAll("th"));
          if (hs.length > 50) truncated = true;
          for (const c of hs.slice(0, 50)) headers.push((c.innerText || "").trim());
        }
        const rows = [];
        for (const tr of trs) {
          if (tr === headerRow) continue;
          if (totalRows >= 500) {
            truncated = true;
            break;
          }
          const cells = Array.from(tr.querySelectorAll("td,th,[role=cell],[role=gridcell],[role=columnheader]"));
          if (cells.length > 50) truncated = true;
          rows.push(cells.slice(0, 50).map((c) => (c.innerText || "").trim()));
          totalRows++;
        }
        tables.push({ caption, headers, rows });
      }
      const result = { url: location.href, tables, truncated };
      while (JSON.stringify(result).length > 800000) {
        let last = null;
        for (let i = result.tables.length - 1; i >= 0; i--) {
          if (result.tables[i].rows.length) {
            last = result.tables[i];
            break;
          }
        }
        if (!last) break;
        last.rows.pop();
        result.truncated = true;
      }
      return result;
    }
    case "scrape.links": {
      if (!/html/i.test(document.contentType || "")) return { error: "NOT_HTML" };
      const seen = new Set();
      const links = [];
      let truncated = false;
      for (const a of deepAll("a[href]", NODE_CAP)) {
        if (links.length >= 2000) {
          truncated = true;
          break;
        }
        const href = a.href || "";
        if (!href || /^javascript:/i.test(href)) continue;
        if (seen.has(href)) continue;
        seen.add(href);
        let sameOrigin = false;
        try {
          sameOrigin = new URL(href).origin === location.origin;
        } catch (e) {}
        links.push({ text: (a.innerText || "").trim().slice(0, 120), href: href.slice(0, 300), sameOrigin });
      }
      const result = { url: location.href, links, truncated };
      while (JSON.stringify(result).length > 800000 && result.links.length) {
        result.links.pop();
        result.truncated = true;
      }
      return result;
    }
    case "click": {
      const el = args.text ? byText() : pick();
      if (!el) return { clicked: false, error: "elemento no encontrado", selector: args.selector || null, text: args.text || null };
      scrollTo(el);
      await sleep(50);
      realClick(el);
      return { clicked: true, tag: el.tagName, text: norm(el.innerText || el.value || el.getAttribute("aria-label")).slice(0, 80), match_level: args.text ? byText.level : null, click_method: "js" };
    }
    case "type":
    case "fill": {
      let el = null;
      if (args.selector) el = pick();
      else if (args.placeholder) {
        el = Array.from(document.querySelectorAll("input,textarea,[contenteditable=true]")).find(
          (e) =>
            visible(e) &&
            (norm(e.getAttribute("placeholder")).includes(norm(args.placeholder)) ||
              norm(e.getAttribute("aria-label")).includes(norm(args.placeholder)))
        );
      } else if (args.text) el = byText();
      else {
        el = document.activeElement;
        let guard = 0;
        while (el && el.shadowRoot && el.shadowRoot.activeElement && guard++ < 10) {
          el = el.shadowRoot.activeElement;
        }
      }
      if (el && !el.isContentEditable && !("value" in el)) {
        const cand = deepAll("[contenteditable]").find(visible);
        if (cand) el = cand;
      }
      if (!el) return { typed: false, error: "input no encontrado" };
      const val = args.value != null ? args.value : args.text != null ? args.text : "";
      scrollTo(el);
      setValue(el, args.append ? (el.value || "") + val : val);
      if (args.submit) press(el, "Enter");
      return {
        typed: true,
        tag: el.tagName,
        value: (el.value || "").slice(0, 200),
        text: el.isContentEditable ? norm(el.innerText).slice(0, 200) : "",
      };
    }
    case "key": {
      const el = args.selector ? pick() : document.activeElement;
      if (!el) return { pressed: false };
      press(el, args.key || "Enter");
      return { pressed: true, key: args.key || "Enter", tag: el.tagName };
    }
    case "scroll": {
      if (args.selector) {
        const el = pick();
        if (el) scrollTo(el);
        return { scrolled: !!el };
      }
      window.scrollBy(args.x || 0, args.y || 0);
      return { x: window.scrollX, y: window.scrollY };
    }
    case "waitFor": {
      const to = args.timeout || 15000;
      const t0 = Date.now();
      while (Date.now() - t0 < to) {
        if (document.querySelector(args.selector)) return { found: true, ms: Date.now() - t0 };
        await sleep(200);
      }
      return { found: false, timeout: to };
    }
    case "eval": {
      let v;
      try {
        v = (0, eval)(args.code);
        if (v && typeof v.then === "function") v = await v;
      } catch (e) {
        return { error: String(e) };
      }
      try {
        return { value: v === undefined ? null : JSON.parse(JSON.stringify(v)) };
      } catch (e) {
        return { value: String(v) };
      }
    }
    case "find": {
      const all = document.querySelectorAll(args.selector || "*");
      const lim = args.limit || 25;
      const items = [];
      for (let i = 0; i < all.length && items.length < lim; i++) {
        const el = all[i];
        const vis = visible(el);
        if (args.visibleOnly && !vis) continue;
        const r = el.getBoundingClientRect();
        items.push({
          i,
          tag: el.tagName,
          role: el.getAttribute("role"),
          aria: el.getAttribute("aria-label"),
          ph: el.getAttribute("placeholder"),
          ce: el.isContentEditable || el.getAttribute("contenteditable") || null,
          vis,
          w: Math.round(r.width),
          h: Math.round(r.height),
          x: Math.round(r.left),
          y: Math.round(r.top),
          text: norm(el.textContent || el.value || el.getAttribute("aria-label")).slice(0, 60),
        });
      }
      return { count: items.length, scanned: all.length, items };
    }
    case "deepfind": {
      const t = norm(args.text || "");
      const lim = args.limit || 25;
      const els = deepAll(args.selector || "*", NODE_CAP);
      const items = [];
      for (let i = 0; i < els.length && items.length < lim; i++) {
        const el = els[i];
        const s = norm(el.textContent || el.value || el.getAttribute("aria-label") || "");
        if (t && (args.exact ? s !== t : !s.includes(t))) continue;
        const vis = visible(el);
        if (args.visibleOnly && !vis) continue;
        const r = el.getBoundingClientRect();
        items.push({
          i,
          tag: el.tagName,
          role: el.getAttribute("role"),
          aria: el.getAttribute("aria-label"),
          vis,
          w: Math.round(r.width),
          h: Math.round(r.height),
          x: Math.round(r.left),
          y: Math.round(r.top),
          text: s.slice(0, 60),
          match: t ? (s === t ? "exact" : s.includes(t) ? "stable" : null) : null,
        });
      }
      return { count: items.length, scanned: els.length, items };
    }
    case "refbox": {
      const el = deepAll('[data-cxt-ref="' + (args.ref || "") + '"]')[0];
      if (!el || !visible(el)) return { found: false };
      const r = el.getBoundingClientRect();
      return { found: true, tag: el.tagName, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
    }
    case "axsnap": {
      const inView = (args.mode || "act") === "act";
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      const sel =
        args.selector ||
        "a,button,input,textarea,select,[role],[contenteditable='true'],[aria-label],[tabindex],h1,h2,h3";
      const els = deepAll(sel);
      const best = new Map();
      for (const el of els) {
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 4 || r.height < 4) continue;
        if (inView && (r.bottom < 0 || r.top > vh || r.right < 0 || r.left > vw)) continue;
        const tag = el.tagName.toLowerCase();
        const role = (el.getAttribute("role") || "").toLowerCase() || tag;
        const name = norm(
          el.getAttribute("aria-label") ||
            el.getAttribute("placeholder") ||
            (el.value != null && el.value !== "" ? el.value : "") ||
            el.innerText ||
            el.title ||
            el.getAttribute("alt") ||
            ""
        ).slice(0, 60);
        if (!name && !["a", "button", "input", "textarea", "select"].includes(tag)) continue;
        const key = role + "|" + name;
        const area = r.width * r.height;
        const prev = best.get(key);
        if (!prev || area < prev.area) {
          best.set(key, {
            role,
            name,
            area,
            x: Math.round(r.left),
            y: Math.round(r.top),
            w: Math.round(r.width),
            h: Math.round(r.height),
            el,
          });
        }
      }
      const items = [];
      let n = 0;
      for (const v of best.values()) {
        const ref = "e" + n++;
        try {
          v.el.setAttribute("data-cxt-ref", ref);
        } catch (e) {}
        items.push({
          ref,
          role: v.role,
          name: v.name,
          x: v.x,
          y: v.y,
          w: v.w,
          h: v.h,
          sig: v.role + "|" + v.name + "|" + Math.round(v.x / 8) + "," + Math.round(v.y / 8),
        });
        if (items.length >= (args.limit || 300)) break;
      }
      if (args.text) {
        const q = norm(args.text);
        return { total: items.length, items: items.filter((it) => it.name.includes(q)) };
      }
      return { total: els.length, items };
    }
    case "probe": {
      const t = norm(args.text || "");
      const els = deepAll(args.selector || "*", NODE_CAP);
      let n = 0;
      let first = null;
      for (const el of els) {
        const s = norm(el.textContent || el.value || el.getAttribute("aria-label") || "");
        const hit = t ? (args.exact ? s === t : s.includes(t)) : true;
        if (!hit) continue;
        if (!visible(el)) continue;
        n++;
        if (!first) {
          const r = el.getBoundingClientRect();
          first = {
            role: el.getAttribute("role") || el.tagName.toLowerCase(),
            name: s.slice(0, 40),
            x: Math.round(r.left),
            y: Math.round(r.top),
          };
        }
        if (n > 500) break;
      }
      return { ok: n > 0, n: n, ref: first };
    }
    default:
      return { error: "op desconocida en pagina: " + op };
  }
}

async function reportState(c) {
  try {
    await post(c.server, "/state", c.token, { enabled: !!c.enabled });
  } catch (e) {}
}

async function applyBadge(on) {
  try {
    await chrome.action.setBadgeText({ text: on ? "ON" : "OFF" });
    await chrome.action.setBadgeBackgroundColor({ color: on ? "#2ecc71" : "#e74c3c" });
    await chrome.action.setTitle({ title: on ? "CXT — ACTIVADA" : "CXT — DESACTIVADA" });
  } catch (e) {}
}

async function init() {
  try {
    const s = await chrome.storage.local.get("autoStats");
    if (s && s.autoStats) autoStatsCache = s.autoStats;
  } catch (e) {
    console.error(e);
  }
  const c = await cfg();
  await applyBadge(c.enabled);
  await reportState(c);
  if (c.enabled) loop();
}

chrome.runtime.onInstalled.addListener(() => init().catch(console.error));
chrome.runtime.onStartup.addListener(() => init().catch(console.error));
chrome.alarms.create("cxt-poll", { periodInMinutes: 0.5 });
chrome.alarms.create("cxt-watch", { periodInMinutes: 1 });

async function watchdog() {
  if (!running) return;
  if (Date.now() - runningSince < 90000) return;
  running = false;
  runningSince = 0;
  try {
    const tabs = await chrome.tabs.query({ url: "*://*.facebook.com/*" });
    for (const t of tabs) await dialog(t.id, "accept");
  } catch (e) {}
  try {
    if (pollCtl) pollCtl.abort();
  } catch (e) {}
  loop();
}

chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "cxt-poll") loop().catch(() => {});
  else if (a.name === "cxt-watch") watchdog().catch(() => {});
});
const API = {
  test: { path: "/health" },
  schedGet: { path: "/schedule" },
  schedSet: { path: "/schedule", method: "POST" },
  shareGet: { path: "/sharegroups" },
  shareSet: { path: "/sharegroups", method: "POST" },
  sharePostGet: { path: "/sharepost" },
  sharePostSet: { path: "/sharepost", method: "POST" },
  autopostSet: { path: "/autopost", method: "POST" },
  commentGet: { path: "/comment" },
  commentSet: { path: "/comment", method: "POST" },
  groupscanGet: { path: "/groupscan" },
  groupscanSet: { path: "/groupscan", method: "POST" },
  logGet: { path: "/log" },
  logClear: { path: "/log", method: "POST" },
};
chrome.runtime.onMessage.addListener((msg, _s, send) => {
  if (!msg || !msg.type) return;
  if (msg.type === "status") {
    cfg()
      .then((c) => send({ running, enabled: c.enabled, server: c.server, hasToken: !!c.token, dom: autoStatsCache }))
      .catch((e) => send({ running, error: String(e) }));
    return true;
  }
  if (msg.type === "start") {
    loop();
    send({ ok: true });
    return true;
  }
  if (msg.type === "setEnabled") {
    (async () => {
      try {
        const on = !!msg.value;
        await chrome.storage.local.set({ enabled: on });
        if (!on) {
          if (pollCtl) {
            try {
              pollCtl.abort();
            } catch (e) {}
          }
          try {
            await chrome.alarms.clear("cxt-poll");
          } catch (e) {}
        } else {
          try {
            chrome.alarms.create("cxt-poll", { periodInMinutes: 0.5 });
          } catch (e) {}
        }
        const c = await cfg();
        await applyBadge(c.enabled);
        await reportState(c);
        if (c.enabled) loop();
        send({ ok: true, enabled: c.enabled });
      } catch (e) {
        send({ ok: false, error: String(e) });
      }
    })();
    return true;
  }
  if (msg.type === "autoQueue") {
    (async () => {
      try {
        const c = await cfg();
        send(await daemonJson(c.server, "/autopost", c.token, msg.value || {}));
      } catch (e) {
        send({ ok: false, error: "daemon no disponible" });
      }
    })();
    return true;
  }
  if (msg.type === "autopostState") {
    (async () => {
      try {
        const c = await cfg();
        send(await daemonJson(c.server, "/autopost", c.token));
      } catch (e) {
        send({ ok: false });
      }
    })();
    return true;
  }
  if (msg.type === "autoTab") {
    (async () => {
      try {
        const t = await getStoredAutoTab();
        const id = _s && _s.tab ? _s.tab.id : null;
        send({ auto: !!(t && id != null && t.tabId === id) });
      } catch (e) {
        send({ auto: false });
      }
    })();
    return true;
  }
  if (msg.type === "autoStats") {
    try {
      const v = msg.value && typeof msg.value === "object" ? msg.value : {};
      autoStatsCache = { tagged: +v.tagged || 0, errors: +v.errors || 0 };
      const p = chrome.storage.local.set({ autoStats: autoStatsCache });
      if (p && p.catch) p.catch((e) => console.error(e));
    } catch (e) {
      console.error(e);
    }
    send({ ok: true });
    return true;
  }
  if (msg.type === "runOp") {
    const reply = send;
    // El popup abierto como pestaña (chrome-extension://<id>/popup.html) llega
    // con sender.tab y antes se rechazaba: depurarlo asi era imposible (F3).
    // Solo se abre la puerta a esa pagina de la propia extension; los content
    // scripts de webs siguen bloqueados igual que antes.
    const ownPopup = !!(_s && _s.tab && typeof _s.url === "string" &&
      _s.url.indexOf(chrome.runtime.getURL("popup.html")) === 0);
    if (_s && _s.tab && !ownPopup) {
      reply({ ok: false, error: "runOp: origen no permitido" });
      return true;
    }
    const v = msg.value && typeof msg.value === "object" ? msg.value : {};
    const op = v.op;
    const args = v.args && typeof v.args === "object" ? v.args : {};
    const allowed = ["scrape.page", "scrape.markdown", "scrape.tables", "scrape.links", "cdp", "navigate", "tabs"];
    if (allowed.indexOf(op) < 0) {
      reply({ ok: false, error: "runOp: op no permitida" });
      return true;
    }
    if (op === "cdp" && args.method !== "Page.printToPDF") {
      reply({ ok: false, error: "runOp: cdp no permitido" });
      return true;
    }
    if (runOpBusy) {
      reply({ ok: false, error: "runOp: ocupado" });
      return true;
    }
    runOpBusy = true;
    (async () => {
      const at = () => {
        const d = new Date();
        return [d.getHours(), d.getMinutes(), d.getSeconds()].map((x) => String(x).padStart(2, "0")).join(":");
      };
      let resp;
      try {
        const result = await exec(op, args);
        const bad = result && (result.ok === false || typeof result.error === "string");
        resp = bad ? { ok: false, error: (result && result.error) || "runOp: error" } : { ok: true, data: result };
      } catch (e) {
        resp = { ok: false, error: "runOp: " + String(e) };
      }
      if (op === "cdp" && resp.ok === true && (((resp.data && resp.data.result && resp.data.result.data) || "").length > 12000000)) {
        resp = { ok: false, error: "PDF_TOO_BIG" };
      }
      if (op.startsWith("scrape.")) {
        const now = at();
        let chars = 0;
        if (resp.ok === true) {
          const d = resp.data || {};
          chars = typeof d.chars === "number" ? d.chars : JSON.stringify(d).length;
        }
        const errMsg = resp.ok === true ? "" : String(resp.error || "runOp: error").slice(0, 200);
        const registro = resp.ok === true ? { op, at: now, ok: true, chars } : { op, at: now, ok: false, error: errMsg };
        let log = [];
        try {
          const st = await chrome.storage.local.get("scrapingLog");
          if (st && Array.isArray(st.scrapingLog)) log = st.scrapingLog;
        } catch (e) {}
        const linea = now + " · " + op + (resp.ok === true ? " · OK " + chars + " chars" : " · ERROR " + errMsg);
        log.push(linea.slice(0, 200));
        if (log.length > 200) log = log.slice(log.length - 200);
        const size = JSON.stringify(registro).length;
        if (size > 1000000) {
          resp = { ok: false, error: "TOO_BIG_STORAGE", chars: size };
        } else {
          try {
            await chrome.storage.local.set({ scrapingLog: log, scrapingResult: registro });
          } catch (e) {
            resp = { ok: false, error: "STORAGE: " + e };
          }
        }
      } else if (op === "cdp") {
        const now = at();
        const bytes = resp.ok === true ? (((resp.data && resp.data.result && resp.data.result.data) || "").length) : 0;
        const registro = resp.ok === true ? { op, at: now, ok: true, bytes } : { op, at: now, ok: false, error: String(resp.error || "").slice(0, 200) };
        try {
          await chrome.storage.local.set({ scrapingResult: registro });
        } catch (e) {
          resp = { ok: false, error: "STORAGE: " + e };
        }
      }
      send(resp);
      runOpBusy = false;
    })();
    return true;
  }
  const ep = API[msg.type];
  if (!ep) return;
  (async () => {
    try {
      const c = await cfg();
      const qs = [];
      if (c.token) qs.push("token=" + encodeURIComponent(c.token));
      if (msg.query) for (const k of Object.keys(msg.query)) qs.push(encodeURIComponent(k) + "=" + encodeURIComponent(msg.query[k]));
      const q = qs.length ? "?" + qs.join("&") : "";
      const opts = ep.method
        ? { method: ep.method, headers: { "Content-Type": "application/json" }, body: JSON.stringify(msg.value || {}) }
        : { cache: "no-store" };
      const r = await fetch(c.server + ep.path + q, opts);
      send(await r.json());
    } catch (e) {
      send({ ok: false, error: String(e) });
    }
  })();
  return true;
});
init().catch(console.error);
