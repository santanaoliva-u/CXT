// CXT background service worker (MV3). Classic script, no imports.
const DEFAULTS = { server: "http://127.0.0.1:8799", token: "", enabled: true, maxHops: 0 };
let running = false;
let runningSince = 0;
let lastStore = 0;
let pollCtl = null;
let currentLoop = null;
let autoStatsCache = null;

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
    const r = await chrome.debugger.sendCommand({ tabId }, method, params || {});
    scheduleDetach(tabId);
    return r;
  }
}

async function clickAt(tabId, x, y) {
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
  await sleep(60);
  await cdp(tabId, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
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

async function resolveTab(args) {
  if (args && args.tabId != null) return await chrome.tabs.get(args.tabId);
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
      const t = await chrome.tabs.create({ url: args.url || "about:blank", active: args.active !== false });
      if (args.wait !== false) await waitLoad(t.id, 30000);
      const nt = await chrome.tabs.get(t.id);
      return { id: nt.id, url: nt.url, title: nt.title };
    }
    case "tab.activate": {
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
