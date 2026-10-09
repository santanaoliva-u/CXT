# CXT — Controla tu Chrome real desde un agente local

CXT deja que un agente (una IA, un script o tu terminal) maneje **tu Chrome real** —
el que ya tienes abierto y con sesión iniciada— a través de un puente local mínimo y
una extensión de Chrome. **Sin puerto de depuración remota y sin un navegador aparte.**

```
Agente / CLI  ──HTTP──▶  cxtd (127.0.0.1:8799)  ──long-poll──▶  Extensión Chrome  ──▶  tu pestaña real
```

- **Daemon (`cxtd`)**: servidor HTTP local con cola de comandos y *long-poll*. **No da shell a la máquina.**
- **CLI (`cxt`)**: envía una operación y te imprime el resultado en JSON.
- **Extensión (MV3)**: ejecuta las operaciones en la pestaña (clic, escribir, scroll, captura…),
  atraviesa el **shadow DOM** y puede usar **CDP** (`chrome.debugger`) cuando el sitio exige
  entrada *confiable* (Facebook, etc.).

## ¿Por qué?

Las webs te obligan a hacer clic "como un humano". CXT le da al agente una superficie
pequeña y programable sobre el navegador que ya usas, y con un **interruptor de apagado**
que controlas tú.

## Requisitos

- Linux o macOS
- Go 1.26+, Python 3, Node 18+ (Node solo para los tests)
- Google Chrome / Chromium (extensiones MV3)

## Instalación rápida

```bash
git clone git@github.com:santanaoliva-u/CXT.git
cd CXT
bash install.sh
```

`install.sh` compila `bin/cxtd` y `bin/cxt` y te imprime los 3 pasos que faltan.
A mano es equivalente a:

```bash
./build.sh                 # -> bin/cxtd, bin/cxt
./bin/cxtd &               # arranca el puente local en 127.0.0.1:8799
# Chrome: chrome://extensions -> Modo desarrollador -> Cargar descomprimida -> ./extension
```

En el popup de la extensión pon el servidor `http://127.0.0.1:8799` (deja el token vacío
salvo que arranques `cxtd` con `CXT_TOKEN=...`). Pulsa **Guardar**. Un punto **verde** = conectado.

## Primeros pasos

```bash
./bin/cxt status          # salud: enabled, extension_online, scripts_ok, jobs...
./bin/cxt tabs            # lista de pestañas (id, url, título)
TAB=<id-de-la-pestana>
./bin/cxt navigate "{\"tabId\":$TAB,\"url\":\"https://example.com\"}"
./bin/cxt snapshot "{\"tabId\":$TAB}"                                   # lista compacta con refs @eN
./bin/cxt cdpclicktext "{\"tabId\":$TAB,\"text\":\"Iniciar sesion\"}"   # clic "confiable" (CDP)
./bin/cxt read "{\"tabId\":$TAB}"                                       # texto de la página (recortado)
./bin/cxt cdp "{\"method\":\"Page.captureScreenshot\",\"params\":{\"format\":\"png\"},\"tabId\":$TAB}"
```

Cualquier operación es `./bin/cxt <op> '<json>'`. El CLI también acepta texto libre:
`./bin/cxt echo hola`.

## Operaciones (resumen)

| Operación | Para qué sirve |
|---|---|
| `status` / `ping` | salud del daemon y de la extensión |
| `tabs`, `tab.new`, `tab.activate`, `tab.close` | manejo de pestañas |
| `navigate`, `back`, `forward`, `reload` | navegación |
| `read` | texto visible de la página / área principal |
| `snapshot` | lista compacta de elementos con refs `@eN` (tipo accesibilidad) |
| `find`, `deepfind` | buscar elementos por texto (atraviesa shadow DOM) |
| `click`, `type`, `key`, `scroll`, `wait`, `waitFor` | interacción con el DOM |
| `cdpclick`, `cdpclicktext`, `cdpclickref`, `cdptype`, `cdpkey`, `cdp` | entrada **confiable** (CDP) y CDP crudo |
| `probe`, `batch`, `macro` | chequeos baratos, operaciones por lotes, recetas con nombre |
| `screenshot` | PNG del viewport (base64) |
| `selftest` | auto-verificación (scripts, grupos, jobs) |

## Interruptor de seguridad (lo controlas tú)

La extensión tiene un botón grande **ACTIVADA / DESACTIVADA**. Cuando está desactivada
**no corre nada**: el daemon rechaza cada comando y la extensión deja de hacer *polling*.
**Solo se puede reactivar desde el popup** (el agente no puede volver a encenderla).
El estado también se refleja en el badge y en `/health` (`enabled`).

## Paneles del popup

- **Auto-publicar**: cada X minutos (3 / 5 / 10…), N publicaciones, con texto automático
  (titulares de `news.sh`) o fijo. Botones INICIAR / DETENER.
- **Compartir en grupos**: escribe un texto y lo publica en los grupos que elijas
  (por filtro de nombre o por id). Casilla *Simulación* para probar sin publicar.
