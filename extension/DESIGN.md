# CXT Popup — DESIGN.md (design contract)

Target: `extension/popup.html` (Chrome MV3 popup, single file, 380px fixed width, `main` scrolls at `max-height:488px`).
Authority: distilled from `~/.cxt/.omo/plans/premium-ui-redesign.md` (the decision-complete plan; §4 tokens, §5 components, §6 motion, §7 signature details, §9 JS contract). Any future UI change must update this file first.

**Design thesis:** the remote control of an expensive instrument. Near-black obsidian dark theme (flagship) built from luminance steps + translucent-white hairlines — not drop shadows; one restrained indigo-violet accent spent only where the operator acts; three-tone status language (emerald / amber / crimson) as **washes with colored glyphs**, never saturated pills; tabular numerals wherever numbers tick.

**Canvas math (load-bearing):** header 42 + nav 67 + main 488 = **597px ≤ 600px** Chrome popup ceiling. Never grow header/nav without re-checking `main{max-height}`.

---

## 0. Research log / references actually used

| Reference | Role |
|---|---|
| `references/design/soft-skill.md` (Layer A, full read) | Premium/expensive material direction: custom cubic-beziers, haptic micro-aesthetics, Double-Bezel nesting (scaled to micro: seg shell/core, log enclosure, logo tile), atmosphere, banned defaults. |
| `references/design/linear.app.md` (Layer B, full read) | Token source of truth: indigo-violet accent ramp, dark-first neutral stack, hairline depth model ("no drop shadows on dark"), 3-tier weight system (400/510/590), dense technical register. Chosen over supabase (emerald identity would fight ok/bad semantics) and vercel (no status-color story). |
| `references/design/interaction-skill.md` | Motion mechanics: meaning→motion mapping (M1–M9), springs for spatial moves, easings for tints, reduce-not-remove contract, GPU-composited only. |
| `references/design/redesign-skill.md` | Audit-first workflow (findings A1–A14 drove every replacement); anti-pattern list (uniform radii, generic card+shadow, accent side-borders). |
| `Lazyweb / Imagen / StyleGallery` | Not used — not available in this run; noted honestly. Layer A + Layer B were the research lanes that ran. |

**Retired (banned):** `#2563eb` (stock blue), `#7c3aed` (blue→purple AI gradient), `system-ui`-only type on Linux, uniform 14/10px radii, one reused untinted shadow, `▸/▾` glyph-swap chevrons, emojis as icons, colored accent side-borders, font weights 700/800.

**Font decision — Geist + Geist Mono, bundled.** SIL OFL 1.1 (license shipped at `extension/fonts/LICENSE-OFL.txt`). Variable `.woff2` (`Geist-var.woff2`, `GeistMono-var.woff2`) so weights 400/510/590 are exact, not synthesized; `system-ui` on Linux falls back to Noto/DejaVu (the A5 finding) and would forfeit the premium read. CSP-safe: same-origin `@font-face` + `<link rel="preload" as="font" type="font/woff2" crossorigin>`, no CDN — the popup works offline. `font-display:swap`; extension package cost ~180–260KB. Fallback stacks (`--font-ui`/`--font-mono`) stay valid if the binaries are ever removed.

---

## 1. Color tokens (single source: `popup.html` `:root` blocks)

`--ok`, `--warn`, `--bad`, `--acc` names are **JS-frozen** (`popup.js` writes `#dot.style.background = "var(--bad)"` / `"var(--ok)"`). Values may change; names may never.

### Dark (flagship — `@media (prefers-color-scheme:dark)`)

| Token | Value | Role |
|---|---|---|
| `--bg-page` | `#08090a` | Canvas |
| `--bg-surface` | `#0f1011` | Cards |
| `--bg-elevated` | `#191a1b` | Active seg core, popovers |
| `--bg-sunken` | `#060708` | Wells: inputs, seg shell, rows, log frame, pill |
| `--line-hairline` | `rgba(255,255,255,.06)` | Card borders, separations |
| `--line` | `rgba(255,255,255,.08)` | Input borders |
| `--line-strong` | `rgba(255,255,255,.14)` | Input hover, scrollbar thumbs |
| `--ink` / `--ink-2` / `--mut` / `--ink-3` | `#f7f8f8` / `#d0d6e0` / `#8a8f98` / `#62666d` | Text / body / labels / placeholders |
| `--acc` / `--acc-hover` / `--acc-strong` / `--acc-press` | `#7170ff` / `#828fff` / `#5e6ad2` / `#515cc9` | Interactive accent / highlight / button fill / pressed |
| `--acc-soft` / `--acc-wash` / `--on-acc` | `rgba(113,112,255,.16)` / `rgba(113,112,255,.09)` / `#ffffff` | Badge fill + ring / ambient glow / label-on-accent |
| `--ok` / `--ok-soft` | `#10b981` / `rgba(16,185,129,.14)` | Success glyph / chip wash |
| `--warn` / `--warn-soft` | `#d9a441` / `rgba(217,164,65,.14)` | Warning |
| `--bad` / `--bad-soft` | `#e5484d` / `rgba(229,72,77,.15)` | Danger |

