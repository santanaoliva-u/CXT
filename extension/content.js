// Content script de la pestaña "Automatización" de CXT.
// Corre en el mundo AISLADO (no MAIN): evita el CSP de Facebook.
if (!window.__cxtAuto) {
  window.__cxtAuto = 1;
  console.log("CXT automatizar listo");

  (function () {
    "use strict";

    // Misma constante que share_post.py:85-88 (regex del permalink de una publicación de FB).
    var PERMALINK_RE = /\/posts\/|\/permalink|\/share\/p\/|\/photo|\/videos\//;
    // Control Compartir. Facebook no siempre usa el texto corto: en el build medido el
    // aria-label real era "Share, send this to friends or post it on your profile."
    // (el equivalente español viene largo igual). Por eso se casa por PREFIJO con límite
    // de palabra, y no por igualdad exacta.
    var SHARE_LABEL_RE = /^(share|compartir|comparte|share this|comparte esto)\b/i;
    // Etiquetas del menú nativo (share_post.py:112-122). NO son el control de compartir
    // aunque empiecen igual: se descartan a mano en isShareLabel().
    var MENU_LABELS = ["compartir en un grupo", "share to a group", "compartir ahora", "share now"];
    var TRACK_PARAMS = ["__cft__", "__tn__", "comment_id", "reply_comment_id"];

    var stats = { tagged: 0, errors: 0 };
    var skipped = new WeakSet();   // role=article descartados (p. ej. comentarios)
    var registry = new Map();      // contenedor -> registro del control inyectado
    var shadowRoots = new Set();
    var selected = new Set();      // permalinks marcados (sobrevive al scroll)
    var selectedData = new Map();  // permalink -> {url, expect}
    var queueState = null;
    var seq = 0;
    var lastStats = "";

    function norm(s) {
      s = (s || "").trim().toLowerCase();
      try { s = s.normalize("NFD").replace(/[\u0300-\u036f]/g, ""); } catch (e) {}
      return s;
    }

    // Chatarra de FB que no identifica un post (logo, chrome del visor de fotos).
    var EXPECT_NOISE_RE = /^(facebook|meta|instagram|whatsapp|me gusta|like|comentar|comment|compartir|share|ver mas|see more|publicidad|advertisement|reels|video|fotos|photos|siguiente|anterior|siguiente foto|previous|next)$/i;

    function expectFrom(container) {
      var raw = (container && container.innerText) || "";
      var lines = raw.split(/\n+/);
      var picked = [];
      var seen = {};
      for (var i = 0; i < lines.length && picked.length < 3; i++) {
        var line = (lines[i] || "").replace(/\s+/g, " ").trim();
        if (line.length < 3) continue;
        var n = norm(line);
        if (EXPECT_NOISE_RE.test(n)) continue;
        if (seen[n]) continue;
        seen[n] = true;
        picked.push(line);
      }
      if (!picked.length) return "";
      return picked.join(" ").slice(0, 120);
    }

    function visible(el) {
      try {
        if (el.checkVisibility) return el.checkVisibility({ checkOpacity: true, checkVisibilityCSS: true });
        var r = el.getBoundingClientRect();
        return r.width > 4 && r.height > 4;
      } catch (e) { return false; }
    }

    function request(type, value) {
      return new Promise(function (res) {
        try {
          chrome.runtime.sendMessage({ type: type, value: value }, function (r) {
            if (chrome.runtime.lastError) { res(null); return; }
            res(r || null);
          });
        } catch (e) { res(null); }
      });
    }

    function isShareLabel(a) {
      var t = (a || "").trim();
      if (!SHARE_LABEL_RE.test(t)) return false;
      if (MENU_LABELS.indexOf(norm(t)) >= 0) return false;
      if (/^(share|compartir)\s+(settings|options|permissions|ajustes|opciones|preferencias)\b/i.test(t)) return false;
      return true;
    }

    // Un comentario/respuesta de FB también es div[role=article] y su timestamp es un
    // link al permalink (con comment_id). Su aria-label propio lo delata.
    function looksLikeComment(el) {
      var lbl = "";
      try { lbl = ((el.getAttribute && el.getAttribute("aria-label")) || "").trim(); } catch (e) {}
      return /^(comentario|comment|respuesta|reply)\b/i.test(lbl);
    }

    // --- extracción por publicación ---
    function findShareControl(container) {
      var cands = container.querySelectorAll("[aria-label]");
      for (var i = 0; i < cands.length; i++) {
        var el = cands[i];
        if (isShareLabel(el.getAttribute("aria-label") || "") && visible(el)) {
          return el.closest("div[role=button],a[role=button],button") || el;
        }
      }
      return null;
    }

    function extractPermalink(container) {
      var anchors = container.querySelectorAll("a[href]");
      for (var i = 0; i < anchors.length; i++) {
        var href = anchors[i].getAttribute("href") || "";
        if (!PERMALINK_RE.test(href)) continue;
        // El timestamp de un COMENTARIO apunta al permalink del post pero con
        // comment_id/reply_comment_id: no es link propio del comentario.
        if (/[?&](comment_id|reply_comment_id)=/.test(href)) continue;
        try {
          var u = new URL(href, location.origin);
          for (var k = 0; k < TRACK_PARAMS.length; k++) u.searchParams.delete(TRACK_PARAMS[k]);
          return u.href;
        } catch (e) {}
      }
      return null;
    }

    function menuPresent() {
      var els = document.querySelectorAll("[aria-label],[role=button]");
      for (var i = 0; i < els.length; i++) {
        var t = norm(els[i].getAttribute("aria-label") || els[i].textContent || "");
        if (MENU_LABELS.indexOf(t) >= 0) return true;
      }
      return false;
    }

    function hasNativeMenu(container) {
      return !!findShareControl(container) || menuPresent();
    }

    // --- inyección del control (checkbox + botón) ---
    var CONTROL_CSS = [
      ":host{all:initial;}",
      ".cxt-wrap{display:inline-flex;align-items:center;gap:6px;font:600 12px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#606770;}",
      ".cxt-chk{margin:0;width:14px;height:14px;cursor:pointer;accent-color:#1877f2;}",
      ".cxt-chk:disabled{cursor:not-allowed;}",
      ".cxt-btn{font:600 12px/1.2 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;color:#fff;background:#1877f2;border:0;border-radius:6px;padding:4px 8px;cursor:pointer;}",
      ".cxt-btn:hover{background:#166fe5;}",
      ".cxt-btn:disabled{background:#ccd0d5;cursor:not-allowed;}"
    ].join("");

    function buildControlHost() {
      var host = document.createElement("span");
      // Marcador visible para probes F3 (el probe cuenta [data-cxt-auto-ui=control]).
      host.setAttribute("data-cxt-auto-ui", "control");
      host.style.cssText = "display:inline-flex;align-items:center;vertical-align:middle;margin-left:8px;";
      var sr = host.attachShadow({ mode: "open" });
      sr.innerHTML = "<style>" + CONTROL_CSS + "</style>" +
        "<label class=\"cxt-wrap\"><input type=\"checkbox\" class=\"cxt-chk\">" +
        "<button type=\"button\" class=\"cxt-btn\">Automatizar</button></label>";
      return host;
    }

    function wireControl(rec) {
      var sr = rec.host.shadowRoot;
      var chk = sr.querySelector(".cxt-chk");
      var btn = sr.querySelector(".cxt-btn");

      if (!rec.permalink) {
        chk.disabled = true;
        chk.title = "Este post no tiene link propio";
      } else {
        chk.checked = selected.has(rec.permalink);
      }
      chk.addEventListener("change", function () {
        if (chk.checked && rec.permalink) {
          selected.add(rec.permalink);
          selectedData.set(rec.permalink, { url: rec.permalink, expect: rec.expect });
        } else if (rec.permalink) {
          selected.delete(rec.permalink);
          selectedData.delete(rec.permalink);
        }
        renderBar();
      });

      if (!rec.permalink && !rec.hasShare) {
        btn.disabled = true;
        btn.title = "Este post no se puede automatizar";
      } else {
        btn.addEventListener("click", function () { onAutomatizar(rec); });
      }
    }

    // ¿Es un esqueleto de carga de FB? Se reconoce porque el texto casi vacío y trae el
    // indicador nativo de carga (data-visualcompletion=loading-state / role=status) y, a
    // diferencia de una publicación real, no trae su control de compartir.
    function isSkeleton(el) {
      if (!el || el.nodeType !== 1) return false;
      var t = "";
      try { t = (el.innerText || "").replace(/\s+/g, " ").trim().toLowerCase(); } catch (e) {}
      if (t.length > 60) return false;
      if (countShareControls(el) > 0) return false;
      try {
        if (el.querySelector('[data-visualcompletion="loading-state"],[role="status"][aria-label]')) return true;
      } catch (e) {}
      var lbl = (el.getAttribute("aria-label") || "").toLowerCase();
      return lbl === "cargando..." || lbl === "loading...";
    }

    // Cuenta los controles nativos de compartir; corta en 2 (solo interesa si hay >1).
    function countShareControls(root) {
      var n = 0;
      try {
        var els = root.querySelectorAll("[aria-label]");
        for (var i = 0; i < els.length; i++) {
          if (isShareLabel(els[i].getAttribute("aria-label") || "")) {
            if (++n > 1) break;
          }
        }
      } catch (e) {}
      return n;
    }

    function heightOf(node) {
      try { return node.getBoundingClientRect().height || node.offsetHeight || 0; } catch (e) { return 0; }
    }

    // Contenedor de la publicación dueña de `el` (camino inverso al de la versión anterior:
    // antes se partía del role=article, ahora se parte del control de compartir).
    // 1) el role=article más cercano si no es esqueleto
    // 2) si no, sube por los ancestros hasta el más bajo que tenga enlace propio o altura de
    //    tarjeta; nunca body/html, máximo 14 niveles (la tarjeta del post en la vista de
    //    permalink queda más profunda que en el feed), y se corta al salir de la publicación
    //    (más de un control de compartir en el nodo = ya se pasó).
    function containerFor(el) {
      if (!el || el.nodeType !== 1) return null;
      var art = null;
      try { art = el.closest("div[role=article]"); } catch (e) {}
      if (art && !isSkeleton(art)) return art;

      var node = el.parentElement, fb = null;
      for (var up = 0; node && up < 14; up++) {
        if (node.nodeType !== 1) break;
        if (node === document.body || node === document.documentElement) break;
        if (countShareControls(node) > 1) break;
        if (!isSkeleton(node)) {
          if (extractPermalink(node)) return node;
          if (!fb && heightOf(node) >= 150) fb = node;
        }
        node = node.parentElement;
      }
      return fb;
    }

    function injectControl(container) {
      var shareEl = findShareControl(container);
      var permalink = extractPermalink(container);
      if (!shareEl && !permalink) return false;

      var expect = expectFrom(container);
      var id = "cxt" + (++seq);
      var host = buildControlHost();
      var rec = { id: id, host: host, permalink: permalink, expect: expect, hasShare: hasNativeMenu(container), container: container };

      try {
        container.setAttribute("data-cxt-auto", id);
        if (shareEl && shareEl.parentElement) shareEl.parentElement.insertBefore(host, shareEl.nextSibling);
        else container.appendChild(host);
      } catch (e) { stats.errors++; return false; }

      registry.set(container, rec);
      wireControl(rec);
      stats.tagged++;
      return true;
    }

    function ensureControl(container) {
      var rec = registry.get(container);
      if (rec) {
        if (document.contains(rec.host)) return; // control vivo: nada que hacer
        try { rec.host.remove(); } catch (e) {}
        registry.delete(container);
        try { container.removeAttribute("data-cxt-auto"); } catch (e) {}
      } else if (skipped.has(container)) {
        return; // ya evaluado y descartado (no es publicación)
      }

      try {
        // No cacheamos el descarte automático: FB puede renderizar el control de compartir
        // o el permalink DESPUÉS (tarjeta con texto pero sin share visible todavía). Solo
        // se cachea vía skipped.add explícito (comentarios, en scan()).
        injectControl(container);
      } catch (e) { stats.errors++; }
    }

    // Libera los contenedores retirados por FB para no dejar nodos zombis.
    function releaseTree(node) {
      if (!node || node.nodeType !== 1) return;
      var list = [];
      try { if (node.matches("[data-cxt-auto]")) list.push(node); } catch (e) {}
      try { node.querySelectorAll && node.querySelectorAll("[data-cxt-auto]").forEach(function (el) { list.push(el); }); } catch (e) {}
      for (var i = 0; i < list.length; i++) {
        var el = list[i];
        var rec = registry.get(el);
        if (rec) { try { rec.host.remove(); } catch (e) {} registry.delete(el); }
        try { el.removeAttribute("data-cxt-auto"); } catch (e) {}
      }
    }

    function collectShadow(node) {
      if (!node || node.nodeType !== 1) return;
      var budget = 600;
      try {
        if (node.shadowRoot) shadowRoots.add(node.shadowRoot);
        var els = node.querySelectorAll("*");
        for (var i = 0; i < els.length && budget > 0; i++) {
          if (els[i].shadowRoot) shadowRoots.add(els[i].shadowRoot);
          budget--;
        }
      } catch (e) {}
    }

    function scan() {
      var roots = [document].concat(Array.prototype.slice.call(shadowRoots));
      var seen = new Set();
      for (var r = 0; r < roots.length; r++) {
        var root = roots[r];
        var arts;
        try { arts = root.querySelectorAll("div[role=article]"); } catch (e) { arts = []; }
        for (var i = 0; i < arts.length; i++) {
          if (seen.has(arts[i])) continue;
          seen.add(arts[i]);
          if (isSkeleton(arts[i])) continue;
          if (looksLikeComment(arts[i])) { skipped.add(arts[i]); continue; }
          ensureControl(arts[i]);
        }

        var shares;
        try { shares = root.querySelectorAll("[aria-label]"); } catch (e) { shares = []; }
        var left = 400;
        for (var j = 0; j < shares.length && left > 0; j++) {
          var s = shares[j];
          if (!isShareLabel(s.getAttribute("aria-label") || "")) continue;
          if (!visible(s)) continue;
          left--;
          var box = containerFor(s);
          if (!box || seen.has(box)) continue;
          seen.add(box);
          if (looksLikeComment(box)) { skipped.add(box); continue; }
          ensureControl(box);
        }
      }
      publishStats();
      renderBar();
    }

    var timer = 0;
    function schedule() {
      if (timer) return;
      timer = setTimeout(function () { timer = 0; scan(); }, 300);
    }

    function publishStats() {
      var j = JSON.stringify(stats);
      if (j === lastStats) return;
      lastStats = j;
      request("autoStats", { tagged: stats.tagged, errors: stats.errors });
    }

    // --- acción Automatizar ---
    function collectTargets() {
      if (selected.size) {
        var out = [];
        selectedData.forEach(function (d) { out.push({ url: d.url, expect: d.expect }); });
        return out;
      }
      return [];
    }

    function openNativeShare(rec) {
      var el = findShareControl(rec.container);
      if (!el) return;
      try { el.scrollIntoView({ block: "center" }); } catch (e) {}
      el.click();
    }

    function onAutomatizar(rec) {
      if (!selected.size && !rec.permalink) { openNativeShare(rec); return; }
      var items = selected.size ? collectTargets() : [{ url: rec.permalink, expect: rec.expect }];
      if (!items.length) return;
      openModal(items);
    }

    // --- barra flotante de selección + contador de cola ---
    var BAR_CSS = [
      ":host{all:initial;}",
      ".cxt-bar{position:fixed;right:16px;bottom:16px;z-index:2147483000;background:#fff;color:#1c1e21;border:1px solid #ced0d4;border-radius:10px;box-shadow:0 2px 12px rgba(0,0,0,.2);padding:10px 12px;font:600 13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-width:200px;}",
      ".cxt-sel{display:block;margin-bottom:6px;}",
      ".cxt-queue{display:block;font-weight:400;color:#606770;margin-bottom:6px;white-space:pre-line;}",
      ".cxt-acts{display:flex;gap:6px;}",
      ".cxt-bar button{font:600 12px/1.2 inherit;border:0;border-radius:6px;padding:5px 10px;cursor:pointer;}",
      ".cxt-go{color:#fff;background:#1877f2;}",
      ".cxt-go:hover{background:#166fe5;}",
      ".cxt-clr{color:#1c1e21;background:#e4e6eb;}",
      ".cxt-clr:hover{background:#d8dadf;}"
    ].join("");

    var bar = null;
    function ensureBar() {
      if (bar) return bar;
      var host = document.createElement("div");
      host.setAttribute("data-cxt-auto-ui", "bar");
      var sr = host.attachShadow({ mode: "open" });
      sr.innerHTML = "<style>" + BAR_CSS + "</style>" +
        "<div class=\"cxt-bar\"><span class=\"cxt-sel\">0 seleccionadas</span>" +
        "<span class=\"cxt-queue\"></span>" +
        "<div class=\"cxt-acts\"><button type=\"button\" class=\"cxt-go\">Automatizar</button>" +
        "<button type=\"button\" class=\"cxt-clr\">Limpiar</button></div></div>";
      sr.querySelector(".cxt-go").addEventListener("click", function () {
        if (selected.size) openModal(collectTargets());
      });
      sr.querySelector(".cxt-clr").addEventListener("click", function () {
        selected.clear();
        selectedData.clear();
        syncCheckboxes();
        renderBar();
      });
      document.documentElement.appendChild(host);
      bar = host;
      return bar;
    }

    function syncCheckboxes() {
      registry.forEach(function (rec) {
        var sr = rec.host.shadowRoot;
        if (!sr || !rec.permalink) return;
        var chk = sr.querySelector(".cxt-chk");
        if (chk) chk.checked = selected.has(rec.permalink);
      });
    }

    function fmtTime(v) {
      if (typeof v === "number") {
        try { return new Date(v < 1e12 ? v * 1000 : v).toLocaleTimeString(); } catch (e) { return String(v); }
      }
      return String(v);
    }

    function renderBar() {
      var host = ensureBar();
      var sr = host.shadowRoot;
      var n = selected.size;
      sr.querySelector(".cxt-sel").textContent = n + (n === 1 ? " seleccionada" : " seleccionadas");

      var q = queueState && (queueState.value || queueState);
      var qEl = sr.querySelector(".cxt-queue");
      if (q && (q.pending != null || q.current != null || q.next_at != null)) {
        var parts = [];
        if (q.pending != null) parts.push(q.pending + " pendientes");
        if (q.current) parts.push("actual: " + q.current);
        if (q.next_at) parts.push("próximo: " + fmtTime(q.next_at));
        qEl.textContent = parts.join(" · ");
      } else {
        qEl.textContent = "";
      }
      host.style.display = (n > 0 || qEl.textContent) ? "block" : "none";
    }

    function pollQueue() {
      request("autopostState").then(function (r) {
        queueState = r;
        renderBar();
      });
    }

    // --- modal (shadow DOM) ---
    var MODAL_CSS = [
      ":host{all:initial;}",
      ".cxt-back{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.45);display:flex;align-items:center;justify-content:center;}",
      ".cxt-modal{width:340px;max-width:92vw;background:#fff;color:#1c1e21;border-radius:12px;box-shadow:0 8px 32px rgba(0,0,0,.35);padding:16px;font:13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}",
      "h3{margin:0 0 10px;font-size:15px;}",
      "label{display:block;margin:8px 0 2px;font-weight:600;color:#606770;}",
      "input[type=text],input[type=number]{width:100%;box-sizing:border-box;padding:7px 8px;border:1px solid #ced0d4;border-radius:6px;font:13px inherit;}",
      ".cxt-hint{color:#8a8d91;font-weight:400;margin-top:2px;}",
      ".cxt-chkrow{display:flex;align-items:center;gap:6px;font-weight:400;margin-top:10px;}",
      ".cxt-count{margin-top:10px;font-weight:600;}",
      ".cxt-err{color:#d93025;font-weight:600;margin-top:6px;min-height:16px;}",
      ".cxt-warn{color:#8a6d00;background:#fff8e1;border-radius:6px;padding:6px 8px;margin-top:8px;font-size:12px;}",
      ".cxt-btns{display:flex;justify-content:flex-end;gap:8px;margin-top:14px;}",
      ".cxt-btns button{font:600 13px inherit;border:0;border-radius:6px;padding:7px 14px;cursor:pointer;}",
      ".cxt-cancel{color:#1c1e21;background:#e4e6eb;}",
      ".cxt-cancel:hover{background:#d8dadf;}",
      ".cxt-enqueue{color:#fff;background:#1877f2;}",
      ".cxt-enqueue:hover{background:#166fe5;}"
    ].join("");

    var modal = null;
    var modalTargets = [];

    function ensureModal() {
      if (modal) return modal;
      var host = document.createElement("div");
      host.setAttribute("data-cxt-auto-ui", "modal");
      var sr = host.attachShadow({ mode: "open" });
      sr.innerHTML = "<style>" + MODAL_CSS + "</style>" +
        "<div class=\"cxt-back\"><div class=\"cxt-modal\" role=\"dialog\" aria-modal=\"true\">" +
        "<h3>Automatizar publicación</h3>" +
        "<label>Grupos (texto)</label><input type=\"text\" class=\"f-filter\" placeholder=\"playa del carmen\">" +
        "<label>Límite</label><input type=\"number\" min=\"0\" value=\"10\" class=\"f-limit\">" +
        "<label>Texto extra (opcional)</label><input type=\"text\" class=\"f-text\">" +
        "<label>Intervalo (min)</label><input type=\"number\" min=\"1\" value=\"15\" class=\"f-interval\">" +
        "<div class=\"cxt-hint\">recomendado 15 · máximo 60</div>" +
        "<label class=\"cxt-chkrow\"><input type=\"checkbox\" checked class=\"f-dry\">Simulación (no publica)</label>" +
        "<div class=\"cxt-count\">Publicaciones seleccionadas: <b class=\"f-count\">0</b></div>" +
        "<div class=\"cxt-err f-err\"></div>" +
        "<div class=\"cxt-warn\">Compartir en grupos de forma automatizada puede limitar tu cuenta</div>" +
        "<div class=\"cxt-btns\"><button type=\"button\" class=\"cxt-enqueue f-enqueue\">Encolar</button>" +
        "<button type=\"button\" class=\"cxt-cancel f-cancel\">Cancelar</button></div>" +
        "</div></div>";

      sr.querySelector(".f-cancel").addEventListener("click", closeModal);
      sr.querySelector(".f-enqueue").addEventListener("click", enqueue);
      sr.querySelector(".cxt-back").addEventListener("click", function (ev) {
        if (ev.target === sr.querySelector(".cxt-back")) closeModal();
      });
      host.style.display = "none";
      document.documentElement.appendChild(host);
      modal = host;
      return modal;
    }

    function showError(text) {
      modal.shadowRoot.querySelector(".f-err").textContent = text || "";
    }

    function openModal(items) {
      var host = ensureModal();
      modalTargets = items.slice();
      host.shadowRoot.querySelector(".f-count").textContent = String(modalTargets.length);
      showError("");
      host.style.display = "block";
    }

    function closeModal() {
      if (modal) modal.style.display = "none";
    }

    function enqueue() {
      var sr = modal.shadowRoot;
      var items = modalTargets.map(function (t) { return { url: t.url, expect: t.expect }; });
      if (!items.length) { showError("sin publicaciones seleccionadas"); return; }
      if (items.some(function (it) { return !it.url; })) { showError("item sin link"); return; }

      var filter = sr.querySelector(".f-filter").value.trim();
      if (!filter) { showError("Indica los grupos (texto)"); return; }

      var value = {
        items: items,
        filter: filter,
        limit: +sr.querySelector(".f-limit").value || 0,
        text: sr.querySelector(".f-text").value,
        dry: sr.querySelector(".f-dry").checked,
        interval_min: +sr.querySelector(".f-interval").value || 0
      };
      request("autoQueue", value).then(function (r) {
        if (r && r.ok) { closeModal(); return; }
        showError((r && r.error) || "sin respuesta del background");
      });
    }

    // --- arranque ---
    collectShadow(document.documentElement);
    scan();

    var obs = new MutationObserver(function (muts) {
      for (var i = 0; i < muts.length; i++) {
        var m = muts[i];
        for (var j = 0; j < m.removedNodes.length; j++) releaseTree(m.removedNodes[j]);
        for (var k = 0; k < m.addedNodes.length; k++) collectShadow(m.addedNodes[k]);
      }
      schedule();
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });

    window.addEventListener("scroll", schedule, { passive: true });
    window.addEventListener("popstate", schedule);
    window.addEventListener("hashchange", schedule);
    window.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && modal && modal.style.display !== "none") closeModal();
    });

    pollQueue();
    setInterval(pollQueue, 5000);
  })();
}