- **Compartir publicación**: re-comparte una publicación que ya existe (tu perfil, una
  Página o un enlace) a los grupos elegidos, usando el diálogo **nativo** de Facebook.
- **Comentar**: comenta en una publicación (por URL o la más reciente) en modo fijo o
  automático (cada X minutos, N veces).
- **Grupos**: vuelve a escanear tus grupos y muestra cuántos hay por categoría.

## Scripts opcionales (ejemplos para redes)

> Son ejemplos específicos de sitio (apuntan al compositor de Facebook). Adapta los
> selectores a tu objetivo. Automatizar una red social puede violar sus Términos de
> Servicio: úsalo solo con cuentas tuyas y bajo tu responsabilidad.

En el repo viven en `scripts/`; en una instalación local (`~/.cxt`) están directamente
en esa carpeta.

```bash
export CXT_TAB=<id-de-la-pestana-de-facebook>

# Publicar un texto (una vez o programado):
python3 scripts/clone_publish.py mi_texto.txt

# Scheduler: publica N posts cada X minutos y se detiene solo:
python3 scripts/scheduler.py

# Escanear y compartir texto en grupos:
python3 scripts/groups_scan.py                       # -> groups.json (categorías por palabras clave)
python3 scripts/groups_share.py --list --filter playa
python3 scripts/groups_share.py --ids 123,456 --text "hola" --dry    # simulación (recomendado)
python3 scripts/groups_share.py --ids 123,456 --text "hola"          # publica de verdad

# Re-compartir una publicación existente (nativo, sin marca de agua):
python3 scripts/share_post.py --ids "Grupo A,Grupo B" --dry
python3 scripts/share_post.py --url https://www.facebook.com/... --ids "Grupo A" --no-dry

# Comentar:
python3 scripts/comment.py --url https://... --text "buen post" --dry
python3 scripts/comment.py --url https://... --text "buen post" --no-dry
python3 scripts/comment.py --delete "texto a buscar"                 # borra un comentario tuyo
```

Cada panel del popup hace lo mismo a través del daemon:
`/schedule` (auto-publicar), `/sharegroups`, `/sharepost`, `/comment` y `/groupscan`.

## Auto-verificación

```bash
python3 test_stack.py        # daemon aislado, sin navegador: imprime PASS/FAIL
./test_bridge.sh             # daemon + extensión simulada (mock)
./bin/cxt selftest           # GET /selftest: scripts presentes, nº de grupos, jobs
```

`/health` incluye `scripts_ok`. `/selftest` informa `cxt_dir`, scripts que falten,
`groups_count` y si cada job está vivo.

## Configuración (variables de entorno)

- `CXT_DIR` — carpeta de estado/scripts (por defecto `~/.cxt`). El daemon lee sus
  scripts y su estado de aquí.
- `CXT_BIN` — ruta al CLI `cxt` (si no, `<repo>/bin/cxt`).
- `CXT_TAB` — id de la pestaña objetivo (si no, la primera de `facebook.com`).
- `CXT_PORT` / `CXT_TOKEN` — puerto del daemon / token opcional.
- `CXT_CITY` / `CXT_TAGS` — tema y hashtags por defecto del auto-publicar.
- `scripts/cxtlib.py` — utilidades compartidas que usan los scripts.

## Seguridad

- Escucha **solo en `127.0.0.1`** y **rechaza peticiones con `Origin` web** (una página
  maliciosa no puede hablar con el daemon). Los procesos locales y la extensión
  (`chrome-extension://`) sí.
- Token opcional (`CXT_TOKEN` o `~/.cxt/token`); la extensión lo envía como `?token=`.
- La extensión controla el **navegador**, no el sistema operativo: no da shell.
- No subas secretos al repo. El `.gitignore` excluye binarios, capturas y estado en tiempo de ejecución.

## Aviso

Automatizar la publicación en plataformas (Facebook, X, TikTok…) puede violar sus
Términos de Servicio y **banear tu cuenta**. Úsalo con tus propias cuentas y bajo tu
responsabilidad.

## Licencia

MIT — ver `LICENSE`.

## Rendimiento (nota)

El motor de búsqueda de la extensión usa `Element.checkVisibility()` con caché, un tope
de nodos (~8000), parada temprana y auto-desacople del *debugger* a los pocos segundos
sin operaciones, para no trabar el navegador. El almacenamiento se escribe con *throttle*.

---

## TL;DR (English)

CXT drives **your real, already-logged-in Chrome** from a local agent via a tiny local
bridge (`cxtd` on `127.0.0.1:8799`) plus an MV3 extension — no remote debugging port and
no separate browser. The extension clicks through the shadow DOM and uses CDP for
*trusted* input. It ships a hard **kill switch** you own, plus optional site-specific
helpers (auto-publish scheduler, group scan/share, re-share an existing post, comment
autopost). Build with `bash install.sh`, load `./extension` unpacked, then
`./bin/cxt status`. MIT licensed.