### Light ("Daylight" — `:root` defaults, identical geometry)

| Token | Value | Role |
|---|---|---|
| `--bg-page` / `--bg-surface` / `--bg-elevated` / `--bg-sunken` | `#f7f8f8` / `#ffffff` / `#ffffff` / `#eef0f3` | Canvas / cards / active core (shadow-carried) / wells |
| `--line-hairline` / `--line` / `--line-strong` | `#e8eaef` / `#dfe2e8` / `#c9cdd6` | Separations / input borders / input hover |
| `--ink` / `--ink-2` / `--mut` / `--ink-3` | `#0b0c0e` / `#3d4249` / `#686d76` / `#9aa0a8` | Never pure `#000` |
| `--acc` / `--acc-hover` / `--acc-strong` / `--acc-press` | `#4b50c8` / `#4044b5` / `#4b50c8` / `#353aa2` | Accent darkens on hover (inverse of dark) |
| `--acc-soft` / `--acc-wash` / `--on-acc` | `rgba(75,80,200,.12)` / `rgba(75,80,200,.06)` / `#ffffff` | |
| `--ok` / `--ok-soft` | `#0e7a4a` / `#e6f4ec` | |
| `--warn` / `--warn-soft` | `#8f6110` / `#f7efdb` | Burnt amber, not orange |
| `--bad` / `--bad-soft` | `#c22b33` / `#fae9ea` | |

One cool-neutral gray family in both themes (fixes the warm/cool mixing). Accent saturation < 80%.

### Component-only tokens (no raw color outside `:root`)

Same values carry per theme: `--hover-wash` (`rgba(11,12,14,.03)` light / `rgba(255,255,255,.03)` dark), `--dot-halo`, `--power-shade`, `--power-on-wash`/`--power-on-line`, `--power-off-wash`/`--power-off-line`, `--logo-bg`, `--logo-shadow`, `--btn-shadow`, `--btn-sec-bg`(`-hover`), `--btn-stop-ring`/`--btn-stop-bg-hover`, `--danger-line`, `--danger-card-bg`, `--alert-line`, `--console-shade`, `--grain-op`. The only raw colors allowed outside `:root`: the two annotated data-URI select-chevron strokes (`%23686d76` light / `%238a8f98` dark — SVG cannot read CSS vars) and the logo SVG `stroke="#fff"` (markup attribute, plan §5.3).

## 2. Elevation / materials

- `--card-bg` — dark: `linear-gradient(180deg, rgba(255,255,255,.02), rgba(255,255,255,0) 42%), #0f1011` (top sheen + panel); light: `#ffffff`.
- `--card-shadow` — dark: `inset 0 1px 0 rgba(255,255,255,.04)` (top-light; **no drop shadow on dark** — Linear); light: `0 1px 2px rgba(15,18,30,.04), 0 1px 1px rgba(15,18,30,.03)`.
- `--well-shade` — recessed inputs/rows: dark `inset 0 1px 2px rgba(0,0,0,.4)`; light `inset 0 1px 2px rgba(15,18,30,.04)`.
- `--ring` — focus: `0 0 0 1px var(--acc), 0 0 0 4px var(--acc-soft)` (light widens the soft alpha to `rgba(75,80,200,.18)`).
- `--elev-2` — reserved for popovers/alerts (defined, currently unused).
- `--grad-acc` (buttons): dark `180deg #7b7aff → #6667f2`; light `180deg #5559d6 → #4b50c8`. `--grad-bar` (progress): dark `90deg #5e6ad2 → #828fff`; light `90deg #4b50c8 → #7a7ff0`.
- `--knob` / `--knob-shadow` — ivory physical knob + inset highlight.

## 3. Typography

