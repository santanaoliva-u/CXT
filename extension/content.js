// Content script de la pestaña "Automatización" de CXT.
// Corre en el mundo AISLADO (no MAIN): evita el CSP de Facebook.
if (!window.__cxtAuto) {
  window.__cxtAuto = 1;
  console.log("CXT automatizar listo");

  (function () {
    "use strict";

    // Misma constante que share_post.py:85-88 (regex del permalink de una publicación de FB).
    var PERMALINK_RE = /\/posts\/|\/permalink|\/share\/p\/|\/photo|\/videos\//;
    // Un enlace a la PUBLICACION entera (posts/permalink/share) lleva todas las
    // fotos adjuntas; `/photo?fbid=...` o `/videos/` apuntan a UNA sola. Se
    // prefiere el primero para que una publicación multi-foto viaje completa.
    var POST_LEVEL_RE = /\/posts\/|\/permalink|\/share\/p\//;
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
    // El content script corre en TODAS las pestañas de Facebook. Solo la pestaña
    // propia de automatización debe cambiar el título y mostrar el resumen final.
    var isAutoTab = false;
    var seq = 0;
    var lastStats = "";

    // --- título dinámico de la pestaña (plan 5.3) ---
    // Textos EXACTOS: tres puntos ASCII en TRABAJANDO, sin prefijo ni sufijo.
    var TITLE_WORKING = "IA TRABAJANDO...";
    var TITLE_PAUSED = "IA EN PAUSA";
    var TITLE_ALERT = "IA ALERTA";
    var TITLE_READY = "IA LISTO";
    // Se captura UNA vez, antes de que este controlador escriba nada: es el título
    // al que se vuelve cuando no hay automatización activa.
    var baseTitle = document.title;
    var titleState = null;        // último autopostState recibido (null = sin estado)
    var titleObserver = null;     // vigila <title>/<head> en las rutas SPA de Facebook
    var titleWriting = false;     // guard: el observer ignora el eco de nuestra escritura
    var titleLastApplied = null;  // último texto escrito por este controlador
    var titleLastLog = "";        // anti-spam del log de depuración (cada 5 s)

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

    // Tras recargar la extensión, la pestaña abierta queda huérfana: sendMessage lanza.
    var lastReqErr = "";
    function extAlive() {
      try { return !!(window.chrome && chrome.runtime && chrome.runtime.id); }
      catch (e) { return false; }
    }
    function request(type, value) {
      return new Promise(function (res) {
        if (!extAlive()) { lastReqErr = "invalidated"; res(null); return; }
        try {
          chrome.runtime.sendMessage({ type: type, value: value }, function (r) {
            if (chrome.runtime.lastError) { lastReqErr = "noresp"; res(null); return; }
            lastReqErr = "";
            res(r || null);
          });
        } catch (e) { lastReqErr = "invalidated"; res(null); }
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
      var fallback = null;
      for (var i = 0; i < anchors.length; i++) {
        var href = anchors[i].getAttribute("href") || "";
        if (!PERMALINK_RE.test(href)) continue;
        // El timestamp de un COMENTARIO apunta al permalink del post pero con
        // comment_id/reply_comment_id: no es link propio del comentario.
        if (/[?&](comment_id|reply_comment_id)=/.test(href)) continue;
        var u;
        try {
          u = new URL(href, location.origin);
          for (var k = 0; k < TRACK_PARAMS.length; k++) u.searchParams.delete(TRACK_PARAMS[k]);
        } catch (e) { continue; }
        // Gana el primer enlace al POST completo (todas las fotos). El de una
        // sola foto/vídeo queda como respaldo para no perder el caso simple.
        if (POST_LEVEL_RE.test(u.pathname)) return u.href;
        if (!fallback) fallback = u.href;
      }
      return fallback;
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
      ".cxt-btn:disabled{background:#ccd0d5;cursor:not-allowed;}",
      ".cxt-btn-fast{background:#d97706;}",
      ".cxt-btn-fast:hover{background:#b45309;}"
    ].join("");

    function buildControlHost() {
      var host = document.createElement("span");
      // Marcador visible para probes F3 (el probe cuenta [data-cxt-auto-ui=control]).
      host.setAttribute("data-cxt-auto-ui", "control");
      host.style.cssText = "display:inline-flex;align-items:center;vertical-align:middle;margin-left:8px;";
      var sr = host.attachShadow({ mode: "open" });
      sr.innerHTML = "<style>" + CONTROL_CSS + "</style>" +
        "<label class=\"cxt-wrap\"><input type=\"checkbox\" class=\"cxt-chk\">" +
        "<button type=\"button\" class=\"cxt-btn cxt-btn-auto\">Automatizar</button>" +
        "<button type=\"button\" class=\"cxt-btn cxt-btn-fast\" title=\"Compartir rápido: abre el diálogo con el modo rápido activado\">Rápido</button></label>";
      return host;
    }

    function wireControl(rec) {
      var sr = rec.host.shadowRoot;
      var chk = sr.querySelector(".cxt-chk");
      var btnAuto = sr.querySelector(".cxt-btn-auto");
      var btnFast = sr.querySelector(".cxt-btn-fast");

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
        btnAuto.disabled = true;
        btnAuto.title = "Este post no se puede automatizar";
        btnFast.disabled = true;
        btnFast.title = "Este post no se puede automatizar";
      } else {
        btnAuto.addEventListener("click", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          onAutomatizar(rec);
        });
        btnFast.addEventListener("click", function (ev) {
          ev.preventDefault();
          ev.stopPropagation();
          onAutomatizar(rec, { fast: true });
        });
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

    // El Share vive dentro de un wrapper flex-column; el action bar real es el
    // flex-row del abuelo. Sin subir ahí el control cae debajo, no al lado.
    function insertControl(host, shareEl, container) {
      if (!shareEl) {
        container.appendChild(host);
        return;
      }
      var node = shareEl;
      var parent = shareEl.parentElement;
      for (var up = 0; parent && up < 5; up++) {
        var cs = null;
        try { cs = getComputedStyle(parent); } catch (e) { break; }
        if (cs && cs.display === "flex" &&
            (cs.flexDirection === "row" || cs.flexDirection === "row-reverse")) {
          parent.insertBefore(host, node.nextSibling);
          return;
        }
        if (parent.getAttribute && parent.getAttribute("role") === "article") break;
        node = parent;
        parent = parent.parentElement;
      }
      if (shareEl.parentElement) shareEl.parentElement.insertBefore(host, shareEl.nextSibling);
      else container.appendChild(host);
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
        insertControl(host, shareEl, container);
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

    function onAutomatizar(rec, opts) {
      if (!selected.size && !rec.permalink) { openNativeShare(rec); return; }
      var items = selected.size ? collectTargets() : [{ url: rec.permalink, expect: rec.expect }];
      if (!items.length) return;
      openModal(items, opts);
    }

    // --- barra flotante de selección + contador de cola ---
    var BAR_CSS = [
      ":host{all:initial;--acc:#1877f2;--acc-d:#166fe5;--ink:#1c1e21;--mut:#65676b;--bg:#fff;--line:#dadde1;--soft:#f0f2f5;}",
      "@media (prefers-color-scheme:dark){:host{--ink:#e4e6eb;--mut:#b0b3b8;--bg:#242526;--line:#3e4042;--soft:#3a3b3c;}}",
      ":host *,:host *::before,:host *::after{box-sizing:border-box;}",
      ".cxt-bar{position:fixed;left:16px;bottom:16px;right:auto;z-index:2147483000;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:14px;box-shadow:0 10px 30px rgba(0,0,0,.28);padding:12px 14px;font:600 13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;min-width:220px;max-width:280px;cursor:grab;touch-action:none;user-select:none;}",
      ".cxt-bar.cxt-dragging{cursor:grabbing;box-shadow:0 16px 40px rgba(0,0,0,.34);}",
      ".cxt-top{display:flex;align-items:center;gap:8px;margin-bottom:8px;}",
      ".cxt-grip{width:14px;height:14px;flex:0 0 auto;cursor:grab;border-radius:3px;opacity:.8;background-image:radial-gradient(var(--mut) 1.1px,transparent 1.1px);background-size:6px 6px;}",
      ".cxt-grip:active{cursor:grabbing;}",
      ".cxt-sel{display:flex;align-items:center;gap:8px;font-size:13px;}",
      ".cxt-sel::before{content:'';width:8px;height:8px;border-radius:50%;background:var(--acc);box-shadow:0 0 0 4px rgba(24,119,242,.18);flex:0 0 auto;}",
      ".cxt-queue{display:block;font-weight:400;color:var(--mut);margin-bottom:10px;white-space:pre-line;font-size:12px;}",
      ".cxt-acts{display:flex;gap:8px;}",
      ".cxt-bar button{font-family:inherit;font-size:12px;font-weight:600;line-height:1.2;border:0;border-radius:9px;padding:7px 12px;cursor:pointer;transition:background .15s,transform .05s;}",
      ".cxt-go{color:#fff;background:var(--acc);flex:1;}",
      ".cxt-go:hover{background:var(--acc-d);}",
      ".cxt-go:active{transform:scale(.98);}",
      ".cxt-go-fast{flex:0 0 auto;background:#d97706;}",
      ".cxt-go-fast:hover{background:#b45309;}",
      ".cxt-clr{color:var(--ink);background:var(--soft);}",
      ".cxt-clr:hover{filter:brightness(.96);}"
    ].join("");

    var bar = null;
    function ensureBar() {
      if (bar) return bar;
      var host = document.createElement("div");
      host.setAttribute("data-cxt-auto-ui", "bar");
      var sr = host.attachShadow({ mode: "open" });
      sr.innerHTML = "<style>" + BAR_CSS + "</style>" +
        "<div class=\"cxt-bar\"><div class=\"cxt-top\"><span class=\"cxt-grip\" title=\"Arrastra para mover\"></span>" +
        "<span class=\"cxt-sel\">0 seleccionadas</span></div>" +
        "<span class=\"cxt-queue\"></span>" +
        "<div class=\"cxt-acts\"><button type=\"button\" class=\"cxt-go\">Automatizar</button>" +
        "<button type=\"button\" class=\"cxt-go cxt-go-fast\" title=\"Compartir rápido: abre el diálogo con el modo rápido activado\">Rápido</button>" +
        "<button type=\"button\" class=\"cxt-clr\">Limpiar</button></div></div>";
      var barEl = sr.querySelector(".cxt-bar");
      sr.querySelector(".cxt-go").addEventListener("click", function () {
        if (selected.size) openModal(collectTargets());
      });
      sr.querySelector(".cxt-go-fast").addEventListener("click", function () {
        if (selected.size) openModal(collectTargets(), { fast: true });
      });
      sr.querySelector(".cxt-clr").addEventListener("click", function () {
        selected.clear();
        selectedData.clear();
        syncCheckboxes();
        renderBar();
      });

      var POS_KEY = "cxtBarPos";
      function clampPos(l, t, w, h) {
        return [Math.max(4, Math.min(l, window.innerWidth - w - 4)),
                Math.max(4, Math.min(t, window.innerHeight - h - 4))];
      }
      function place(l, t) {
        barEl.style.left = l + "px";
        barEl.style.top = t + "px";
        barEl.style.right = "auto";
        barEl.style.bottom = "auto";
      }
      try {
        chrome.storage.local.get([POS_KEY], function (o) {
          var p = o && o[POS_KEY];
          if (p && typeof p.l === "number" && typeof p.t === "number") place(p.l, p.t);
        });
      } catch (e) {}
      var dragging = false, dox = 0, doy = 0, bw = 0, bh = 0;
      barEl.addEventListener("pointerdown", function (e) {
        if (e.target.closest("button")) return;
        var r = barEl.getBoundingClientRect();
        dragging = true;
        dox = e.clientX - r.left;
        doy = e.clientY - r.top;
        bw = r.width;
        bh = r.height;
        barEl.classList.add("cxt-dragging");
        try { barEl.setPointerCapture(e.pointerId); } catch (err) {}
        e.preventDefault();
      });
      barEl.addEventListener("pointermove", function (e) {
        if (!dragging) return;
        var p = clampPos(e.clientX - dox, e.clientY - doy, bw, bh);
        place(p[0], p[1]);
      });
      function endDrag() {
        if (!dragging) return;
        dragging = false;
        barEl.classList.remove("cxt-dragging");
        var r = barEl.getBoundingClientRect();
        try { chrome.storage.local.set({ [POS_KEY]: { l: r.left, t: r.top } }); } catch (err) {}
      }
      barEl.addEventListener("pointerup", endDrag);
      barEl.addEventListener("pointercancel", endDrag);
      window.addEventListener("resize", function () {
        var r = barEl.getBoundingClientRect();
        var p = clampPos(r.left, r.top, r.width, r.height);
        place(p[0], p[1]);
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

    // --- resumen final de la corrida (status.last_run) ---
    // Panel informativo, NO modal: no roba foco, no usa alert() y no toca la barra
    // ni el controlador del título. Se muestra UNA vez por `last_run.id`.
    //
    // Regla de novedad (documentada):
    //   1. `last_run` debe traer un `id` no vacío y distinto del último mostrado.
    //   2. Y además: (a) `finished_at` cae dentro de la ventana de frescura de
    //      10 min (con 60 s de tolerancia hacia el futuro por desfase de reloj),
    //      o (b) esta pestaña vio la corrida ACTIVA en un poll anterior y acaba
    //      de pasar a inactiva (transición activo -> terminado).
    // Nunca se muestra el `last_run` vacío inicial ni uno viejo al cargar la página.
    var SUMMARY_FRESH_MS = 10 * 60 * 1000;
    var SUMMARY_FUTURE_MS = 60 * 1000;
    var SUMMARY_HIDE_MS = 12000;
    var SUMMARY_MSG_MAX = 400;

    var summaryEl = null;
    var summaryTimer = 0;
    var summaryShownId = "";
    var summaryActive = false;
    var summaryJustFinished = false;

    function summaryFinishedMs(lr) {
      var f = lr && lr.finished_at;
      if (typeof f === "string" && /^\d+$/.test(f)) f = +f;
      if (typeof f !== "number" || !isFinite(f) || f <= 0) return 0;
      return f < 1e12 ? f * 1000 : f;
    }

    function summaryCount(v) {
      var n = typeof v === "number" ? v : parseInt(v, 10);
      return isFinite(n) && n > 0 ? Math.floor(n) : 0;
    }

    function summaryModeLabel(mode) {
      if (mode === "dry") return "Simulación";
      if (mode === "fast") return "Rápido";
      if (mode === "normal" || mode === "full") return "Normal";
      return mode ? String(mode) : "";
    }

    function summaryTime(ms) {
      if (!ms) return "";
      try { return new Date(ms).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }); }
      catch (e) { console.debug("[CXT resumen] hora no disponible: " + (e && e.message ? e.message : e)); return ""; }
    }

    var SUMMARY_CSS = [
      ":host{all:initial;--ink:#e4e6eb;--mut:#b0b3b8;--bg:#242526;--line:#3e4042;--soft:#3a3b3c;--ok:#31a24c;--err:#f02849;--warn:#f7b928;}",
      ":host *,:host *::before,:host *::after{box-sizing:border-box;}",
      // 999: encima del feed y del header, pero por debajo de las capas modales
      // de Facebook (>=1000) para no tapar diálogos críticos de la publicación.
      ".cxt-sum{position:fixed;right:16px;bottom:16px;z-index:999;width:calc(100vw - 32px);max-width:320px;background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:14px;box-shadow:0 10px 30px rgba(0,0,0,.4);padding:12px 14px;font:600 13px/1.4 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;pointer-events:auto;animation:cxt-sum-in .18s ease-out;}",
      ".cxt-sum-head{display:flex;align-items:center;gap:8px;}",
      ".cxt-sum-dot{width:8px;height:8px;flex:0 0 auto;border-radius:50%;background:var(--ok);box-shadow:0 0 0 4px rgba(49,162,76,.18);}",
      ".cxt-sum-dot.bad{background:var(--err);box-shadow:0 0 0 4px rgba(240,40,73,.18);}",
      ".cxt-sum-title{flex:1;min-width:0;font-size:12px;font-weight:700;letter-spacing:.01em;text-transform:uppercase;color:var(--mut);}",
      ".cxt-sum-x{flex:0 0 auto;width:24px;height:24px;border:0;border-radius:7px;background:var(--soft);color:var(--mut);font-family:inherit;font-size:17px;line-height:1;cursor:pointer;padding:0;}",
      ".cxt-sum-x:hover{color:var(--ink);filter:brightness(1.1);}",
      ".cxt-sum-msg{margin:7px 0 8px;font-weight:500;font-size:13px;overflow-wrap:anywhere;white-space:pre-line;}",
      ".cxt-sum-meta{margin:0 0 9px;color:var(--mut);font-weight:400;font-size:11.5px;overflow-wrap:anywhere;}",
      ".cxt-sum-meta:empty{display:none;}",
      ".cxt-sum-counts{display:flex;flex-wrap:wrap;gap:6px;}",
      ".cxt-sum-c{display:inline-flex;align-items:baseline;gap:5px;background:var(--soft);border-radius:8px;padding:4px 8px;color:var(--mut);font-weight:500;font-size:11px;}",
      ".cxt-sum-n{font-size:14px;font-weight:700;font-variant-numeric:tabular-nums;}",
      ".cxt-sum-n.c-ok{color:var(--ok);}",
      ".cxt-sum-n.c-fail{color:var(--err);}",
      ".cxt-sum-n.c-skip{color:var(--warn);}",
      "@keyframes cxt-sum-in{from{opacity:0;transform:translateY(8px);}to{opacity:1;transform:translateY(0);}}",
      "@media (prefers-reduced-motion:reduce){.cxt-sum{animation:none;}}"
    ].join("");

    function ensureSummary() {
      if (summaryEl && document.contains(summaryEl)) return summaryEl;
      if (summaryEl) {
        try { summaryEl.remove(); }
        catch (e) { console.debug("[CXT resumen] no se pudo retirar el panel previo: " + (e && e.message ? e.message : e)); }
        summaryEl = null;
      }
      var host = document.createElement("div");
      host.setAttribute("data-cxt-auto-ui", "summary");
      var sr = host.attachShadow({ mode: "open" });
      sr.innerHTML = "<style>" + SUMMARY_CSS + "</style>" +
        "<div class=\"cxt-sum\" role=\"status\" aria-live=\"polite\" aria-atomic=\"true\">" +
        "<div class=\"cxt-sum-head\"><span class=\"cxt-sum-dot\"></span>" +
        "<span class=\"cxt-sum-title\">Resumen final</span>" +
        "<button type=\"button\" class=\"cxt-sum-x\" aria-label=\"Cerrar resumen\">×</button></div>" +
        "<p class=\"cxt-sum-msg\"></p>" +
        "<div class=\"cxt-sum-meta\"></div>" +
        "<div class=\"cxt-sum-counts\">" +
        "<span class=\"cxt-sum-c\"><b class=\"cxt-sum-n c-ok\">0</b>compartidos</span>" +
        "<span class=\"cxt-sum-c\"><b class=\"cxt-sum-n c-fail\">0</b>fallidos</span>" +
        "<span class=\"cxt-sum-c\"><b class=\"cxt-sum-n c-skip\">0</b>omitidos</span>" +
        "</div></div>";
      sr.querySelector(".cxt-sum-x").addEventListener("click", dismissSummary);
      document.documentElement.appendChild(host);
      summaryEl = host;
      return host;
    }

    function clearSummaryTimer() {
      if (summaryTimer) { clearTimeout(summaryTimer); summaryTimer = 0; }
    }

    function dismissSummary() {
      clearSummaryTimer();
      if (!summaryEl) return;
      try { summaryEl.remove(); }
      catch (e) { console.debug("[CXT resumen] no se pudo cerrar el panel: " + (e && e.message ? e.message : e)); }
      summaryEl = null;
    }

    function showSummary(lr, id, finishedMs) {
      var host = ensureSummary();
      var sr = host.shadowRoot;
      var shared = summaryCount(lr.shared);
      var failed = summaryCount(lr.failed);
      var skipped = summaryCount(lr.skipped);

      var msg = typeof lr.message === "string" ? lr.message.trim() : "";
      if (msg.length > SUMMARY_MSG_MAX) msg = msg.slice(0, SUMMARY_MSG_MAX - 1) + "…";
      sr.querySelector(".cxt-sum-msg").textContent = msg;
      sr.querySelector(".cxt-sum-n.c-ok").textContent = String(shared);
      sr.querySelector(".cxt-sum-n.c-fail").textContent = String(failed);
      sr.querySelector(".cxt-sum-n.c-skip").textContent = String(skipped);
      sr.querySelector(".cxt-sum-dot").classList.toggle("bad", lr.ok === false);

      var meta = [];
      var mode = summaryModeLabel(lr.mode);
      if (mode) meta.push("Modo: " + mode);
      var profile = typeof lr.profile_name === "string" ? lr.profile_name.trim() : "";
      if (profile) meta.push("Perfil: " + profile);
      var when = summaryTime(finishedMs);
      if (when) meta.push(when);
      sr.querySelector(".cxt-sum-meta").textContent = meta.join(" · ");

      summaryShownId = id;
      clearSummaryTimer();
      summaryTimer = setTimeout(function () {
        summaryTimer = 0;
        dismissSummary();
      }, SUMMARY_HIDE_MS);
    }

    function evaluateSummary(state) {
      if (!isAutoTab) return;
      var working = !!(state && titleWorking(state));
      if (working) {
        summaryActive = true;
        summaryJustFinished = false;
        return;
      }
      if (summaryActive) {
        summaryActive = false;
        summaryJustFinished = true;
      }
      try {
        var lr = state && state.last_run;
        if (lr && typeof lr === "object") {
          var id = lr.id == null ? "" : String(lr.id).trim();
          if (id && id !== summaryShownId) {
            var fin = summaryFinishedMs(lr);
            var now = Date.now();
            var fresh = !!fin && fin <= now + SUMMARY_FUTURE_MS &&
                        (now - fin) <= SUMMARY_FRESH_MS;
            if (fresh || summaryJustFinished) showSummary(lr, id, fin);
          }
        }
      } catch (e) {
        console.debug("[CXT resumen] error al evaluar last_run: " + (e && e.message ? e.message : e));
      } finally {
        summaryJustFinished = false;
      }
    }

    // --- controlador del título (plan 5.3) ---
    // Sin alert()/focus()/notificaciones: el aviso vive solo en document.title.
    function titleDebug(msg) {
      if (msg === titleLastLog) return; // sin spam: el poll repite el mismo estado
      titleLastLog = msg;
      try { console.debug("[CXT título] " + msg); } catch (e) {}
    }

    // El daemon responde el status plano; se aceptan también envoltorios {value|status}
    // para no romper si una ruta futura lo envuelve. `ok:false` = sin estado.
    function stateFrom(r) {
      if (!r || r.ok === false) return null;
      if (r.value && typeof r.value === "object") return r.value;
      if (r.status && typeof r.status === "object") return r.status;
      return r;
    }

    // `running` del daemon significa "worker vivo", no "publicando" (el status real
    // trae current/pending explícitos). Hay trabajo si hay job o cola; un
    // `{running:true}` sin esos campos (contrato simple) también cuenta como trabajo.
    function titleWorking(state) {
      if (state.current != null && state.current !== "") return true;
      if (+state.pending > 0) return true;
      return !!state.running && state.current === undefined && state.pending == null;
    }

    // Prioridad: alerta > pausa > trabajando > listo (última corrida) > título base.
    function desiredTitle(state) {
      if (!state || typeof state !== "object") return baseTitle;
      var alerta = state.alerta;
      if (typeof alerta === "string" && alerta.trim()) return TITLE_ALERT;
      if (alerta && typeof alerta === "object" && String(alerta.reason || "").trim()) return TITLE_ALERT;
      if (state.pausado) return TITLE_PAUSED;
      if (titleWorking(state)) return TITLE_WORKING;
      if (state.last_run && typeof state.last_run === "object") return TITLE_READY;
      return baseTitle;
    }

    function writeTitle(next) {
      if (!next) return;
      if (document.title === next) { titleLastApplied = next; return; }
      titleWriting = true; // guard: la mutación que sigue es nuestra, no de FB
      titleLastApplied = next;
      try {
        document.title = next;
      } catch (e) {
        titleLastApplied = document.title;
        titleDebug("no se pudo escribir el título: " + (e && e.message ? e.message : e));
      }
      // El MutationObserver corre como microtask: se libera el guard justo después.
      setTimeout(function () { titleWriting = false; }, 0);
    }

    function applyTitle(state) {
      var prev = titleState;
      titleState = (state && typeof state === "object") ? state : null;
      if (!isAutoTab) return;
      try {
        if (prev && !titleState) titleDebug("estado ausente: se restaura el título base");
        writeTitle(desiredTitle(titleState));
      } catch (e) {
        titleDebug("error al aplicar el título: " + (e && e.message ? e.message : e));
      }
    }

    // Facebook (SPA) reescribe <title> al navegar. Si hay estado activo, se re-aplica;
    // si no lo hay, el título nuevo pasa a ser la base (no se pisa la ruta del usuario).
    function onTitleMutation() {
      if (!isAutoTab) return;
      if (titleWriting) return;                // eco de nuestra propia escritura
      var actual = document.title;
      if (actual === titleLastApplied) return; // segunda red anti-bucle
      var deseado = desiredTitle(titleState);
      if (deseado === baseTitle) {
        baseTitle = actual;
        titleLastApplied = actual;
        return;
      }
      writeTitle(deseado);
    }

    function startTitleObserver(tries) {
      if (titleObserver) return;
      var head = document.head;
      if (!head) {
        // `head` puede no existir todavía al inyectar: reintento acotado, sin bucle.
        if ((tries || 0) < 20) {
          setTimeout(function () { startTitleObserver((tries || 0) + 1); }, 250);
        }
        return;
      }
      try {
        titleObserver = new MutationObserver(onTitleMutation);
        // Solo <head>: observar todo el documento dispararía en cada mutación de FB.
        titleObserver.observe(head, { childList: true, subtree: true, characterData: true });
      } catch (e) {
        titleObserver = null;
        titleDebug("no se pudo observar el título: " + (e && e.message ? e.message : e));
      }
    }

    // Al cerrarse la página no debe quedar pegado un "IA TRABAJANDO..." en el historial
    // (bfcache) ni en una pestaña reutilizada.
    function restoreTitle() {
      if (!isAutoTab) return;
      if (titleObserver) {
        try { titleObserver.disconnect(); } catch (e) {}
        titleObserver = null;
      }
      try { document.title = baseTitle; } catch (e) {}
      titleLastApplied = baseTitle;
    }

    function pollQueue() {
      request("autopostState").then(function (r) {
        queueState = r;
        var state = stateFrom(r);
        // El título se aplica AQUÍ y no en renderBar(): así funciona aunque la barra
        // no se pinte (ruta SPA de Facebook) y comparte el ciclo existente de 5 s.
        applyTitle(state);
        evaluateSummary(state);
        renderBar();
      });
    }

    // --- modal (shadow DOM) ---
    var MODAL_CSS = [
      ":host{all:initial;--acc:#1877f2;--acc-d:#166fe5;--ink:#1c1e21;--mut:#65676b;--bg:#fff;--line:#dadde1;--soft:#f0f2f5;--err:#d93025;}",
      "@media (prefers-color-scheme:dark){:host{--ink:#e4e6eb;--mut:#b0b3b8;--bg:#242526;--line:#3e4042;--soft:#3a3b3c;}}",
      "*{box-sizing:border-box;}",
      ".cxt-back{position:fixed;inset:0;z-index:2147483001;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;padding:16px;}",
      ".cxt-modal{width:380px;max-width:94vw;max-height:90vh;overflow:auto;background:var(--bg);color:var(--ink);border-radius:16px;box-shadow:0 24px 64px rgba(0,0,0,.45);font:13px/1.45 -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;}",
      ".cxt-head{display:flex;align-items:flex-start;gap:12px;padding:18px 18px 12px;}",
      ".cxt-head .t{flex:1;min-width:0;}",
      ".cxt-head h3{margin:0;font-size:16px;font-weight:700;letter-spacing:-.01em;}",
      ".cxt-head p{margin:3px 0 0;color:var(--mut);font-size:12px;font-weight:400;}",
      ".cxt-x{flex:0 0 auto;width:28px;height:28px;border:0;border-radius:8px;background:var(--soft);color:var(--mut);font-family:inherit;font-size:17px;line-height:1;cursor:pointer;}",
      ".cxt-x:hover{color:var(--ink);filter:brightness(.95);}",
      ".cxt-body{padding:0 18px;}",
      ".cxt-field{margin-bottom:12px;}",
      ".cxt-field label{display:block;font-weight:600;font-size:12px;color:var(--mut);margin-bottom:5px;}",
      ".cxt-field input{width:100%;padding:10px 12px;border:1px solid var(--line);border-radius:10px;font-family:inherit;font-size:13px;background:var(--bg);color:var(--ink);outline:none;transition:border-color .15s,box-shadow .15s;}",
      ".cxt-field input:focus{border-color:var(--acc);box-shadow:0 0 0 3px rgba(24,119,242,.22);}",
      ".cxt-grid{display:flex;gap:12px;}",
      ".cxt-grid>.cxt-field{flex:1;margin-bottom:12px;}",
      ".cxt-hint{color:var(--mut);font-weight:400;font-size:11px;margin-top:4px;}",
      // Picker de etiquetas (chips): mismo lenguaje visual del modal (tokens :host).
      ".f-picks{margin:-4px 0 12px;}",
      ".f-picks-head{display:flex;align-items:center;gap:8px;margin-bottom:6px;}",
      ".f-picks-title{flex:1;font-weight:600;font-size:12px;color:var(--mut);}",
      ".f-picks-head button{font-family:inherit;font-size:11px;font-weight:600;line-height:1;border:0;border-radius:7px;padding:5px 8px;background:var(--soft);color:var(--mut);cursor:pointer;transition:background-color .14s,color .14s;}",
      ".f-picks-head button:hover{color:var(--ink);filter:brightness(.95);}",
      ".f-picks-head button[hidden]{display:none;}",
      ".f-picks-q{width:100%;padding:7px 10px;border:1px solid var(--line);border-radius:8px;font-family:inherit;font-size:12px;background:var(--bg);color:var(--ink);outline:none;margin:0 0 6px;transition:border-color .14s,box-shadow .14s;}",
      ".f-picks-q:focus{border-color:var(--acc);box-shadow:0 0 0 2px rgba(24,119,242,.2);}",
      ".f-picks-q[hidden]{display:none;}",
      ".f-picks-row{display:flex;flex-wrap:wrap;gap:6px;align-items:flex-start;}",
      ".f-picks-row.f-all{max-height:140px;overflow:auto;padding-right:2px;}",
      ".f-pick{display:inline-flex;align-items:center;gap:6px;max-width:100%;padding:5px 9px;border:1px solid var(--line);border-radius:8px;background:var(--soft);color:var(--ink);font-family:inherit;font-size:12px;font-weight:500;line-height:1.3;cursor:pointer;transition:background-color .14s,color .14s;}",
      ".f-pick:hover{filter:brightness(.96);}",
      ".f-pick:focus-visible{outline:2px solid var(--acc);outline-offset:1px;}",
      ".f-pick.on{background:var(--acc);border-color:var(--acc);color:#fff;}",
      ".f-pick.on:hover{filter:none;background:var(--acc-d);border-color:var(--acc-d);}",
      ".f-pick .lb{min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:168px;}",
      ".f-pick .num{font-variant-numeric:tabular-nums;font-size:11px;font-weight:700;color:var(--mut);background:var(--bg);border-radius:6px;padding:1px 5px;}",
      ".f-pick.on .num{color:#fff;background:rgba(255,255,255,.22);}",
      ".f-pick-note{color:var(--mut);font-size:11.5px;font-weight:400;padding:1px 0;}",
      "@media (prefers-reduced-motion:reduce){.f-pick,.f-picks-head button,.f-picks-q{transition:none;}}",
      ".cxt-chkrow{display:flex;align-items:center;gap:10px;font-weight:500;margin:2px 0 4px;cursor:pointer;}",
      ".cxt-chkrow input{width:16px;height:16px;accent-color:var(--acc);flex:0 0 auto;}",
      ".cxt-chkrow small{display:block;color:var(--mut);font-weight:400;font-size:11px;}",
      ".cxt-count{display:flex;align-items:center;gap:8px;margin:14px 0 4px;font-weight:600;font-size:12px;}",
      ".cxt-count b{display:inline-flex;align-items:center;justify-content:center;min-width:22px;height:22px;padding:0 6px;border-radius:999px;background:var(--acc);color:#fff;font-size:12px;}",
      ".cxt-err{color:var(--err);font-weight:600;font-size:12px;margin-top:6px;}",
      ".cxt-err:empty{display:none;}",
      ".cxt-warn{display:block;color:#8a6d00;background:var(--soft);border-left:3px solid #e2b93b;border-radius:8px;padding:8px 10px;margin:14px 0 0;font-size:11.5px;font-weight:500;}",
      ".cxt-foot{display:flex;justify-content:flex-end;gap:8px;padding:16px 18px 18px;}",
      ".cxt-foot button{font-family:inherit;font-size:13px;font-weight:600;border:0;border-radius:10px;padding:10px 16px;cursor:pointer;transition:background .15s,transform .05s;}",
      ".cxt-cancel{color:var(--ink);background:var(--soft);}",
      ".cxt-cancel:hover{filter:brightness(.95);}",
      ".cxt-enqueue{color:#fff;background:var(--acc);}",
      ".cxt-enqueue:hover{background:var(--acc-d);}",
      ".cxt-enqueue:active{transform:scale(.98);}"
    ].join("");

    var modal = null;
    var modalTargets = [];

    // Etiquetas del selector de grupos: chips con conteo (request "groupscanGet",
    // que devuelve labels [{label,count,category,examples}] en orden count-desc).
    // La verdad de la selección sigue siendo .f-filter; los chips solo lo editan.
    var PICK_TTL = 60000;
    var picks = { labels: null, at: 0, all: false, q: "" };
    var picksPending = false;
    var picksRetried = false;   // reintento único por racha de fallo (sin bucle)
    var picksRetryTimer = 0;

    function ensureModal() {
      if (modal) return modal;
      var host = document.createElement("div");
      host.setAttribute("data-cxt-auto-ui", "modal");
      var sr = host.attachShadow({ mode: "open" });
      sr.innerHTML = "<style>" + MODAL_CSS + "</style>" +
        "<div class=\"cxt-back\"><div class=\"cxt-modal\" role=\"dialog\" aria-modal=\"true\">" +
        "<div class=\"cxt-head\"><div class=\"t\"><h3>Automatizar publicación</h3>" +
        "<p>Elige los grupos y encola el envío automático</p></div>" +
        "<button type=\"button\" class=\"cxt-x f-close\" aria-label=\"Cerrar\">×</button></div>" +
        "<div class=\"cxt-body\">" +
        "<div class=\"cxt-field\"><label>Grupos (texto)</label><input type=\"text\" class=\"f-filter\" placeholder=\"playa del carmen\"></div>" +
        "<div class=\"f-picks\"><div class=\"f-picks-head\"><span class=\"f-picks-title\">Etiquetas</span>" +
        "<button type=\"button\" class=\"f-picks-clr\" hidden>limpiar</button>" +
        "<button type=\"button\" class=\"f-picks-all\">ver todas</button></div>" +
        "<input type=\"text\" class=\"f-picks-q\" placeholder=\"buscar…\">" +
        "<div class=\"f-picks-row\"></div></div>" +
        "<div class=\"cxt-grid\">" +
        "<div class=\"cxt-field\"><label>Límite</label><input type=\"number\" min=\"0\" value=\"10\" class=\"f-limit\"></div>" +
        "<div class=\"cxt-field\"><label>Intervalo (min)</label><input type=\"number\" min=\"1\" value=\"15\" class=\"f-interval\"><div class=\"cxt-hint\">recomendado 15 · máx 60</div></div>" +
        "</div>" +
        "<div class=\"cxt-field\"><label>Texto extra (opcional)</label><input type=\"text\" class=\"f-text\"></div>" +
        "<label class=\"cxt-chkrow\"><input type=\"checkbox\" class=\"f-dry\"><span>Simulación<small>No publica · solo prueba</small></span></label>" +
        "<label class=\"cxt-chkrow\"><input type=\"checkbox\" class=\"f-fast\"><span>Rápido (sin esperas anti-ban)</span></label>" +
        "<div class=\"cxt-count\">Publicaciones seleccionadas <b class=\"f-count\">0</b></div>" +
        "<div class=\"cxt-err f-err\"></div>" +
        "<div class=\"cxt-warn\">Compartir en grupos de forma automatizada puede limitar tu cuenta.</div>" +
        "</div>" +
        "<div class=\"cxt-foot\"><button type=\"button\" class=\"cxt-cancel f-cancel\">Cancelar</button>" +
        "<button type=\"button\" class=\"cxt-enqueue f-enqueue\">Encolar</button></div>" +
        "</div></div>";

      sr.querySelector(".f-close").addEventListener("click", closeModal);
      sr.querySelector(".f-cancel").addEventListener("click", closeModal);
      sr.querySelector(".f-enqueue").addEventListener("click", enqueue);
      sr.querySelector(".f-picks-all").addEventListener("click", function () {
        picks.all = !picks.all;
        renderPicks();
      });
      sr.querySelector(".f-picks-clr").addEventListener("click", clearPicks);
      sr.querySelector(".f-picks-q").addEventListener("input", function () {
        picks.q = this.value;
        renderPicks();
      });
      // El input manual y los chips comparten estado: al teclear, re-marca chips.
      sr.querySelector(".f-filter").addEventListener("input", syncPicks);
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

    function openModal(items, opts) {
      var host = ensureModal();
      modalTargets = items.slice();
      host.shadowRoot.querySelector(".f-count").textContent = String(modalTargets.length);
      host.shadowRoot.querySelector(".f-dry").checked = false; // Simulación: opt-in, arranca apagada
      host.shadowRoot.querySelector(".f-fast").checked = !!(opts && opts.fast); // Rápido: opt-in; solo se enciende si el botón Rápido lo pide
      showError("");
      picks.all = false;
      picks.q = "";
      picksRetried = false;                                   // nueva racha: reintento disponible
      if (picksRetryTimer) { clearTimeout(picksRetryTimer); picksRetryTimer = 0; }
      host.shadowRoot.querySelector(".f-picks-q").value = "";
      host.style.display = "block";
      loadPicks();
    }

    function closeModal() {
      if (modal) modal.style.display = "none";
    }

    // --- etiquetas (chips) del modal ---
    function knownLabels() {
      var m = {};
      var labels = (picks && picks.labels) || [];
      for (var i = 0; i < labels.length; i++) m[norm(labels[i].label)] = 1;
      return m;
    }

    function filterParts() {
      var v = modal ? modal.shadowRoot.querySelector(".f-filter").value : "";
      var out = [];
      v.split(",").forEach(function (p) {
        p = p.trim();
        if (p) out.push(p);
      });
      return out;
    }

    // Reescribe .f-filter: primero lo manual/desconocido (en su orden actual) y
    // luego las etiquetas conocidas seleccionadas en orden de conteo.
    function writeFilter(parts) {
      if (!modal) return;
      var known = knownLabels();
      var unknown = [];
      var sel = {};
      for (var i = 0; i < parts.length; i++) {
        var k = norm(parts[i]);
        if (known[k]) sel[k] = 1;
        else unknown.push(parts[i]);
      }
      var ordered = [];
      var labels = (picks && picks.labels) || [];
      for (var j = 0; j < labels.length; j++) {
        if (sel[norm(labels[j].label)]) ordered.push(labels[j].label);
      }
      modal.shadowRoot.querySelector(".f-filter").value = unknown.concat(ordered).join(", ");
      renderPicks();
    }

    function togglePick(label) {
      var key = norm(label);
      var parts = filterParts();
      var out = [];
      var had = false;
      for (var i = 0; i < parts.length; i++) {
        if (norm(parts[i]) === key) { had = true; continue; }
        out.push(parts[i]);
      }
      if (!had) out.push(label);
      writeFilter(out);
    }

    // "limpiar": quita solo las entradas que son etiquetas conocidas; deja lo manual.
    function clearPicks() {
      var known = knownLabels();
      var parts = filterParts();
      var out = [];
      for (var i = 0; i < parts.length; i++) {
        if (!known[norm(parts[i])]) out.push(parts[i]);
      }
      writeFilter(out);
    }

    // Refresca el estado visual de los chips sin reconstruirlos (tecleo manual).
    function syncPicks() {
      if (!modal) return;
      var sr = modal.shadowRoot;
      var parts = filterParts();
      var keys = {};
      for (var i = 0; i < parts.length; i++) keys[norm(parts[i])] = 1;
      var chips = sr.querySelectorAll(".f-pick");
      for (var c = 0; c < chips.length; c++) {
        var on = !!keys[norm(chips[c].getAttribute("data-label") || "")];
        chips[c].classList.toggle("on", on);
        chips[c].setAttribute("aria-pressed", on ? "true" : "false");
      }
      var known = knownLabels();
      var hasSel = parts.some(function (p) { return !!known[norm(p)]; });
      var clr = sr.querySelector(".f-picks-clr");
      clr.hidden = !hasSel;
      clr.disabled = !hasSel;
    }

    function chipFor(label) {
      if (!modal) return null;
      var chips = modal.shadowRoot.querySelectorAll(".f-pick");
      for (var i = 0; i < chips.length; i++) {
        if (chips[i].getAttribute("data-label") === label) return chips[i];
      }
      return null;
    }

    // Vista por defecto: top 8 por conteo; "ver todas" o buscar muestran todo
    // (el contenedor se vuelve scrolleable con max-height vía CSS).
    function renderPicks() {
      if (!modal) return;
      var sr = modal.shadowRoot;
      var row = sr.querySelector(".f-picks-row");
      var qEl = sr.querySelector(".f-picks-q");
      var allBtn = sr.querySelector(".f-picks-all");
      var labels = (picks && picks.labels) || [];
      row.innerHTML = "";
      allBtn.textContent = picks.all ? "ver menos" : "ver todas";

      function note(text) {
        var s = document.createElement("span");
        s.className = "f-pick-note";
        s.textContent = text;
        row.appendChild(s);
      }

      if (!labels.length) {
        qEl.hidden = true;
        allBtn.hidden = true;
        note("sin etiquetas — escanea grupos en la extensión");
        syncPicks();
        return;
      }
      qEl.hidden = false;
      allBtn.hidden = false;

      var q = norm(picks.q);
      var list = [];
      for (var i = 0; i < labels.length; i++) {
        if (q && norm(labels[i].label).indexOf(q) < 0) continue;
        list.push(labels[i]);
        if (!q && !picks.all && list.length >= 8) break;
      }
      row.classList.toggle("f-all", picks.all || !!q);
      if (!list.length) { note("sin coincidencias"); syncPicks(); return; }

      for (var j = 0; j < list.length; j++) {
        (function (l) {
          var b = document.createElement("button");
          b.type = "button";
          b.className = "f-pick";
          b.setAttribute("data-label", l.label);
          b.title = l.label + " · " + (typeof l.count === "number" ? l.count : 0) +
                    " grupos" + (l.category ? " · " + l.category : "");
          var lb = document.createElement("span");
          lb.className = "lb";
          lb.textContent = l.label;
          b.appendChild(lb);
          if (typeof l.count === "number") {
            var n = document.createElement("span");
            n.className = "num";
            n.textContent = String(l.count);
            b.appendChild(n);
          }
          b.addEventListener("click", function () {
            togglePick(l.label);
            var next = chipFor(l.label);
            if (next) next.focus();
          });
          row.appendChild(b);
        })(list[j]);
      }
      syncPicks();
    }

    // Solo se cachea una respuesta con etiquetas válidas (TTL 60s). Un fallo o un
    // `labels` vacío NUNCA se guarda como caché: se pinta el vacío y se reintenta
    // exactamente una vez a los ~1.5s; con la caché vieja, cada apertura re-pide.
    function schedulePicksRetry() {
      if (picksRetried) return; // un reintento por racha: sin bucle
      picksRetried = true;
      picksRetryTimer = setTimeout(function () {
        picksRetryTimer = 0;
        if (modal && modal.style.display !== "none" && !picksPending) loadPicks();
      }, 1500);
    }

    function loadPicks() {
      var now = Date.now();
      if (picks.labels && picks.labels.length && (now - picks.at) < PICK_TTL) { renderPicks(); return; }
      if (picksPending) { renderPicks(); return; }
      picksPending = true;
      request("groupscanGet").then(function (r) {
        picksPending = false;
        var clean = [];
        if (r && typeof r === "object" && Object.prototype.toString.call(r.labels) === "[object Array]") {
          var raw = r.labels;
          for (var i = 0; i < raw.length; i++) {
            var l = raw[i];
            if (l && typeof l.label === "string" && l.label.trim()) {
              clean.push({ label: l.label.trim(), count: l.count, category: l.category });
            }
          }
        }
        if (clean.length) {
          picks.labels = clean;
          picks.at = Date.now();
          picksRetried = false; // racha nueva: el próximo fallo vuelve a tener su reintento
          renderPicks();
          return;
        }
        picks.labels = []; // sin datos: se pinta el vacío pero no se cachea nada
        renderPicks();
        schedulePicksRetry();
      });
    }

    // Limpia el texto de expect: quita sufijos de interfaz de Facebook
    // ("Ver más", "Ver traducción", "hace aproximad…"), colapsa espacios y
    // recorta a 160 caracteres en un límite de palabra. Si queda vacío,
    // devuelve el original recortado en espacios.
    function cleanExpect(s) {
      var raw = (s === null || s === undefined) ? "" : String(s);
      var out = raw;
      var cuts = [
        "... Ver más", "… Ver más", "Ver más",
        "Ver traducción", "Ver original",
        "hace aproximad...", "hace aproximad…", "hace aproximad"
      ];
      for (var i = 0; i < cuts.length; i++) {
        var at = out.indexOf(cuts[i]);
        if (at >= 0) out = out.slice(0, at);
      }
      out = out.replace(/[…]+/g, " ").replace(/\.{3,}/g, " ").replace(/\s+/g, " ").trim();
      if (out.length > 160) {
        out = out.slice(0, 160);
        var cut = out.lastIndexOf(" ");
        if (cut >= 80) out = out.slice(0, cut);
        out = out.trim();
      }
      if (!out) return raw.trim();
      return out;
    }

    function enqueue() {
      var sr = modal.shadowRoot;
      var fastFlag = !!(sr.querySelector(".f-fast") && sr.querySelector(".f-fast").checked);
      var items = modalTargets.map(function (t) {
        return {
          url: t.url,
          expect: cleanExpect(t.expect),
          expect_raw: t.expect,
          fast: fastFlag
        };
      });
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
      showError("");
      request("autoQueue", value).then(function (r) {
        if (r && r.ok) { closeModal(); return; }
        if (r && r.error) { showError(r.error); return; }
        if (lastReqErr === "invalidated") {
          showError("La extensión se recargó y esta página quedó desconectada. Recarga la página (F5) y reintenta.");
          return;
        }
        if (r && typeof r === "object") {
          showError("Respuesta inesperada del motor: " + JSON.stringify(r).slice(0, 160));
          return;
        }
        showError("El motor no respondió. Verifica que CXT esté activo e inténtalo de nuevo.");
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

    function detectAutoTab(tries) {
      request("autoTab").then(function (r) {
        if (r && r.auto) {
          isAutoTab = true;
          startTitleObserver();
          applyTitle(titleState);
          evaluateSummary(stateFrom(queueState));
          return;
        }
        if ((tries || 0) < 4) {
          setTimeout(function () { detectAutoTab((tries || 0) + 1); }, 800);
        }
      });
    }

    pollQueue();
    setInterval(pollQueue, 5000);
    detectAutoTab(0);
    window.addEventListener("pagehide", restoreTitle);
    window.addEventListener("pagehide", function () {
      clearSummaryTimer();
      dismissSummary();
    });
    // Si Chrome restaura la página desde bfcache, el observer quedó desconectado.
    window.addEventListener("pageshow", function (ev) {
      if (ev && ev.persisted && isAutoTab) startTitleObserver();
    });

    // Guard de pruebas sin navegador (Node): en Chrome `module` no existe y no pasa nada.
    // Permite verificar `desiredTitle` con estados simulados sin abrir un navegador.
    if (typeof module !== "undefined" && module.exports) {
      module.exports = {
        TITLE_WORKING: TITLE_WORKING,
        TITLE_PAUSED: TITLE_PAUSED,
        TITLE_ALERT: TITLE_ALERT,
        TITLE_READY: TITLE_READY,
        desiredTitle: desiredTitle
      };
    }
  })();
}
