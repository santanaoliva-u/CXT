(() => {
  "use strict";

  const API = "http://127.0.0.1:8795";
  const DEMO = new URLSearchParams(location.search).get("demo") === "1";
  const REQUEST_TIMEOUT = 120000;

  const state = {
    demo: DEMO,
    run: null,
    activeAngle: "",
    busy: false,
    dirty: false,
    approved: false,
    lastText: "",
  };

  const $ = (id) => document.getElementById(id);

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined && text !== null) el.textContent = String(text);
    return el;
  }

  function clear(target) {
    while (target.firstChild) target.removeChild(target.firstChild);
  }

  function safeUrl(value) {
    try {
      const url = new URL(String(value || ""));
      return url.protocol === "http:" || url.protocol === "https:" ? url.href : "";
    } catch (err) {
      return "";
    }
  }

  function prettyDate(value) {
    if (!value) return "sin fecha";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value);
    return date.toLocaleString("es-MX", {
      day: "2-digit",
      month: "short",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function prettyIntent(value) {
    const labels = {
      news: "Actualidad verificada",
      research: "Investigación",
      explainer: "Explicativo",
      promotion: "Brief de marca",
    };
    return labels[value] || value || "Sin clasificar";
  }

  function toast(message, kind) {
    const box = $("toast");
    box.textContent = message || "";
    box.className = "toast" + (kind ? " is-" + kind : "");
    box.hidden = !message;
    if (message) {
      window.clearTimeout(toast.timer);
      toast.timer = window.setTimeout(() => {
        box.hidden = true;
      }, 5200);
    }
  }

  async function api(path, options) {
    const controller = new AbortController();
    const timer = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT);
    try {
      const response = await fetch(API + path, {
        ...(options || {}),
        signal: controller.signal,
        headers: { "Content-Type": "application/json", ...((options && options.headers) || {}) },
        cache: "no-store",
      });
      const raw = await response.text();
      let body = {};
      if (raw) {
        try {
          body = JSON.parse(raw);
        } catch (err) {
          body = { ok: false, error: "Respuesta no válida del servidor." };
        }
      }
      if (!response.ok) {
        const error = new Error(body.error || "Error " + response.status);
        error.status = response.status;
        error.body = body;
        throw error;
      }
      return body;
    } catch (err) {
      if (err && err.name === "AbortError") throw new Error("El servidor tardó demasiado. Intenta de nuevo.");
      throw err;
    } finally {
      window.clearTimeout(timer);
    }
  }

  function setServer(status, label) {
    const pill = $("serverPill");
    pill.className = "server-pill " + status;
    pill.textContent = label;
  }

  async function checkHealth() {
    if (state.demo) {
      setServer("is-demo", "Modo demo");
      return;
    }
    setServer("is-checking", "Comprobando servidor");
    try {
      const health = await api("/health");
      setServer("is-online", "Servidor en línea");
      if (!health.deepseek_configured) {
        toast("El servidor está en línea, pero DeepSeek no está configurado.", "error");
      }
    } catch (err) {
      setServer("is-offline", "Servidor apagado");
      toast("No se pudo conectar con 127.0.0.1:8795. Inicia editorial_server.py.", "error");
    }
  }

  function demoData() {
    const now = new Date();
    const when = (hours) => new Date(now.getTime() - hours * 3600000).toISOString();
    const sources = [
      {
        title: "La Casa Blanca anuncia cambios arancelarios sobre productos canadienses",
        outlet: "BBC Mundo",
        url: "https://www.bbc.com/mundo",
        published_at: when(2),
        excerpt: "La medida afecta a un grupo acotado de bienes y se aplicará en las próximas semanas, según el comunicado oficial.",
        region: "internacional",
        language: "es",
        confidence: "alta",
        claim: "La Casa Blanca anunció cambios arancelarios.",
        warnings: [],
      },
      {
        title: "Trump afirma que ordenó al Departamento de Justicia intervenir en Alaska",
        outlet: "The New York Times",
        url: "https://www.nytimes.com/",
        published_at: when(3),
        excerpt: "La declaración se produce en la recta final de la campaña por el Senado y genera preguntas sobre los límites del Ejecutivo.",
        region: "internacional",
        language: "en",
        confidence: "media",
        claim: "Trump dijo que ordenó la intervención.",
        warnings: [],
      },
      {
        title: "Los casos de dengue siguen al alza en Florida tras la emergencia sanitaria",
        outlet: "The Washington Post",
        url: "https://www.washingtonpost.com/",
        published_at: when(4),
        excerpt: "Las autoridades locales reportan un aumento sostenido de casos y refuerzan las campañas de prevención.",
        region: "internacional",
        language: "en",
        confidence: "alta",
        claim: "Los casos de dengue aumentaron.",
        warnings: [],
      },
      {
        title: "México revisa su estrategia comercial ante los cambios en Norteamérica",
        outlet: "El Universal",
        url: "https://www.eluniversal.com.mx/",
        published_at: when(5),
        excerpt: "La Secretaría de Economía señaló que mantiene abiertos los canales de diálogo con sus socios comerciales.",
        region: "mexico",
        language: "es",
        confidence: "media",
        claim: "México revisa su estrategia comercial.",
        warnings: [],
      },
      {
        title: "Playa del Carmen refuerza la vigilancia sanitaria en zonas turísticas",
        outlet: "Diario de Quintana Roo",
        url: "https://example.com/quintana-roo",
        published_at: when(6),
        excerpt: "El ayuntamiento informó sobre recorridos preventivos y campañas de información para visitantes y residentes.",
        region: "local",
        language: "es",
        confidence: "media",
        claim: "Playa del Carmen refuerza la vigilancia.",
        warnings: [],
      },
    ];
    const angles = [
      {
        id: "global",
        label: "Global",
        thesis: "La agenda global se mueve entre la política estadounidense, el comercio y una alerta sanitaria que ya tiene efectos locales.",
        source_ids: [sources[0].url, sources[1].url],
        why_now: sources[0].published_at,
        warning: "",
      },
      {
        id: "mexico",
        label: "México",
        thesis: "México observa los cambios comerciales de Norteamérica con una mezcla de cautela y margen de negociación.",
        source_ids: [sources[3].url],
        why_now: sources[3].published_at,
        warning: "",
      },
      {
        id: "local",
        label: "Local",
        thesis: "En Playa del Carmen, la conversación global aterriza en turismo, salud preventiva y la temporada alta.",
        source_ids: [sources[4].url],
        why_now: sources[4].published_at,
        warning: "",
      },
    ];
    const draftText = [
      "Tres frentes marcan la conversación global de esta semana: la política estadounidense entra en su recta final, la Casa Blanca ajusta aranceles sobre productos canadienses y Florida enfrenta un aumento de casos de dengue tras declarar la emergencia sanitaria.",
      "Para Playa del Carmen, la conexión no es abstracta: el turismo y la temporada alta vuelven relevantes las señales sobre comercio, movilidad y prevención. La evidencia disponible permite seguir los hechos, no adelantar conclusiones.",
      "Lo que está pasando se entiende mejor con fuentes a la vista. La mesa seguirá contrastando cada dato antes de publicar.",
    ].join("\n\n");
    return {
      ok: true,
      run_id: "demo-run-01",
      assignment: {
        instruction: "qué está pasando en el mundo, con contexto para Playa del Carmen",
        intent: "news",
        source_policy: "news",
        audience: "público general de Playa del Carmen",
        locale: "es-MX",
        tone: "claro, humano y verificable",
        must_include: ["contexto local"],
        must_not: ["ataques personales"],
        freshness_hours: 48,
        min_sources: 2,
        source_mix: ["local", "nacional", "internacional"],
        max_length: 900,
        cta: "",
        hashtags: ["#PlayaDelCarmen", "#Actualidad", "#Mundo"],
        mode: "editorial",
        local: true,
        focus_terms: ["pasando", "mundo", "playa", "carmen"],
      },
      dossier: { query: "qué está pasando en el mundo", sources, errors: [], cached: false, fetched_at: now.toISOString() },
      angles,
      brief: {
        instruction: "qué está pasando en el mundo, con contexto para Playa del Carmen",
        intent: "news",
        source_policy: "news",
        audience: "público general de Playa del Carmen",
        tone: "claro, humano y verificable",
        must_include: ["contexto local"],
        must_not: ["ataques personales"],
        freshness_hours: 48,
        min_sources: 2,
        checklist: [
          "Incluye: contexto local.",
          "Usa al menos 2 fuentes independientes y frescas (máximo 48 h).",
          "Cada afirmación de actualidad debe poder rastrearse a una fuente del dossier.",
          "Extensión máxima: 900 caracteres.",
        ],
        red_lines: ["No menciones: ataques personales.", "No inventes hechos, cifras, fechas, nombres ni citas."],
        requires_sources: true,
      },
      draft: {
        text: draftText,
        angle_id: "global",
        provider: "deepseek",
        model: "deepseek-flash",
        created_at: now.toISOString(),
        claims: [
          { text: "La Casa Blanca ajusta aranceles sobre productos canadienses.", sources: [sources[0].url] },
          { text: "Trump dijo que ordenó al Departamento de Justicia intervenir en Alaska.", sources: [sources[1].url] },
          { text: "Florida enfrenta un aumento de casos de dengue tras la emergencia.", sources: [sources[2].url] },
          { text: "México revisa su estrategia comercial.", sources: [sources[3].url] },
        ],
      },
      stamp: {
        color: "green",
        reasons: ["Fuentes, instrucción, claims y formato pasan la verificación."],
        checks: {
          safety_instruction: { ok: true, detail: "" },
          draft: { ok: true, detail: "" },
          sources_present: { ok: true, detail: "5 fuentes" },
          freshness: { ok: true, detail: "5 fuentes dentro de 48 h" },
          independent_outlets: { ok: true, detail: "5 medios" },
          no_filler: { ok: true, detail: "" },
          must_include: { ok: true, detail: "contexto local" },
          must_not: { ok: true, detail: "" },
          instruction_focus: { ok: true, detail: "mundo, playa" },
          claims_sourced: { ok: true, detail: "" },
          numbers_grounded: { ok: true, detail: "" },
          prompt_injection: { ok: true, detail: "" },
          duplicate: { ok: true, detail: "" },
          format: { ok: true, detail: "603 caracteres, 3 hashtags" },
          not_edited: { ok: true, detail: "sin editar" },
        },
        blocked: false,
        publishable: true,
        requires_review: false,
        created_at: now.toISOString(),
      },
      audit: [
        { id: "demo-run-01", created_at: now.toISOString(), instruction: "qué está pasando en el mundo", mode: "editorial", stamp_color: "green" },
        { id: "demo-run-02", created_at: when(28), instruction: "contexto local para la temporada alta", mode: "editorial", stamp_color: "amber" },
        { id: "demo-run-03", created_at: when(52), instruction: "explica el acuerdo comercial", mode: "research", stamp_color: "red" },
      ],
    };
  }

  function briefSection(title, items) {
    const block = node("div", "brief-block");
    block.appendChild(node("h3", "", title));
    if (!items || !items.length) {
      block.appendChild(node("p", "empty", "Sin condiciones."));
      return block;
    }
    const list = node("ul");
    items.forEach((item) => list.appendChild(node("li", "", item)));
    block.appendChild(list);
    return block;
  }

  function renderBrief(run) {
    const body = $("briefBody");
    clear(body);
    const brief = run && run.brief;
    if (!brief) {
      body.appendChild(node("p", "empty", "El brief aparecerá después de investigar."));
      $("briefPolicy").textContent = "—";
      return;
    }
    $("briefPolicy").textContent = brief.requires_sources ? "fuentes obligatorias" : "brief de marca";
    body.appendChild(briefSection("Objetivo", [brief.instruction]));
    body.appendChild(briefSection("Debe aparecer", brief.checklist));
    body.appendChild(briefSection("Líneas rojas", brief.red_lines));
  }

  function renderSummary(run) {
    const assignment = (run && run.assignment) || {};
    const sources = ((run && run.dossier) || {}).sources || [];
    $("summaryIntent").textContent = prettyIntent(assignment.intent);
    $("summarySources").textContent = sources.length + (sources.length === 1 ? " fuente" : " fuentes");
    $("summaryFreshness").textContent = (assignment.freshness_hours || "—") + " h";
  }

  function renderAngles(run) {
    const tabs = $("angleTabs");
    clear(tabs);
    const angles = (run && run.angles) || [];
    if (!angles.length) {
      $("angleThesis").replaceChildren(node("p", "empty", "Los ángulos aparecerán aquí."));
      return;
    }
    if (!state.activeAngle || !angles.some((angle) => angle.id === state.activeAngle)) {
      state.activeAngle = (run.draft && run.draft.angle_id) || angles[0].id;
    }
    angles.forEach((angle) => {
      const button = node("button", "angle-tab", angle.label || angle.id);
      button.type = "button";
      button.setAttribute("role", "tab");
      button.setAttribute("aria-selected", angle.id === state.activeAngle ? "true" : "false");
      if (angle.id === state.activeAngle) button.classList.add("is-active");
      if (angle.warning) {
        button.appendChild(node("span", "warn", "*"));
        button.title = angle.warning;
      }
      button.addEventListener("click", () => selectAngle(angle.id));
      tabs.appendChild(button);
    });
    renderThesis(run);
  }

  function renderThesis(run) {
    const box = $("angleThesis");
    clear(box);
    const angle = ((run && run.angles) || []).find((item) => item.id === state.activeAngle);
    if (!angle) {
      box.appendChild(node("p", "empty", "Selecciona un ángulo."));
      return;
    }
    box.appendChild(node("p", "", angle.thesis || "Sin tesis."));
    const why = node("p", "why", "Por qué ahora · " + prettyDate(angle.why_now));
    box.appendChild(why);
    if (angle.warning) box.appendChild(node("p", "warning", angle.warning));
  }

  function renderSources(run) {
    const list = $("sourcesList");
    clear(list);
    const sources = ((run && run.dossier) || {}).sources || [];
    $("sourcesCount").textContent = String(sources.length);
    if (!sources.length) {
      list.appendChild(node("p", "empty", "Aún no hay fuentes."));
      return;
    }
    sources.forEach((source) => {
      const card = node("article", "source-card");
      const meta = node("div", "source-meta");
      meta.appendChild(node("span", "outlet", source.outlet || "Medio"));
      meta.appendChild(node("span", "", prettyDate(source.published_at)));
      meta.appendChild(node("span", "", source.region || "internacional"));
      meta.appendChild(node("span", "", source.confidence || "media"));
      card.appendChild(meta);
      card.appendChild(node("h3", "source-title", source.title || "Sin titular"));
      if (source.excerpt) card.appendChild(node("p", "source-excerpt", source.excerpt));
      const href = safeUrl(source.url);
      if (href) {
        const link = node("a", "source-link", "Abrir fuente");
        link.href = href;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
        card.appendChild(link);
      }
      if (source.warnings && source.warnings.length) {
        card.appendChild(node("p", "source-warn", source.warnings.join(" · ")));
      }
      list.appendChild(card);
    });
  }

  function sourceLabel(run, url) {
    const sources = ((run && run.dossier) || {}).sources || [];
    const found = sources.find((source) => source.url === url);
    if (found) return found.outlet;
    try {
      return new URL(String(url || "")).hostname;
    } catch (err) {
      return String(url || "").slice(0, 60);
    }
  }

  function renderClaims(run) {
    const list = $("claimsList");
    clear(list);
    const claims = ((run && run.draft) || {}).claims || [];
    $("claimsCount").textContent = String(claims.length);
    if (!claims.length) {
      list.appendChild(node("li", "empty", "Sin claims declarados."));
      return;
    }
    claims.forEach((claim) => {
      const item = node("li", "claim-item");
      item.appendChild(node("strong", "", claim.text || "Claim sin texto"));
      const sources = claim.sources || [];
      if (sources.length) {
        item.appendChild(node("span", "claim-src", sources.map((url) => sourceLabel(run, url)).join(" · ")));
      } else {
        item.appendChild(node("span", "claim-src", "sin fuente"));
      }
      list.appendChild(item);
    });
  }

  function checkLabel(key) {
    const labels = {
      safety_instruction: "Seguridad de la instrucción",
      draft: "Borrador presente",
      sources_present: "Fuentes presentes",
      freshness: "Frescura",
      independent_outlets: "Medios independientes",
      no_filler: "Sin relleno",
      must_include: "Debe incluir",
      must_not: "No debe incluir",
      instruction_focus: "Foco de la instrucción",
      claims_sourced: "Claims con fuente",
      numbers_grounded: "Cifras respaldadas",
      prompt_injection: "Inyección de instrucciones",
      duplicate: "Duplicados",
      format: "Formato",
      not_edited: "Sin edición posterior",
    };
    return labels[key] || key.replace(/_/g, " ");
  }

  function renderChecks(run) {
    const list = $("stampChecks");
    clear(list);
    const stamp = run && run.stamp;
    const checks = (stamp && stamp.checks) || {};
    const keys = Object.keys(checks);
    $("checksCount").textContent = keys.length ? keys.length + " controles" : "—";
    if (!keys.length) {
      list.appendChild(node("li", "empty", "Sin controles todavía."));
      return;
    }
    keys.forEach((key) => {
      const check = checks[key] || {};
      const row = node("li", "check-row " + (check.ok ? "ok" : (stamp && stamp.color === "red" ? "bad" : "warn")));
      row.appendChild(node("span", "mark", check.ok ? "✓" : (stamp && stamp.color === "red" ? "×" : "!")));
      const copy = node("div");
      copy.appendChild(node("strong", "", checkLabel(key)));
      if (check.detail) copy.appendChild(node("span", "detail", check.detail));
      row.appendChild(copy);
      list.appendChild(row);
    });
  }

  function renderStamp(run) {
    const card = $("stampCard");
    const stamp = run && run.stamp;
    const color = stamp && stamp.color ? stamp.color : "idle";
    card.className = "stamp stamp-" + color;
    const dot = $("stampDot");
    dot.className = "stamp-dot";
    const title = $("stampTitle");
    const summary = $("stampSummary");
    const reasons = $("stampReasons");
    clear(reasons);
    if (!stamp) {
      title.textContent = "Sin verificar";
      summary.textContent = "Investiga y redacta para emitir un sello.";
      return;
    }
    const labels = { green: "Verde", amber: "Ámbar", red: "Rojo" };
    title.textContent = labels[color] || "Sin verificar";
    summary.textContent = stamp.reasons && stamp.reasons.length ? stamp.reasons[0] : "Sin observaciones.";
    (stamp.reasons || []).slice(1).forEach((reason) => reasons.appendChild(node("li", "", reason)));
  }

  function renderRedTeam(run) {
    const list = $("redTeam");
    clear(list);
    const stamp = run && run.stamp;
    if (!stamp) {
      list.appendChild(node("li", "empty", "Sin hallazgos."));
      return;
    }
    const checks = stamp.checks || {};
    const failed = Object.keys(checks).filter((key) => checks[key] && !checks[key].ok);
    if (!failed.length) {
      list.appendChild(node("li", "ok", "Sin hallazgos críticos: todos los controles pasan."));
      return;
    }
    failed.forEach((key) => {
      const check = checks[key] || {};
      const kind = stamp.color === "red" ? "bad" : "warn";
      list.appendChild(node("li", kind, checkLabel(key) + (check.detail ? ": " + check.detail : "")));
    });
  }

  function publishBlockReason() {
    const run = state.run;
    const text = $("draftText").value.trim();
    if (!run || !run.stamp) return "Primero investiga y redacta: Publicar se habilita cuando hay un borrador verificado.";
    if (!text) return "El borrador está vacío: escribe o genera un texto antes de publicar.";
    if (state.dirty) return "Hay ediciones sin verificar. Pulsa «Verificar cambios» y después «Aprobar borrador».";
    if (!run.stamp.publishable) return "El sello editorial bloquea la publicación. Revisa los controles fallidos en Evidencia.";
    if (!state.approved) return "Falta tu aprobación: pulsa «Aprobar borrador» y después «Publicar».";
    return "";
  }

  function renderPublishGate() {
    const box = $("publishGate");
    const copy = $("publishGateText");
    const reason = publishBlockReason();
    const ready = !reason;
    const message = ready
      ? "Aprobado. Pulsa «Publicar» y confirma «Publicar ahora» en el diálogo."
      : reason;
    if (copy.textContent !== message) copy.textContent = message;
    box.classList.toggle("is-ready", ready);
    box.classList.toggle("is-blocked", !ready);
    const button = $("publishBtn");
    button.classList.toggle("is-gated", !ready);
    button.setAttribute("aria-disabled", ready ? "false" : "true");
  }

  function showPublishBlocked(reason) {
    renderPublishGate();
    toast(reason);
    const box = $("publishGate");
    box.classList.remove("is-nudge");
    void box.offsetWidth;
    box.classList.add("is-nudge");
    box.focus();
  }

  function renderDraft(run) {
    const draft = run && run.draft;
    const text = (draft && draft.text) || "";
    const area = $("draftText");
    if (!state.dirty && area.value !== text) area.value = text;
    const displayText = state.dirty ? area.value : text;
    state.lastText = text;
    $("postBody").textContent = displayText || "El borrador aparecerá aquí.";
    $("charCount").textContent = displayText.length + " / 2000";
    const stateLabel = $("draftState");
    stateLabel.className = "";
    if (!text) {
      stateLabel.textContent = "Sin borrador";
    } else if (state.dirty) {
      stateLabel.textContent = "Editado: verifica antes de publicar";
      stateLabel.classList.add("is-dirty");
    } else if (state.approved) {
      stateLabel.textContent = "Aprobado";
    } else {
      stateLabel.textContent = "Borrador verificado";
    }
    $("verifyBtn").hidden = !state.dirty || !(run && run.run_id);
    $("approveBtn").disabled = !(run && run.stamp && run.stamp.publishable) || state.dirty || !text;
    $("copyBtn").disabled = !text;
    renderPublishGate();
  }

  function renderAudit(runs) {
    const list = $("auditList");
    clear(list);
    if (!runs || !runs.length) {
      list.appendChild(node("p", "empty", "Sin ejecuciones."));
      return;
    }
    runs.forEach((run) => {
      const button = node("button", "audit-item");
      button.type = "button";
      button.appendChild(node("span", "audit-dot " + (run.stamp_color || "")));
      const copy = node("span", "audit-copy");
      copy.appendChild(node("strong", "", run.instruction || "Sin instrucción"));
      copy.appendChild(node("span", "", (run.mode || "editorial") + " · " + (run.stamp_color || "sin sello")));
      button.appendChild(copy);
      button.appendChild(node("span", "audit-time", prettyDate(run.created_at)));
      button.addEventListener("click", () => loadRun(run.id));
      list.appendChild(button);
    });
  }

  function renderRun(run) {
    state.run = run;
    state.dirty = false;
    state.approved = false;
    if (!run) return;
    state.activeAngle = (run.draft && run.draft.angle_id) || state.activeAngle;
    renderBrief(run);
    renderSummary(run);
    renderAngles(run);
    renderSources(run);
    renderClaims(run);
    renderStamp(run);
    renderChecks(run);
    renderRedTeam(run);
    renderDraft(run);
    $("modeBadge").textContent = ({ editorial: "Editorial", autonomous: "Autónomo", research: "Investigación", ghost: "Ghost" })[run.mode] || "Editorial";
  }

  function setBusy(value, button, label) {
    state.busy = value;
    document.body.classList.toggle("is-busy", value);
    if (!button) return;
    const span = button.querySelector("span");
    if (value) {
      if (span) {
        button.dataset.label = span.textContent;
        span.textContent = label || "Trabajando…";
      } else {
        button.dataset.label = button.textContent;
        button.textContent = label || "Trabajando…";
      }
    } else if (button.dataset.label) {
      if (span) span.textContent = button.dataset.label;
      else button.textContent = button.dataset.label;
      delete button.dataset.label;
    }
  }

  function formData() {
    return {
      instruction: $("instruction").value.trim(),
      mode: $("mode").value,
      freshness_hours: Number($("freshness").value) || 48,
      min_sources: Number($("minSources").value) || 2,
      max_length: Number($("maxLength").value) || 900,
      tone: $("tone").value.trim(),
      audience: $("audience").value.trim(),
      must_include: $("mustInclude").value.split(",").map((item) => item.trim()).filter(Boolean),
      must_not: $("mustNot").value.split(",").map((item) => item.trim()).filter(Boolean),
    };
  }

  function showFormError(message) {
    const box = $("formError");
    box.textContent = message || "";
    box.hidden = !message;
  }

  async function runDesk(researchOnly) {
    if (state.busy) return;
    const payload = formData();
    if (!payload.instruction) {
      showFormError("Escribe una instrucción antes de investigar.");
      $("instruction").focus();
      return;
    }
    showFormError("");
    if (state.demo) {
      toast("Modo demo: la interfaz no llama al servidor.", "ok");
      return;
    }
    const button = researchOnly ? $("researchBtn") : $("runBtn");
    setBusy(true, button, researchOnly ? "Investigando…" : "Investigando y redactando…");
    $("draftStatus").textContent = researchOnly ? "Leyendo fuentes…" : "Leyendo fuentes y redactando. Puede tardar un minuto.";
    $("draftStatus").className = "inline-status";
    try {
      const result = await api(researchOnly ? "/api/research" : "/api/run", { method: "POST", body: JSON.stringify(payload) });
      renderRun(result);
      if (result.error) {
        toast(result.error, "error");
        $("draftStatus").textContent = result.error;
        $("draftStatus").className = "inline-status is-error";
      } else if (researchOnly) {
        toast("Investigación lista. Elige un ángulo y redacta.", "ok");
        $("draftStatus").textContent = "Investigación lista. Pulsa un ángulo para redactar.";
        $("draftStatus").className = "inline-status is-ok";
      } else {
        toast(result.stamp && result.stamp.color === "green" ? "Borrador verificado con sello verde." : "Borrador listo; revisa el sello.", "ok");
        $("draftStatus").textContent = "Borrador listo.";
        $("draftStatus").className = "inline-status is-ok";
      }
      await loadAudit();
    } catch (err) {
      const message = err && err.message ? err.message : "No se pudo completar la investigación.";
      showFormError(message);
      toast(message, "error");
      $("draftStatus").textContent = message;
      $("draftStatus").className = "inline-status is-error";
    } finally {
      setBusy(false, button);
    }
  }

  async function selectAngle(angleId) {
    if (state.busy || !state.run) return;
    if (angleId === state.activeAngle) return;
    if (state.dirty) {
      toast("Verifica o descarta los cambios del borrador antes de cambiar de ángulo.", "error");
      return;
    }
    state.activeAngle = angleId;
    renderAngles(state.run);
    if (state.demo) {
      toast("Modo demo: ángulo seleccionado.", "ok");
      return;
    }
    if (!state.run.run_id || !(((state.run.dossier || {}).sources) || []).length) return;
    setBusy(true);
    $("draftStatus").textContent = "Redactando el ángulo " + angleId + "…";
    try {
      const result = await api("/api/draft", { method: "POST", body: JSON.stringify({ run_id: state.run.run_id, angle_id: angleId }) });
      if (result.ok === false) {
        toast(result.error || "No se pudo redactar este ángulo.", "error");
        return;
      }
      state.run.draft = result.draft;
      state.run.stamp = result.stamp;
      state.dirty = false;
      state.approved = false;
      renderClaims(state.run);
      renderStamp(state.run);
      renderChecks(state.run);
      renderRedTeam(state.run);
      renderDraft(state.run);
      toast("Borrador actualizado para " + angleId + ".", "ok");
      await loadAudit();
    } catch (err) {
      toast(err.message || "No se pudo redactar este ángulo.", "error");
    } finally {
      setBusy(false);
    }
  }

  async function verifyEdits() {
    if (!state.run || !state.run.run_id || state.busy) return;
    if (state.demo) {
      toast("Modo demo: la verificación no llama al servidor.", "ok");
      return;
    }
    const text = $("draftText").value.trim();
    if (!text) {
      toast("El borrador está vacío.", "error");
      return;
    }
    setBusy(true, $("verifyBtn"), "Verificando…");
    try {
      const result = await api("/api/verify", { method: "POST", body: JSON.stringify({ run_id: state.run.run_id, text, angle_id: state.activeAngle }) });
      state.run.stamp = result.stamp;
      state.run.draft = { ...(state.run.draft || {}), text };
      state.dirty = false;
      state.approved = false;
      renderStamp(state.run);
      renderChecks(state.run);
      renderRedTeam(state.run);
      renderDraft(state.run);
      toast(result.stamp && result.stamp.color === "green" ? "Texto verificado." : "El texto editado no obtiene sello verde; regenera el borrador.", result.stamp && result.stamp.color === "green" ? "ok" : "error");
    } catch (err) {
      toast(err.message || "No se pudo verificar.", "error");
    } finally {
      setBusy(false, $("verifyBtn"));
    }
  }

  function approveDraft() {
    if (!state.run || !state.run.stamp || !state.run.stamp.publishable || state.dirty) return;
    state.approved = true;
    renderDraft(state.run);
    toast("Borrador aprobado. Pulsa «Publicar» y confirma «Publicar ahora».", "ok");
  }

  function openPublish() {
    const reason = publishBlockReason();
    if (reason) {
      showPublishBlocked(reason);
      return;
    }
    $("publishQuote").textContent = $("draftText").value.trim();
    $("publishDialog").showModal();
  }

  async function confirmPublish(event) {
    event.preventDefault();
    if (state.demo) {
      $("publishDialog").close();
      toast("Modo demo: no se publica nada.", "ok");
      return;
    }
    const text = $("draftText").value.trim();
    const button = $("publishConfirm");
    button.disabled = true;
    button.textContent = "Encolando…";
    try {
      const result = await api("/api/publish", {
        method: "POST",
        body: JSON.stringify({ run_id: state.run.run_id, text, approve: true }),
      });
      $("publishDialog").close();
      const message = result.queued
        ? "Encolado: el scheduler publicará en segundos. Revisa tu muro."
        : (result.message || (result.published ? "Publicado." : "Aprobado y encolado."));
      toast(message, "ok");
    } catch (err) {
      toast(err.message || "No se pudo publicar.", "error");
    } finally {
      button.disabled = false;
      button.textContent = "Publicar ahora";
    }
  }

  async function copyDraft() {
    const text = $("draftText").value;
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast("Texto copiado.", "ok");
    } catch (err) {
      $("draftText").select();
      document.execCommand("copy");
      toast("Texto copiado.", "ok");
    }
  }

  async function loadAudit() {
    if (state.demo) {
      renderAudit((demoData().audit || []).slice(0, 8));
      return;
    }
    try {
      const result = await api("/api/audit?limit=8");
      renderAudit(result.runs || []);
    } catch (err) {
      renderAudit([]);
    }
  }

  async function loadRun(runId) {
    if (state.busy) return;
    if (state.demo) {
      const demo = demoData();
      if (runId === demo.run_id) renderRun(demo);
      else toast("Modo demo: ejecución de ejemplo.", "ok");
      return;
    }
    setBusy(true);
    try {
      const run = await api("/api/run?id=" + encodeURIComponent(runId));
      renderRun(run);
      $("draftStatus").textContent = "Ejecución cargada.";
      $("draftStatus").className = "inline-status";
    } catch (err) {
      toast(err.message || "No se pudo cargar la ejecución.", "error");
    } finally {
      setBusy(false);
    }
  }

  function bind() {
    $("assignmentForm").addEventListener("submit", (event) => {
      event.preventDefault();
      runDesk(false);
    });
    $("researchBtn").addEventListener("click", () => runDesk(true));
    $("instruction").addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        runDesk(false);
      }
    });
    $("mode").addEventListener("change", () => {
      $("modeBadge").textContent = $("mode").selectedOptions[0].textContent;
    });
    $("draftText").addEventListener("input", () => {
      const text = $("draftText").value;
      state.dirty = text !== state.lastText;
      state.approved = false;
      $("postBody").textContent = text || "El borrador aparecerá aquí.";
      $("charCount").textContent = text.length + " / 2000";
      renderDraft(state.run);
    });
    $("approveBtn").addEventListener("click", approveDraft);
    $("verifyBtn").addEventListener("click", verifyEdits);
    $("copyBtn").addEventListener("click", copyDraft);
    $("publishBtn").addEventListener("click", openPublish);
    $("auditRefresh").addEventListener("click", loadAudit);
    $("publishConfirm").addEventListener("click", confirmPublish);
    $("publishDialog").addEventListener("close", () => {
      $("publishConfirm").disabled = false;
      $("publishConfirm").textContent = "Publicar ahora";
    });
    window.addEventListener("keydown", (event) => {
      if (event.key === "Escape" && $("publishDialog").open) $("publishDialog").close();
    });
  }

  async function init() {
    bind();
    const instruction = new URLSearchParams(location.search).get("instruction");
    if (instruction) $("instruction").value = instruction.slice(0, 2000);
    if (state.demo) {
      const demo = demoData();
      renderRun(demo);
      renderAudit(demo.audit);
      setServer("is-demo", "Modo demo");
      $("draftStatus").textContent = "Datos de demostración. No se llama al servidor.";
      return;
    }
    await checkHealth();
    await loadAudit();
  }

  init();
})();
