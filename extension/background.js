// CXT background service worker (MV3). Classic script, no imports.
const DEFAULTS = { server: "http://127.0.0.1:8799", token: "", enabled: true, maxHops: 0 };
let running = false;

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

async function cdp(tabId, method, params) {
  try {
    return await chrome.debugger.sendCommand({ tabId }, method, params || {});
  } catch (e) {
    try {
      await chrome.debugger.attach({ tabId }, "1.3");
    } catch (e2) {}
    return await chrome.debugger.sendCommand({ tabId }, method, params || {});
  }
}

async function loop() {
  if (running) return;
  running = true;
  while (true) {
    const c = await cfg();
    if (!c.enabled) {
      await reportState(c);
      running = false;
      return;
    }
    let cmd = null;
    try {
      const res = await fetch(qs(c.server, c.token), { cache: "no-store" });
      if (res.status === 204) continue;
      if (!res.ok) {
        await sleep(1000);
        continue;
      }
      cmd = await res.json();
    } catch (e) {
      await sleep(1000);
      continue;
    }
    let out;
    try {
      out = { id: cmd.id, ok: true, data: await exec(cmd.op, cmd.args || {}) };
    } catch (e) {
      out = { id: cmd.id, ok: false, error: String(e && e.message ? e.message : e) };
    }
    await chrome.storage.local.set({
      last: { op: cmd.op, ok: out.ok, error: out.error || "", at: Date.now() },
    });
    try {
      await post(c.server, "/result", c.token, out);
    } catch (e) {}
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
      const t = await chrome.tabs.update(args.tabId, { active: true });
      if (t.windowId != null) await chrome.windows.update(t.windowId, { focused: true });
      return { id: t.id, url: t.url };
    }
    case "tab.close":
      await chrome.tabs.remove(args.tabId);
      return { closed: args.tabId };
    case "navigate": {
      const t = await resolveTab(args);
      await chrome.tabs.update(t.id, { url: args.url });
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
      const t = await resolveTab(args);
      await chrome.tabs.update(t.id, { active: true });
      if (t.windowId != null) await chrome.windows.update(t.windowId, { focused: true });
      await sleep(150);
      const dataUrl = await chrome.tabs.captureVisibleTab(t.windowId, { format: args.format || "png" });
      return { dataUrl };
    }
    case "cdp":
      return { result: await cdp((await resolveTab(args)).id, args.method, args.params) };
    case "cdpclick": {
      const t = await resolveTab(args);
      const x = args.x;
      const y = args.y;
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
      await sleep(60);
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
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
      const code = key === "Enter" ? 13 : key === "Tab" ? 9 : key === "Escape" ? 27 : 0;
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
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
      await sleep(60);
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
      return { clicked: true, x, y, tag: p.tag, text: p.text, match_level: p.match || null, click_method: "cdp" };
    }
    case "cdpclickref": {
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
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mouseMoved", x, y, button: "none" });
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mousePressed", x, y, button: "left", clickCount: 1 });
      await sleep(60);
      await cdp(t.id, "Input.dispatchMouseEvent", { type: "mouseReleased", x, y, button: "left", clickCount: 1 });
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
      const cur = new Map();
      for (const it of items) cur.set(it.role + "|" + it.name, it);
      const prev = snapState.get(t.id);
      if (args.delta && prev) {
        const lines = [];
        let added = 0;
        let removed = 0;
        let changed = 0;
        for (const [k, it] of cur) {
          const p = prev.get(k);
          if (!p) {
            added++;
            lines.push("+ " + snapLine(it));
          } else if (p.sig !== it.sig) {
            changed++;
            lines.push("~ " + snapLine(it));
          }
        }
        for (const [k, p] of prev) {
          if (!cur.has(k)) {
            removed++;
            lines.push('- ' + p.role + ' "' + p.name + '"');
          }
        }
        snapState.set(t.id, cur);
        const cap = args.maxChars || 6000;
        let body = lines.join("\n");
        if (body.length > cap) body = body.slice(0, cap) + "\n...[truncado]";
        return { mode: "delta", added, removed, changed, text: body || "(sin cambios)" };
      }
      snapState.set(t.id, cur);
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
    case "batch":
    case "macro": {
      const t = await resolveTab(args);
      let steps = args.steps || null;
      if (op === "macro" || args.name) {
        const m = MACROS[args.name];
        if (!m) return { ok: false, error: "macro desconocida: " + (args.name || "") };
        steps = m;
      }
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

const snapState = new Map();

const MACROS = {
  "fb.post": [
    { op: "cdpclicktext", args: { text: "pensando", selector: "*", minW: 150, minH: 25, maxH: 130 }, require: true },
    { op: "wait", args: { ms: 1800 } },
    { op: "probe", args: { selector: "[contenteditable='true']" }, require: true },
    { op: "cdptype", args: { value: "$TEXT" }, require: true },
    { op: "wait", args: { ms: 500 } },
    { op: "cdpclicktext", args: { text: "siguiente", minW: 60 } },
    { op: "wait", args: { ms: 2500 } },
    { op: "cdpclicktext", args: { text: "publicar", minW: 120 } },
    { op: "wait", args: { ms: 2500 } },
    { op: "probe", args: { text: "$TEXT" } },
  ],
};

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
  snapshot: ["mode", "total", "kept", "added", "removed", "changed"],
  read: ["url", "title", "chars"],
  eval: ["value", "error"],
  macro: ["ok", "steps"],
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
  const visible = (el) => {
    if (!el || !el.getClientRects().length) return false;
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && s.opacity !== "0";
  };
  const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const deepRoots = () => {
    const roots = [document];
    const seen = new Set(roots);
    const visit = (root) => {
      let els = [];
      try {
        els = Array.from(root.querySelectorAll("*"));
      } catch (e) {
        return;
      }
      for (const el of els) {
        if (el.tagName === "IFRAME") {
          let d = null;
          try {
            d = el.contentDocument;
          } catch (e) {}
          if (d && !seen.has(d)) {
            seen.add(d);
            roots.push(d);
            visit(d);
          }
        }
        if (el.shadowRoot && !seen.has(el.shadowRoot)) {
          seen.add(el.shadowRoot);
          roots.push(el.shadowRoot);
          visit(el.shadowRoot);
        }
      }
    };
    visit(document);
    return roots;
  };
  const deepAll = (sel) => {
    const out = [];
    for (const r of deepRoots()) {
      try {
        out.push(...Array.from(r.querySelectorAll(sel || "*")));
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
    const code = key === "Enter" ? 13 : key === "Tab" ? 9 : key === "Escape" ? 27 : 0;
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
      const els = Array.from(document.querySelectorAll(args.selector || "*"));
      const lim = args.limit || 25;
      const items = [];
      els.forEach((el, i) => {
        if (items.length >= lim) return;
        const r = el.getBoundingClientRect();
        if (args.visibleOnly && !visible(el)) return;
        items.push({
          i,
          tag: el.tagName,
          role: el.getAttribute("role"),
          aria: el.getAttribute("aria-label"),
          ph: el.getAttribute("placeholder"),
          ce: el.isContentEditable || el.getAttribute("contenteditable") || null,
          vis: visible(el),
          w: Math.round(r.width),
          h: Math.round(r.height),
          x: Math.round(r.left),
          y: Math.round(r.top),
          text: norm(el.innerText || el.value || el.getAttribute("aria-label")).slice(0, 60),
        });
      });
      return { count: els.length, items };
    }
    case "deepfind": {
      const t = norm(args.text || "");
      const els = deepAll(args.selector || "*");
      const lim = args.limit || 25;
      const items = [];
      els.forEach((el, i) => {
        if (items.length >= lim) return;
        const s = norm(el.innerText || el.value || el.getAttribute("aria-label") || el.textContent || "");
        if (t && (args.exact ? s !== t : !s.includes(t))) return;
        const r = el.getBoundingClientRect();
        if (args.visibleOnly && !visible(el)) return;
        items.push({
          i,
          tag: el.tagName,
          role: el.getAttribute("role"),
          aria: el.getAttribute("aria-label"),
          vis: visible(el),
          w: Math.round(r.width),
          h: Math.round(r.height),
          x: Math.round(r.left),
          y: Math.round(r.top),
          text: s.slice(0, 60),
          match: t ? (s === t ? "exact" : s.includes(t) ? "stable" : null) : null,
        });
      });
      return { count: els.length, items };
    }
    case "snapshot": {
      const els = deepAll(
        args.selector ||
          "a,button,input,textarea,select,[role=button],[role=link],[role=textbox],[role=menuitem],[role=checkbox],[contenteditable='true'],[aria-label]"
      );
      const lim = args.limit || 120;
      const items = [];
      let n = 0;
      for (const el of els) {
        if (items.length >= lim) break;
        if (!visible(el)) continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        const name = norm(el.getAttribute("aria-label") || el.getAttribute("placeholder") || el.value || el.innerText || el.textContent || "").slice(0, 50);
        const ref = "e" + n++;
        try {
          el.setAttribute("data-cxt-ref", ref);
        } catch (e) {}
        items.push({ ref, tag: el.tagName, role: el.getAttribute("role"), name, x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) });
      }
      return { count: items.length, items };
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
      const els = deepAll(args.selector || "*");
      let n = 0;
      let first = null;
      for (const el of els) {
        if (!visible(el)) continue;
        const s = norm(el.innerText || el.value || el.getAttribute("aria-label") || el.textContent || "");
        const hit = t ? (args.exact ? s === t : s.includes(t)) : true;
        if (!hit) continue;
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

function applyBadge(on) {
  try {
    chrome.action.setBadgeText({ text: on ? "ON" : "OFF" });
    chrome.action.setBadgeBackgroundColor({ color: on ? "#2ecc71" : "#e74c3c" });
    chrome.action.setTitle({ title: on ? "CXT — ACTIVADA" : "CXT — DESACTIVADA" });
  } catch (e) {}
}

async function init() {
  const c = await cfg();
  applyBadge(c.enabled);
  await reportState(c);
  if (c.enabled) loop();
}

chrome.runtime.onInstalled.addListener(() => init());
chrome.runtime.onStartup.addListener(() => init());
chrome.alarms.create("cxt-poll", { periodInMinutes: 1 });
chrome.alarms.onAlarm.addListener((a) => {
  if (a.name === "cxt-poll") loop();
});
chrome.runtime.onMessage.addListener((msg, _s, send) => {
  if (msg && msg.type === "status") {
    cfg().then((c) =>
      send({ running, enabled: c.enabled, server: c.server, hasToken: !!c.token })
    );
    return true;
  }
  if (msg && msg.type === "start") {
    loop();
    send({ ok: true });
    return true;
  }
  if (msg && msg.type === "setEnabled") {
    (async () => {
      await chrome.storage.local.set({ enabled: !!msg.value });
      const c = await cfg();
      applyBadge(c.enabled);
      await reportState(c);
      if (c.enabled) loop();
      send({ ok: true, enabled: c.enabled });
    })();
    return true;
  }
  if (msg && msg.type === "test") {
    cfg().then(async (c) => {
      try {
        const r = await fetch(c.server + "/health" + (c.token ? "?token=" + encodeURIComponent(c.token) : ""));
        send({ ok: r.ok, status: r.status });
      } catch (e) {
        send({ ok: false, error: String(e) });
      }
    });
    return true;
  }
  if (msg && msg.type === "schedGet") {
    cfg().then(async (c) => {
      try {
        const r = await fetch(c.server + "/schedule" + (c.token ? "?token=" + encodeURIComponent(c.token) : ""), { cache: "no-store" });
        send(await r.json());
      } catch (e) {
        send({ ok: false, error: String(e) });
      }
    });
    return true;
  }
  if (msg && msg.type === "schedSet") {
    cfg().then(async (c) => {
      try {
        const r = await fetch(c.server + "/schedule" + (c.token ? "?token=" + encodeURIComponent(c.token) : ""), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(msg.value || {}),
        });
        send(await r.json());
      } catch (e) {
        send({ ok: false, error: String(e) });
      }
    });
    return true;
  }
});
init();