| Token | Value | Use |
|---|---|---|
| `--font-ui` | `"Geist", ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif` | All UI |
| `--font-mono` | `"Geist Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace` | Console |
| Weights | `400` read · `510` UI emphasis · `590` announce/buttons/labels-on-accent. **No 700/800 anywhere.** | Variable-font values |
| `--fs-brand` | 13.5px / 590 / `-.01em` | `h1` |
| `--fs-h2` | 10px / 590 / uppercase / `.12em` | Card section rule |
| `--fs-label` | 10.5px / 510 / `.01em` | Field labels |
| `--fs-body` | 12.5px / 400 / 1.55 | Base |
| `--fs-small` | 11px | Hints, pill, summaries |
| `--fs-micro` | 9px / 590 / tabular | Badges |
| `--fs-btn` | 12px / 590 / `.01em` | Buttons |
| `--fs-console` | 10.5px mono / 1.6 | `pre.console` |
| Numerals | `font-variant-numeric:tabular-nums` on `.status`, `.badge`, console, rows | Stops counter/ETA jitter |

## 4. Spacing & radius

- Spacing scale (4px base with optical half-steps): `--sp-1..10` = 2/4/6/8/10/12/14/16/20/24px. Component geometry uses the plan's per-component values (header `10px 12px`, nav `0 8px`, main `12px`, card `14px` + `10px` gap, label `10px 0 5px`, btnrow `8px`/`12px`).
- Radius hierarchy: `--r-xs:4px` (chips) · `--r-sm:6px` (buttons/inputs/logo tile/row quick-actions) · `--r-md:8px` (rows, seg shell, log frame, alerts) · `--r-card:10px` (cards — the single soft step) · `--r-pill:999px` (pill, power capsule, bar, `.ic`, dot, badges, scrollbar thumbs).

## 5. Component primitives

1. **Canvas** — `body` 380px, layered atmosphere `radial-gradient(420px 190px at 30% -50px, var(--acc-wash), transparent 68%)` over `--bg-page`; fixed `body::after` feTurbulence grain (`--grain-op` .015 light / .022 dark), `pointer-events:none`, never attached to a scrolling container.
2. **Header (42px, sticky, z-5)** — `color-mix(in srgb, var(--bg-page) 86%, transparent)` + `backdrop-filter: blur(14px) saturate(1.4)`; `@supports not (backdrop-filter:blur(1px))` → solid `--bg-page`; hairline bottom. Sticky + blur is the only blurred surface in the popup.
3. **Logo tile (24px)** — machined `--r-sm` tile, `--logo-bg` (indigo ramp, **not** the banned 135° blue→purple), `--logo-shadow` (inner bevel + hairline ring + small drop), inline SVG "C" aperture `M9.6 3.4a4.35 4.35 0 1 0 .01 5.2`, `stroke #fff` 1.7, `stroke-linecap:round`.
4. **Connection pill** — recessed capsule (`--bg-sunken` + `--well-shade`), `--fs-small`/510/`--ink-2`; `#conn` ellipsizes (`min-width:0` on the span). `#dot` 7px, background JS-owned (`var(--ok)`/`var(--bad)`), neutral halo `--dot-halo`, `background-color` crossfade 140ms.
5. **Power toggle (#power)** — 118×26 capsule. JS owns `textContent` (`"● ACTIVADA"/"● DESACTIVADA"`) and `.off` → **styling only via CSS + `::after`** (never child elements). Track: `--bg-sunken` + `--power-on-wash`/`--power-on-line` (green) vs `.off` red variants; label `--ok`/`--bad`, 10px/590/`.04em`, left padding 27px (the `●` prints as the switch lamp). Knob: 18px `--knob` + `--knob-shadow`, spring travel `translateX(92px)` when `:not(.off)` (= 118 − 2 borders − 2×3 inset − 18). Hover `brightness(1.06)`; press `scale(.97)`; focus `--ring`.
6. **Nav (67px)** — 6 equal tabs, 10.5px/510, `--mut`; hover `--hover-wash` + `--ink-2`; active `.on` = `--ink`/590 + 2px `--acc` indicator (10px inset, `tab-in` entrance, `transform-origin:center`). Label "Automatización" shortened to "Auto" (A11 fix; `data-tab="auto"` unchanged).
7. **Badge** — wash chip, **no children ever** (JS overwrites `textContent` each tick): `--acc-soft` fill, `--acc-hover` text, `--acc-wash` ring, `--fs-micro` tabular. Pop animation only on `display:none→block` (`badge-pop`), so the 2s tick never re-triggers it.
8. **Card + section rule** — `--card-bg` + hairline border + `--card-shadow`; `h2` = uppercase micro-label followed by a flex hairline rule (`h2::after`), not a box. Danger card (`.card danger`) is the **only** tinted card: `--danger-line` border, `--danger-card-bg` wash, red `h2`.
9. **Fields** — inputs/selects/textareas: `--bg-sunken` well + `--well-shade`, `--line` border, hover `--line-strong`, focus `--acc` border + `--ring`; placeholders `--ink-3`; native select removed (`appearance:none`) with data-URI chevron (per-theme stroke); checkbox `accent-color:var(--acc)`, 14px, `min-height:0` (input rule's 30px min-height must not inflate it).
10. **Segmented control (.seg)** — Double-Bezel at micro scale: shell (`--bg-sunken` + hairline + `--well-shade`, radius `--r-md`) over core (`.on` = `--bg-elevated` + hairline ring + `--card-shadow`, radius `--r-sm`).
11. **Buttons** — primary `--grad-acc` fill, `--on-acc` 590 label, `--btn-shadow`; hover `brightness(1.1)`; press `translateY(.5px) scale(.985)`. Secondary: `--btn-sec-bg` wash + inset hairline. Destructive (`.btn.stop`): `--bad-soft` wash + `--bad` text + `--btn-stop-ring` inset hairline — **no saturated red slab**. Queue-row buttons override to 24×24 grid quick-actions.
12. **Progress bar** — 5px pill track (`--line-hairline`, `overflow:hidden`), fill `--grad-bar` + `--acc-soft` glow, `width` transition 420ms (see Motion exception). `<i>` stays the first element child.
13. **Result rows** — sunken `--r-md` rows with hairline; `.ic` = 16px wash chip with colored glyph (`--ok-soft`/`--bad-soft`/`--warn-soft` + matching ink), glyphs `✓ ✗ •` are JS text (typographic, not emoji); `li.hint` = borderless centered `--ink-3` empty state.
14. **Status blocks** — 12px/1.6 `--ink-2`, `pre-wrap`, tabular numerals; pure text sinks, JS-owned.
15. **Console (details.log + pre.console)** — hairline enclosure on `--bg-sunken`; summary 11px/510 with a single rotating chevron (`summary::before` 7px borders, −45°→45°, no `▸/▾` glyphs); `pre.console` uses `--console-bg`/`--console-ink` (dark in **both** themes — deliberate, see Debt), `--console-shade` top hairline, styled scrollbars; fade-in on `[open]`.
16. **Error alert (#aerr)** — CSS default `display:block` (JS toggles inline display), `--bad-soft` wash, `--alert-line` tinted hairline, `--bad` text 11px/510. No side borders.
17. **Label picker — Etiquetas (`.picks`)** — searchable catalogue of group-filter labels with their group counts (e.g. `playa del carmen` 119), placed in the Compartir card between `#segshare` and the active sub-mode block (`#m_post`/`#m_text`) so one block serves both modes. Purpose: a job's filter is comma-split and each token must be a substring of the accent-stripped group name — one typo zeroes the job; chips write canonical labels into the ACTIVE filter field (`#pfilter` post / `#gfilter` text). Anatomy: `.picks-head` (`.picks-t` title in the field-label register + `#picksmeta` right-aligned, `--fs-small`/`--mut`/tabular, `N etiquetas · YYYY-MM-DD`); `.picks-tools` (compact `#picksq` `type=search` in the standard well; secondary-wash `.picks-btn`s `#picksall` "ver todas"/"ver menos" — hidden when ≤12 labels — and `#picksclear` "limpiar"); `.picks-row#picksrow` flex-wrap chip cluster. `.chip` = `--r-pill` pill on the seg-shell material (`--bg-sunken`+`--line-hairline`+`--well-shade`), `--fs-small` label span + `.chip-c` count (10px, `--ink-3`, `font-variant-numeric:tabular-nums`); selected `.chip.on` reuses the badge material exactly (`--acc-soft` fill, `--acc-hover` text, transparent border + `0 0 0 1px var(--acc-wash)` ring, count at .72 opacity) — no saturated fills, no side borders. States: default = top 12 by count (server order is count-desc), no scroll, no entrance animation; `#picksq` filters by case/accent-insensitive substring (same `norm` as the engine: trim + lower + NFD strip), no match → `--ink-3` "sin coincidencias" line; `#picksall` toggles `.picks.expanded` → ALL labels in a 120px scrollable `.picks-row` (thin `--line-strong` scrollbar; `main` still owns the popup height budget, the row never grows it); `#picksclear` renders only when ≥1 label token is selected (`.picks.has-sel`); empty/older-daemon = `.picks.empty` (tools hidden; `#picksmeta` = `sin etiquetas — escanea grupos` when the array is empty, `sin etiquetas` when the key is missing; no chips; never throws); `:disabled` mirrors `.btn:disabled` (reserved). Selection truth is the active input's comma-split value compared to chip labels with the same `norm` (equality, accents stripped); a click rewrites the input as free (non-label) tokens in user order, then selected labels in display order, joined `", "`, then syncs `form[key]` + `chrome.storage` by hand (programmatic `.value` fires no `input` event); each mode derives its own selection on `#segshare` switch. Keyboard: chips are `<button>`s (Enter/Space) with the global `:focus-visible` `--ring`. Motion: M10 — tint crossfades only, no entrance, no loop (the 2s tick forbids decoration here). Data contract: `groupscanGet` → `labels:[{label,count,category,examples}]` (render uses `label`+`count`), `labels_total`, `labels_generated_at`. Rebuild discipline: `renderPicks()` re-fetches, rebuilds chip DOM only when the catalogue/query/expanded signature changes, re-applies `.on` only when the active input value changes — idempotent inside `tick()`. Data refresh (sin caché negativo): con catálogo, `renderPicks()` re-pide en cada invocación y repinta en cuanto llega la respuesta; si el último fetch no dejó etiquetas, reintenta como máximo 1 vez cada ~5s — el `tick` de 2s no martilla al daemon y el estado vacío nunca es permanente (un fetch exitoso siempre repinta). El modal en página aplica la misma política de forma independiente: solo cachea una respuesta con `labels` no vacío (TTL 60s); un fallo o `labels:[]` nunca se guarda como caché; abrir el modal sin caché válida re-pide; y tras un fallo pinta el vacío y reintenta exactamente una vez a los ~1.5s (sin bucle, una sola petición en vuelo).

18. **Simulación — contrato de formularios (opt-in)** — `#pdry` (Compartir post), `#gdry` (Compartir texto), `#cdry` (Comentar) y `.f-dry` (modal en página) son OPT-IN: llegan sin marcar por defecto y jamás se restauran marcadas (`bindFields()` fuerza `false` para todo campo `*dry`, una casilla encendida de una sesión anterior no puede reactivarse sola; `openModal()` resetea `.f-dry` en cada apertura). Desmarcada = publica de verdad (pasa por `needConfirm(false)`); marcada = pasada en simulación, todavía disponible. Visibilidad post-hoc (el no-op silencioso queda prohibido): la fila de la cola de Automatización dice SIEMPRE el estado real en español — `· SIMULACIÓN (no publica)` si el item es dry, `· SIMULACIÓN completada (no publicó)` cuando acabó un dry, `· ya publicado (N grupos)` para `state:"hecho"` (N sale de `it.published`; sin el dato, `· ya publicado`), `· se omitirá: ya publicado (sin grupos nuevos)` cuando la url de un item no-error cruza un `results` con `state:"hecho"` (identidad por `fbid`/ruta, sin campos ni endpoints nuevos: solo `items`/`results` de `autopostState`) y `· error · intento N` en fallos; las filas de resultado de Compartir mantienen `[simulacro]` cuando el daemon expone `r.dry`.

## 6. Motion (M1–M10)

Tokens: `--dur-press:90ms` · `--dur-tint:140ms` · `--dur-enter:180ms` · `--dur-pop:220ms` · `--dur-bar:420ms`; eases `--ease-out` (arrivals), `--ease-snap` (press), `--ease-spring` (knob, badge — the only overshoot), `--ease-tint` (color fades).

| # | Trigger | Motion |
|---|---|---|
| M1 | Tab switch | Section `translateY(3px)→0` + fade (`--dur-enter`/`--ease-out`) |
| M2 | Tab active | Indicator `scaleX(.35)→1` + fade (`tab-in`, `--dur-pop`) |
| M3 | Share sub-mode flip (`#m_post`/`#m_text`) | fade-up (animation restarts on `display` flip) |
| M4 | Button press/hover | press `translateY(.5px) scale(.985)`; hover `brightness(1.1)`; secondary = bg wash |
| M5 | Power toggle | knob 92px spring (`--ease-spring`); label/border tint crossfade (`--dur-tint`) |
| M6 | Badge appears | `scale(.6)→1.06→1` pop (`badge-pop`, `--ease-spring`) |
| M7 | Progress fill | width eases (`--dur-bar`/`--ease-out`) |
| M8 | Log open/close | chevron −45°→45°; console fades up |
| M9 | Status lamp | `background-color` crossfade (`--dur-tint`) |
| M10 | Picker chip toggle / expand / clear | tint crossfades only (`--dur-tint`/`--ease-tint`) on background/color/border/shadow + the shared press haptic on chips (`--dur-press`/`--ease-snap`); expand is an unanimated re-render — no entrance, no loop (data refreshes every 2s) |

**GPU exception (single, documented):** `.bar > i` animates `width` because `popup.js:51` writes `style.width = …%` and the fill must stay the first element child — `transform:scaleX()` is unavailable without a JS edit (frozen). Mitigation: 5px-tall track, `overflow:hidden`, width-fixed, no sibling reflow. Every other motion is `transform`/`opacity`/`filter` only. `filter` transitions (button brightness) are compositor-safe tints.

**Reduced motion** (`@media (prefers-reduced-motion:reduce)`): badge pop, tab-in, knob travel, bar width and chevron transitions are removed; section/sub-mode entrances keep a 120ms opacity-only fade (`sec-in-r`) — reduce, not remove.

Rejected as slop (must not be added): looping pulses, shine sweeps, parallax, animated glows, hover wiggles, count-up animations (data updates every 2s and would fight the animation), decorative hovers with no state meaning.

## 7. Accessibility constraints

- Contrast: `--mut` labels ≈ 5.1:1 (light) / 5.4:1 (dark) on their surface; white on `--acc-strong` `#5e6ad2` = 4.67:1; accent text ≥ 5:1 on page; placeholders ≥ 3:1.
- Keyboard: every interactive element reachable; `:focus-visible` shows `--ring` (`button`, `summary`, inputs/selects via `:focus`); no focusable element may have `outline:0` without a ring replacement.
- `prefers-reduced-motion` honored (see §6). No autoplaying/looping animation exists anywhere.
- Numeric data uses `tabular-nums`; text is real text (selectable, screen-reader reachable); icons are SVG or typographic glyphs — **no emojis**.
- `.ic` chips carry a glyph (`✓/✗/•`) in addition to color (never color-only status).

## 8. Frozen JS contract (do not break)

`popup.js` was untouched by the premium redesign; the label picker (§5.17) is the one sanctioned addition (`renderPicks()` + chip wiring — every name below stays frozen). Never rename or remove: `#power #conn #dot #pill`; badge ids `#b_estado #b_pub #b_share #b_com #b_auto #b_grp`; section ids `#t_estado #t_publicar #t_compartir #t_comentar #t_auto #t_ajustes`; `#segshare` (`data-mode`), `#m_post #m_text`; `#aerr`; bar ids `#bar_pub #bar_share #bar_sharepost #bar_com #bar_grp` with `<i>` as first element child; `details.log > summary + pre.console` nesting with `pre.console` a direct child; every button/field id in the ledger (plan Annex B); `nav button[data-tab]` values; `.on`/`.show` class names; `ic ok/bad/warn`; `li.hint`; row `button.btn.sec/.stop`. Custom-property **names** `--ok --warn --bad --acc` are referenced from JS inline styles.

Known couplings: `#power` width (118px) and knob travel (92px) depend on the JS strings `"● ACTIVADA"/"● DESACTIVADA"` — if those strings change, re-tune width/travel. `#aerr` CSS default must stay `display:block` (JS clears inline display to show it). Badges never get child elements (JS writes `textContent`).

## 9. Accepted debt & deliberate decisions

1. **Dark console in both themes** — `pre.console` is dark even in light mode: a terminal viewport is an instrument, conventionally dark inside light developer tools. Deliberate; do not "fix".
2. **`.bar > i` width transition** — the documented GPU-rule exception (§6, reason above).
3. **Geist binaries** — ~180–260KB added to the extension package + OFL license must ship with it; removing the files degrades to the documented fallback stack without invalidating anything else.
4. **"Auto" nav label** — user-visible copy change (was "Automatización", which could not fit its 59px slot); `data-tab="auto"` and the badge child are untouched.
5. **Fixed-width power toggle** — geometry couples to the current JS label strings (see §8).
6. **`--elev-2` defined but unused** — reserved for future popovers; kept in the token contract.
7. **`color-scheme:light dark`** — lets native select dropdown / scrollbar chrome follow the theme; option rows are themed via `option{background:var(--bg-elevated)}`.
8. **Grain overlay** — fixed, `pointer-events:none`, 1.5–2.2% opacity; if it ever shows on a screenshot pipeline it is decorative and safe to zero via `--grain-op`.
9. **No Lighthouse run** — `chrome-extension://` popups are unreachable by Lighthouse; verification was manual (grep audits + state matrix in the plan §10) instead of faking a score.

## 10. Verification checklist (re-run after any edit)

1. `#2563eb`, `#7c3aed`, `▸`, `▾`, `border-left:`/`border-right:` absent from `popup.html`.
2. Zero hex/rgba outside the `:root` blocks except the two annotated select-chevron data-URIs (and the logo SVG `stroke="#fff"` attribute).
3. Every id/selector from popup.js (`grep -oE '\$\("[a-zA-Z0-9_]+"\)' popup.js | sort -u`) still exists in `popup.html`.
4. Height math: header 42 + nav (2×33+1) 67 + main ≤488 = 597 ≤ 600.
5. Both themes + `prefers-reduced-motion:reduce` pass over all 6 tabs and the state matrix (disconnected, idle, running, results, error, paused automation, queue).
6. Picker (§5.17): `#picksq #picksall #picksclear #picksmeta #picksrow` exist; `.chip-c` carries `font-variant-numeric:tabular-nums`; `.picks.expanded .picks-row` caps at `max-height:120px`.
7. Simulación opt-in: `pdry`/`gdry`/`cdry` y `.f-dry` sin `checked` en markup; `bindFields()` no restaura `*dry`; la fila de la cola de Automatización declara siempre su estado real (`SIMULACIÓN` / `ya publicado` / `se omitirá`).

---

## Scraping area contract

New area contract, fixed BEFORE the code (popup.html + popup.js + background.js). It appends to this document; it does not alter §8, and it invents no ids and no tokens beyond the ones listed here.

**(a) Navigation, two rows in one `<nav>`.** The `<nav>` (popup.html:144) changes from `display:flex` to `display:flex;flex-direction:column;gap:0;padding:0`, keeping its `border-bottom`. It gets exactly two direct children: `<div class="arearow">` and `<div class="subrow">`. Literal CSS: `.arearow,.subrow{display:flex;gap:2px;padding:0 8px}`, `.subrow[hidden]{display:none}`, `.subrow button[hidden]{display:none}`. `.arearow` holds 4 `<button type="button" data-area="estado|facebook|scraping|ajustes">` (text Estado/Facebook/Scraping/Ajustes); the `scraping` button carries `<span class="badge" id="b_scraping"></span>`. `.subrow` holds 7 `<button type="button" data-tab>` in this exact order: `estado, publicar, compartir, comentar, auto, grupos, ajustes`, with badges `b_estado, b_pub, b_share, b_com, b_auto, b_grp` (the `grupos` one) and **none** on `ajustes`. Every nav selector uses a mandatory suffix: `nav button[data-tab]` and `nav button[data-area]`, NEVER bare `nav button`.

**(b) Height cost.** Two rows: 2 x 33 + 1px = 67px; `main{max-height:488px}`; arithmetic 42 + 67 + 488 = 597 <= 600 (worst case 42 + 68 + 488 = 598).

**(c) Sections and element types.** `#t_scraping` sits between `#t_grupos` and `#t_ajustes` and holds: `input#surl type="url"` (placeholder `https://…`, empty = active tab), `input#ssel` (text, optional CSS selector), `input#s_main type="checkbox" checked` (main frame only), `input#sf_md`/`#sf_txt type="checkbox" checked` and `input#sf_csv`/`#sf_pdf type="checkbox"` (no `checked`), `div.bar#bar_scraping` with `<i></i>` as FIRST child, `button#s_run` (text EXTRAER), `button#s_exp.sec` (text EXPORTAR), `ul#scraping_res.res`, `details.log > summary + pre.console#log_scraping`. `#b_scraping` is the badge on the area button. `#t_grupos` is a new `<section>` that contains, moved literally out of `#t_ajustes` (popup.html:431-437), the "Grupos escaneados" card with `#grstatus`, `#bar_grp`, `#grscan` and `#log_grp` intact.

**(d) JS nav dispatch.** `AREA_OF = {estado:"estado",publicar:"facebook",compartir:"facebook",comentar:"facebook",auto:"facebook",grupos:"facebook",scraping:"scraping",ajustes:"ajustes"}`, `DEFAULT_TAB = {estado:"estado",facebook:"publicar",scraping:"scraping",ajustes:"ajustes"}` and `applyNav(t)` with this contract: marks `.on` on the `button[data-area]` of `AREA_OF[t]||"estado"`; marks `.on` on `button[data-tab=t]` (may not exist: `scraping` has no sub-tab button, must not throw); sets `hidden = (AREA_OF[btn.dataset.tab] !== area)` on every `button[data-tab]`; sets `hidden = (area === "scraping")` on `.subrow` (with guard `const sr = document.querySelector("nav .subrow"); if (sr) ...`); marks `.on` on `section#t_<t>`. `showTab(t)` = `tab=t; applyNav(t); saveForm(); tick();`. On load, if `!AREA_OF[tab]` then `tab="estado"`.

**(e) Formats, caps and errors (identical in JS and Python).** markdown 200000 chars (`TOO_BIG` when exceeded), tables 500 rows x 50 columns, links 2000, page text 200000 chars; tables and links are truncated with `truncated:true` and also until their JSON is <= 800000 chars. Literal error catalogue (10): `TOO_BIG`, `TOO_BIG_STORAGE`, `SELECTOR_INVALID`, `NO_TAB`, `TAB_AMBIGUA`, `MISSING_OP`, `PDF_UNAVAILABLE`, `NOT_HTML`, `UNSUPPORTED_URL`, `PDF_TOO_BIG`.

**(f) File name.** `slugify(s)` = NFD, strip diacritics `[\u0300-\u036f]`, lowercase, `[^a-z0-9]+` -> `-`, collapse `-`, trim edges, `slice(0,60)`, and if empty `pagina`; final name `<slug>-<YYYYMMDD-HHMM>.<ext>` with LOCAL time and `ext` in `md|txt|csv|pdf`.

**(g) `runOp` message.** request `{type:"runOp", value:{op, args}}` (args default `{}`), response `{ok:true, data}` or `{ok:false, error}`; exact whitelist `scrape.page`, `scrape.markdown`, `scrape.tables`, `scrape.links`, `cdp`, `navigate`, `tabs` (anything else -> `{ok:false, error:"runOp: op no permitida"}`); sender guard: if the message does not come from a `sender.tab` (`if (_s && _s.tab)`) -> `{ok:false, error:"runOp: origen no permitido"}`; `cdp` restricted: only `args.method === "Page.printToPDF"` is allowed (any other method -> `{ok:false, error:"runOp: cdp no permitido"}`); if a run is already in progress -> `{ok:false, error:"runOp: ocupado"}`. `runOp` responds EXACTLY ONCE on every path and releases the mutex ONLY after sending: `runOpBusy = true`, a single `send(resp)` at the end, and `runOpBusy = false` executed AFTER that `send(resp)` (never before, so two runs cannot overlap while persisting). An `exec` that throws (kill-switch) is caught with `try/catch`.

**(h) Storage.** `scrapingResult = {op, at:Date.now(), ok:true, data}` or `{op, at, ok:false, error}`; for `op === "cdp"` the payload is NEVER stored: the record is `{op, at, ok:true, bytes:<data.length>}` (the base64 travels only in the `send` response, not in storage); `scrapingLog` = array of strings, max 200 (trimmed from the front), line `HH:MM:SS · <op> · OK <n> chars` or `HH:MM:SS · <op> · ERROR <mensaje>`, trimmed to 200 chars; a single `chrome.storage.local.set({scrapingLog, scrapingResult})` call, awaited before responding; if `op` starts with `scrape.` and `JSON.stringify(registro).length > 1000000` it responds `{ok:false, error:"TOO_BIG_STORAGE", chars}` and does NOT write storage. New keys: only `scrapingResult` and `scrapingLog` (they do not collide with the existing `server, token, enabled, maxHops, form, tab, last, autoStats, cxtBarPos`, captured in `storage.keys.before.txt`).

**(i) Button split.** `#s_run` runs ONLY `scrape.page` (preview); `#s_exp` runs, per checked box, the op it needs (`sf_md`->`scrape.markdown`, `sf_txt`->`scrape.page`, `sf_csv`->`scrape.tables`, `sf_pdf`->`cdp Page.printToPDF`) and downloads that format, sharing a single `nombreBase` (same slug+timestamp) for the whole EXPORTAR run.

**(j)** Scraping is multi-site by design.

**(k) Shadow DOM / iframes.** Text and markdown extraction walks the DOM including open shadow roots (same semantics as `deepAll`, bounded by `NODE_CAP=8000`), and `scrape.page` returns `frames` = number of `<iframe>` found at the root; the `contentDocument` of a cross-origin iframe is NEVER dereferenced (only counted): an unreachable iframe neither hangs nor empties the result.

**(l) Unsupported URLs and content.** For every `scrape.*` op, if the target tab has scheme `chrome:`, `chrome-extension:`, `edge:`, `about:`, `view-source:` or `devtools:`, or its host is `chromewebstore.google.com`/`chrome.google.com`, the op returns `{error:"UNSUPPORTED_URL"}` WITHOUT injecting; if `document.contentType` is not HTML/XHTML -> `{error:"NOT_HTML"}`; if the tab no longer exists or is navigating and injection fails -> `{error:"NO_TAB"}`.

**(m) PDF cap.** The `cdp` handler (Page.printToPDF) returns `{error:"PDF_TOO_BIG"}` if the length of the base64 `data` exceeds `12000000` chars, BEFORE `send`; above that size the message does not travel reliably through `chrome.runtime.sendMessage`/`/cmd` (budget 150 s in `server/main.go:599`, 140 s in `cli/main.go:123`); on huge pages `printToPDF` may exceed the budget and the op must fail with an error, never hang.
