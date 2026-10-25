# CXT — controla tu Chrome real desde un agente local

CXT es un puente local entre un agente (IA, script o terminal) y **tu Chrome real**:
el navegador que ya tienes abierto, con tus sesiones, cookies y pestañas. No usa un
navegador aparte ni abre un puerto de depuración remota.

```
Agente / CLI / HTTP
        │
        ▼
   cxtd  (127.0.0.1:8799)
        │  long-poll
        ▼
 Extensión Chrome (MV3)
        │  DOM + CDP
        ▼
 tu pestaña real
```

CXT está pensado para automatizar tareas **sobre tu propia sesión**: publicar en
Facebook, leer una página, extraer contenido, rellenar formularios o hacer clic donde
un sitio exige entrada "humana". Es una herramienta local y personal, no un servicio
en la nube.

> Repositorio: <https://github.com/santanaoliva-u/CXT>. Esta copia vive en
> `/home/x/.cxt` y es a la vez el directorio de estado (`CXT_DIR`). Licencia MIT:
> consulta `LICENSE`.

---

## Inicio rápido

Si ya tienes Go, Python y Chrome instalados:

```bash
cd /home/x/.cxt
bash build.sh
./bin/cxtd
```

En otra terminal:

```bash
cd /home/x/.cxt
./bin/cxt status
./bin/cxt tabs
```

Luego:

1. Abre `chrome://extensions`.
2. Activa **Modo de desarrollador**.
3. **Cargar descomprimida** → `/home/x/.cxt/extension`.
4. Abre el popup → **Ajustes** → servidor `http://127.0.0.1:8799`.
5. Guarda y prueba. El punto debe estar verde.

Con eso ya puedes usar `navigate`, `read`, `snapshot`, `click` y `type`. Para Facebook,
usa `cdpclicktext`/`cdpclickref` y empieza con modo simulación. Para el resto de
detalles, sigue leyendo.

---

## Índice

- [Inicio rápido](#inicio-rápido)
- [Qué incluye](#qué-incluye)
- [Compatibilidad y sistema operativo](#compatibilidad-y-sistema-operativo)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Arrancar el daemon](#arrancar-el-daemon)
- [Cargar la extensión en Chrome](#cargar-la-extensión-en-chrome)
- [Primer uso](#primer-uso)
- [Operaciones](#operaciones)
- [Configuración](#configuración)
- [Paneles del popup](#paneles-del-popup)
- [Gestor de perfiles de Chrome](#gestor-de-perfiles-de-chrome)
- [Mesa editorial](#mesa-editorial)
- [Scraping](#scraping)
- [Flujos de Facebook](#flujos-de-facebook)
- [API local](#api-local)
- [Seguridad y privacidad](#seguridad-y-privacidad)
- [Solución de problemas](#solución-de-problemas)
- [Pruebas](#pruebas)
- [Límites conocidos](#límites-conocidos)
- [Estructura del proyecto](#estructura-del-proyecto)
- [Licencia](#licencia)

---

## Qué incluye

### Tres piezas

1. **`cxtd`** — daemon HTTP local. Recibe comandos, los encola y los entrega a la
   extensión por *long-poll*. Nunca ejecuta una shell en tu máquina.
2. **`cxt`** — CLI que envía una operación al daemon y devuelve JSON.
3. **Extensión Chrome MV3** — ejecuta las operaciones en la pestaña: clic, escritura,
   scroll, lectura, captura, scraping y, cuando hace falta, entrada real por CDP
   (`chrome.debugger`).

### Capacidades

- Navegar, abrir, cerrar y activar pestañas.
- Leer texto visible, inspeccionar el DOM, buscar por texto y atravesar **shadow DOM**.
- Hacer clic y escribir de forma normal o **confiable por CDP** (útil en Facebook).
- Enviar teclas, hacer scroll, esperar selectores y evaluar JavaScript en la página.
- Capturas de pantalla y PDF por CDP.
- Scraping multi-sitio a texto, Markdown, tablas, enlaces y PDF.
- Automatizaciones de Facebook: publicar, programar, compartir en grupos, comentar y
  escanear grupos.
- **Mesa editorial** asistida por DeepSeek: investigación RSS, brief, borrador,
  verificación y publicación aprobada.
- **Gestor de perfiles** opcional para cambiar entre perfiles de Chrome sin mezclar
  sesiones.

---

## Compatibilidad y sistema operativo

### Sistemas operativos

| Sistema | Estado | Notas |
|---|---|---|
| **Linux x86_64** | **Soportado y probado** | Entorno de referencia: Arch Linux, kernel 7.1.3, Go 1.26.5, Python 3.14.6, Chromium 150. |
| Linux arm64 | No probado | Go puede compilar, pero no hay pruebas del stack completo. |
| **macOS** | **No soportado** | El gestor de perfiles usa `fcntl/flock`, `/proc` y `setsid`; no se ha probado. |
| **Windows** | **No soportado** | El daemon y los scripts asumen un entorno POSIX/Linux. |

CXT es, en la práctica, **Linux únicamente**. Si necesitas macOS o Windows, habría que
portar el gestor de perfiles y revisar las suposiciones de procesos, señales y rutas.

### Navegadores

| Navegador | Estado |
|---|---|
| **Google Chrome / Chromium** | **Soportado y probado** (extensiones MV3). |
| Microsoft Edge | Probable, no probado. Usa el mismo motor Chromium y MV3. |
| Brave | Probable, no probado. |
| Firefox | No soportado (la extensión es MV3 de Chrome y usa `chrome.debugger`). |
| Safari | No soportado. |

La extensión se carga **descomprimida** en modo desarrollador. No está publicada en
Chrome Web Store.

### Versiones

| Componente | Versión |
|---|---|
| Go | **1.26+** (`go.mod` declara `go 1.26`). |
| Python | **3**; probado con **3.14.6**. Solo biblioteca estándar. |
| Node.js | Solo para tests/mock; probado con **26.5.0**. No es necesario para usar CXT. |
| Chrome/Chromium | Versión reciente con soporte MV3; probado con **150**. |

No hay dependencias Python externas. No hay `requirements.txt`, `package.json` ni
`Makefile`; el build es Go puro.

---

## Requisitos

- Linux x86_64.
- Go 1.26 o superior.
- Python 3.
- Google Chrome o Chromium.
- Node.js 18+ **solo** si vas a ejecutar los tests con el mock de extensión.
- Opcional para el scraping a PDF local: Chromium, o `pandoc` + `tectonic`.
- Opcional para la mesa editorial: una clave de DeepSeek.
- Opcional para el gestor de perfiles: `systemd` de usuario (no obligatorio).

Instala lo básico según tu distribución. Ejemplos:

```bash
# Arch Linux
sudo pacman -S go python nodejs chromium

# Debian/Ubuntu (la versión de Go del repositorio puede ser antigua)
sudo apt install golang-go python3 nodejs chromium
```

Si el Go de tu distribución es menor que 1.26, instala el toolchain oficial de Go
antes de compilar.

---

## Instalación

### 1. Entra en la carpeta

Desde un clon del repositorio:

```bash
git clone git@github.com:santanaoliva-u/CXT.git
cd CXT
```

Si ya usas la instalación local, la carpeta es:

```bash
cd /home/x/.cxt
```

Esta carpeta **es** el directorio de instalación y, a la vez, el directorio de estado
por defecto (`CXT_DIR`).

### 2. Compila

```bash
bash build.sh
```

El script hace:

```bash
go build -trimpath -ldflags '-s -w' -o bin/cxtd ./server
go build -trimpath -ldflags '-s -w' -o bin/cxt ./cli
```

Debe terminar con algo similar a:

```
OK: bin/cxt bin/cxtd
```

Verifica:

```bash
ls -l bin/cxt bin/cxtd
```

### 3. Opcional: instala el runtime en `~/.cxt`

Si clonaste el repo en otra carpeta y quieres que el daemon, el scheduler y los
scripts vivan en `~/.cxt`, ejecuta:

```bash
bash install.sh
```

El instalador compila, copia los scripts de ejecución, `fixtures/` y los binarios a
`${CXT_DIR:-$HOME/.cxt}`. No copia tests, capturas ni estado local. Si el repo ya
está en `~/.cxt`, basta con `bash build.sh`.

### 4. Comprueba que el CLI arranca

```bash
./bin/cxt help
```

---

## Arrancar el daemon

En una terminal:

```bash
cd /home/x/.cxt
./bin/cxtd
```

Escucha por defecto en:

```
http://127.0.0.1:8799
```

Déjalo en primer plano mientras pruebas. Para producción local, usa el servicio de
usuario de systemd de más abajo.

### Puerto y token

```bash
# Otro puerto
CXT_PORT=8800 ./bin/cxtd

# Token explícito
CXT_TOKEN="mi-token-largo" ./bin/cxtd

# Sin arrancar el scheduler automáticamente
CXT_NO_SCHED=1 ./bin/cxtd

# Logs de depuración del daemon
CXT_DEBUG=1 ./bin/cxtd
```

También puedes dejar el token en un archivo:

```bash
printf '%s\n' 'mi-token-largo' > "${CXT_DIR:-$HOME/.cxt}/token"
chmod 600 "${CXT_DIR:-$HOME/.cxt}/token"
./bin/cxtd
```

El daemon lee `CXT_TOKEN`; si está vacío, lee `$CXT_DIR/token` (por defecto
`~/.cxt/token`). La extensión debe enviar el mismo token.

### Comprobar salud

Sin token:

```bash
curl -s http://127.0.0.1:8799/health
```

Con token:

```bash
curl -s "http://127.0.0.1:8799/health?token=mi-token-largo"
```

Respuesta esperada (resumida):

```json
{
  "ok": true,
  "version": "0.1.0",
  "extension_online": true,
  "enabled": true,
  "scripts_ok": true
}
```

`extension_online` solo es `true` si la extensión hizo *poll* en los últimos 30
segundos. Si acabas de abrir Chrome, espera unos segundos o abre el popup.

### Servicio systemd de usuario (opcional)

Crea `~/.config/systemd/user/cxtd.service`:

```ini
[Unit]
Description=CXT local bridge
After=graphical-session.target

[Service]
Type=simple
WorkingDirectory=/home/x/.cxt
ExecStart=/home/x/.cxt/bin/cxtd
Environment=CXT_DIR=/home/x/.cxt
Environment=CXT_PORT=8799
Restart=always
RestartSec=3

[Install]
WantedBy=default.target
```

Actívalo:

```bash
systemctl --user daemon-reload
systemctl --user enable --now cxtd
systemctl --user status cxtd
```

Si quieres que siga vivo aunque cierres la sesión gráfica:

```bash
loginctl enable-linger "$USER"
```

Para ver logs:

```bash
journalctl --user -u cxtd -f
```

---

## Cargar la extensión en Chrome

1. Abre `chrome://extensions`.
2. Activa **Modo de desarrollador**.
3. Pulsa **Cargar descomprimida**.
4. Selecciona la carpeta:

   ```
   /home/x/.cxt/extension
   ```

5. Fija la extensión en la barra si quieres tener el popup a mano.
6. Abre el popup de CXT.
7. Ve a **Ajustes**.
8. En **Servidor**, pon:

   ```
   http://127.0.0.1:8799
   ```

   Si cambiaste `CXT_PORT`, usa el puerto correspondiente.

9. En **Token**, pon el mismo token del daemon. Si no configuraste token, déjalo vacío.
10. Pulsa **Guardar** y luego **Probar**.
11. Un punto **verde** y `extension_online: true` significan que está conectado.

La extensión pide permisos amplios (`tabs`, `scripting`, `debugger`, `<all_urls>`) porque
necesita trabajar sobre cualquier pestaña que tú elijas. No es una extensión de tienda
ni está firmada por Google.

---

## Primer uso

### 1. Estado

```bash
./bin/cxt status
```

También existen:

```bash
./bin/cxt ping
./bin/cxt selftest
```

### 2. Ver pestañas

```bash
./bin/cxt tabs
```

Cada pestaña trae `id`, `url`, `title` y `active`. Usa el `id` en las operaciones.

### 3. Navegar y leer

Sustituye `123` por el id real:

```bash
./bin/cxt navigate '{"tabId":123,"url":"https://example.com"}'
./bin/cxt read '{"tabId":123,"max":3000}'
./bin/cxt snapshot '{"tabId":123}'
```

`snapshot` devuelve una lista compacta con referencias `@eN`. Puedes hacer clic real
sobre una referencia:

```bash
./bin/cxt cdpclickref '{"tabId":123,"ref":"e12"}'
```

### 4. Clic y escritura

Clic normal por JavaScript:

```bash
./bin/cxt click '{"tabId":123,"text":"Iniciar sesion"}'
```

Clic "confiable" por CDP, recomendado en Facebook:

```bash
./bin/cxt cdpclicktext '{"tabId":123,"text":"Publicar"}'
```

Escribir:

```bash
./bin/cxt type '{"tabId":123,"selector":"input[name=q]","value":"hola"}'
```

### 5. Captura

```bash
./bin/cxt screenshot '{"tabId":123}'
```

Devuelve un PNG en base64.

### 6. Lote de pasos

```bash
./bin/cxt batch '{
  "tabId": 123,
  "steps": [
    {"op":"navigate","args":{"url":"https://example.com"}},
    {"op":"waitFor","args":{"selector":"h1"}},
    {"op":"read","args":{"max":2000}}
  ]
}'
```

`batch` se detiene si un paso marcado como requerido falla. También sustituye `$TEXT`
en los argumentos.

### Regla general del CLI

```bash
./bin/cxt <operacion> '<json>'
```

Si el segundo argumento no es JSON válido, el CLI lo envía como `{"text":"..."}`. Por
eso `./bin/cxt echo hola` funciona para operaciones que esperan `text`.

---

## Operaciones

El CLI acepta cualquier operación que entienda el daemon, aunque no aparezca en
`cxt help`. Las más importantes:

| Operación | Para qué |
|---|---|
| `status`, `ping` | Salud del daemon y de la extensión. |
| `selftest` | Scripts presentes, grupos y jobs. |
| `tabs` | Lista de pestañas. |
| `tab.new`, `tab.activate`, `tab.close` | Crear, activar y cerrar pestañas. |
| `tab.ensure_auto` | Crea o reutiliza una ventana no enfocada para trabajo automático. Devuelve `tabId`/`windowId`; no enfoca la ventana. |
| `tab.show` | Selecciona una pestaña dentro de su ventana, sin enfocar la ventana. |
| `navigate`, `back`, `forward`, `reload` | Navegación. |
| `read` | Texto visible de la página o del área principal. |
| `snapshot` | Árbol compacto de elementos con refs `@eN`. |
| `find` | Busca elementos en el documento. |
| `deepfind` | Busca atravesando shadow DOM. |
| `click` | Clic normal por JavaScript. |
| `cdpclicktext` | Clic real por CDP buscando por texto. |
| `cdpclickref` | Clic real por CDP sobre una ref `@eN`. |
| `type`, `fill` | Escribe en input, textarea o `contenteditable`. |
| `key`, `cdpkey` | Envía teclas. |
| `scroll` | Scroll de página o de un elemento. |
| `wait`, `waitFor` | Espera fija o espera de selector. |
| `eval` | JavaScript en la página (no en el sistema). |
| `probe` | Comprobación barata de si un texto/selector existe. |
| `batch` | Varios pasos en una sola llamada. |
| `screenshot` | PNG del viewport en base64. |
| `cdp` | CDP crudo, por ejemplo `Page.captureScreenshot`. |
| `scrape.page` | Texto de la página. |
| `scrape.markdown` | Markdown de la página. |
| `scrape.tables` | Tablas. |
| `scrape.links` | Enlaces. |

### Argumentos que conviene recordar

- `tabId`: id numérico de la pestaña. Si se omite, muchas operaciones usan la pestaña
  activa; en automatizaciones es mejor pasarlo siempre.
- `text`: texto visible a buscar.
- `selector`: selector CSS.
- `ref`: referencia `@eN` devuelta por `snapshot`.
- `max`: límite de caracteres en lecturas.
- `timeout`: milisegundos de espera.

### Entrada normal vs. CDP

- `click`, `type` y `key` usan eventos sintéticos del DOM. Son rápidos y suficientes
  para muchas webs.
- `cdpclicktext`, `cdpclickref`, `cdptype` y `cdpkey` usan `chrome.debugger` para enviar
  entrada real. Es lo que suele funcionar en Facebook y en sitios que detectan eventos
  sintéticos.
- CDP puede mostrar el aviso de "Chrome está siendo controlado por software automatizado"
  y puede entrar en conflicto con DevTools abierto. Es normal.

---

## Configuración

### Variables de entorno del daemon

| Variable | Defecto | Qué hace |
|---|---|---|
| `CXT_DIR` | `~/.cxt` | Carpeta de estado y scripts. El daemon busca aquí `scheduler.py`, `groups_scan.py`, etc. |
| `CXT_PORT` | `8799` | Puerto HTTP local. |
| `CXT_TOKEN` | vacío | Token opcional. Si falta, se lee `$CXT_DIR/token`. |
| `CXT_NO_SCHED` | vacío | Si vale `1`, no arranca el scheduler automáticamente. |
| `CXT_DEBUG` | vacío | Logs de depuración del daemon. |

### Variables del CLI

| Variable | Defecto | Qué hace |
|---|---|---|
| `CXT_SERVER` | `http://127.0.0.1:8799` | URL del daemon. |
| `CXT_TOKEN` | vacío | Token que envía el CLI. |

### Variables de los scripts Python

| Variable | Defecto | Qué hace |
|---|---|---|
| `CXT_DIR` | `~/.cxt` | Estado y scripts. |
| `CXT_BIN` | `<repo>/bin/cxt` o `$CXT_DIR/bin/cxt` | Ruta al CLI. |
| `CXT_TAB` | pestaña de Facebook | Fuerza una pestaña concreta. |
| `CXT_CITY` | `Playa del Carmen` | Ciudad para el texto automático del scheduler. |
| `CXT_TAGS` | hashtags de Playa del Carmen | Hashtags por defecto del scheduler. |
| `CXT_PORT` | `8799` | Puerto que usan los scripts para hablar con el daemon. |

### Variables del gestor de perfiles

| Variable | Defecto | Qué hace |
|---|---|---|
| `CXT_PM_HOME` | `~/.cxt` | Estado del gestor (`profiles_active.json`, `profiles_ports.json`, token, logs). |
| `CXT_DAEMON_BIN` / `CXT_CXTD` | `bin/cxtd` | Ruta explícita al binario `cxtd`. |

### Variables de la mesa editorial

| Variable | Defecto | Qué hace |
|---|---|---|
| `DEEPSEEK_API_KEY` | vacío | Clave de DeepSeek. Si falta, se intenta leer de la configuración de opencode. |
| `CXT_EDITORIAL_ALLOW_PUBLISH` | vacío | Si vale `1`, permite publicar desde la mesa (con sello verde y texto aprobado). |
| `CXT_EDITORIAL_DEV` | vacío | Si vale `1`, permite orígenes `http://127.0.0.1:*` y `http://localhost:*` para desarrollo. |

### Archivos de estado

La raíz por defecto es `~/.cxt`. La mayoría de los componentes respeta `CXT_DIR`, pero **no todos**: la mesa editorial, `research.py` y `scheduler.py` usan `~/.cxt` fijo; el gestor de perfiles usa `CXT_PM_HOME`. Si cambias `CXT_DIR`, revisa la columna «Dónde».

| Archivo | Contenido | Dónde |
|---|---|---|
| `token` | Token opcional del daemon. | `CXT_DIR` |
| `editorial.token` | Token opcional del servidor editorial. | `~/.cxt` fijo |
| `profile_manager.token` | Token opcional del gestor de perfiles. | `CXT_PM_HOME` |
| `schedule.json` | Estado del auto-publicar. | `CXT_DIR` (daemon) / `~/.cxt` (scheduler) |
| `schedule.cmd.json` | Comando pendiente para el scheduler. | `CXT_DIR` (daemon) / `~/.cxt` (scheduler) |
| `autoqueue.json` | Cola de publicaciones automáticas. | `CXT_DIR` |
| `autopost_status.json` | Estado del autopost. | `CXT_DIR` |
| `groups.json` | Grupos escaneados. | `CXT_DIR` |
| `groups_labels.json` | Etiquetas de grupos. | `CXT_DIR` |
| `ledger.db` | Registro de operaciones. | `CXT_DIR` |
| `editorial.db` | Base de la mesa editorial. | `~/.cxt` fijo |
| `research_cache.json` | Caché de investigación. | `~/.cxt` fijo |
| `commented.json` | Deduplicación de comentarios. | `CXT_DIR` |
| `pool.txt`, `used.json` | Pool y usados del scheduler. | `~/.cxt` fijo |
| `*.pid`, `*.lock` | Procesos y bloqueos. | `CXT_DIR` (scheduler: `~/.cxt`) |
| `logs/` | Logs locales. | `CXT_DIR/logs` y `~/.cxt/logs` |

No edites estos archivos con procesos en marcha. Haz copia antes de tocarlos.

### Prioridad de configuración

1. Variable de entorno explícita.
2. Archivo de token en `$CXT_DIR` (por defecto `~/.cxt`).
3. Valor por defecto.

El popup guarda su propia configuración en el almacenamiento de la extensión. El
`servidor` y el `token` del popup deben coincidir con los del daemon.

---

## Paneles del popup

La barra superior tiene cuatro áreas: **Estado**, **Facebook**, **Scraping** y
**Ajustes**. Facebook se divide en **Publicar**, **Compartir**, **Comentar**, **Auto**
y **Grupos**.

- **Estado**: salud del daemon, extensión y jobs; actividad reciente.
- **Facebook → Publicar**: publica en tu muro cada X minutos, N veces, con texto
  automático o fijo. Incluye **ABRIR MESA EDITORIAL**, modos `editorial`, `ai`, `fixed`
  y `auto`, vista previa, publicación inmediata, programación y detención.
- **Facebook → Compartir**: comparte un post existente por enlace o publica texto nuevo
  en grupos filtrados por etiqueta o nombre. Tiene modo simulación.
- **Facebook → Comentar**: comenta en una publicación por URL o en la más reciente, en
  modo fijo o automático. Tiene modo simulación.
- **Facebook → Auto**: cola de publicaciones programadas que se comparten solas en los
  grupos. Permite pausar, pasar a la siguiente y reintentar fallidos.
- **Facebook → Grupos**: vuelve a escanear grupos y muestra el conteo por categoría.
- **Scraping**: extrae y exporta la página actual o una URL.
- **Ajustes**: servidor, token, gestor de perfiles, logs, guardar/probar y
  **DETENER TODO**.

El botón **ACTIVADA / DESACTIVADA** es un interruptor de seguridad. Cuando está
desactivado, el daemon rechaza los comandos y la extensión deja de hacer *poll*. Solo
se puede reactivar desde el popup; el agente no puede volver a encenderlo.

---

## Gestor de perfiles de Chrome

El gestor es **opcional**. Sirve para usar varios perfiles de Chrome (por ejemplo,
personal y trabajo) sin mezclar sesiones. Sin él, puedes arrancar `cxtd` a mano y usar
la extensión con el perfil que tengas abierto.

### Arrancarlo

```bash
cd /home/x/.cxt
python3 profile_manager.py --check   # descubre perfiles y sale
python3 profile_manager.py           # sirve en 127.0.0.1:8798
```

Opciones:

```bash
python3 profile_manager.py --port 8798
python3 profile_manager.py --host 127.0.0.1
python3 profile_manager.py --once
python3 profile_manager.py --verbose
```

### Configurarlo en el popup

1. Abre **Ajustes**.
2. En **Gestor de perfiles**, pon `http://127.0.0.1:8798`.
3. Si definiste token, ponlo en **Token del gestor**.
4. Guarda y prueba.

### Qué hace

- Lee `Local State` de Chrome en modo solo lectura para descubrir perfiles.
- Nunca copia cookies ni escribe `Local State`.
- Mantiene **un** `cxtd` activo a la vez por perfil, con su propio PID y puerto.
- Puede arrancar `cxtd` directamente o mediante una unidad systemd de usuario.
- Guarda estado en `CXT_PM_HOME`:
  - `profiles_active.json`
  - `profiles_ports.json`
  - `profile_manager.token`
  - `logs/profile_manager.log`
- El cambio de perfil es explícito, nunca automático.
- Bloquea el cambio si hay un trabajo en curso.
- Si el gestor no está disponible, el popup sigue funcionando y solo muestra
  "Gestor no disponible".

El token del gestor es opcional. Si existe `~/.cxt/profile_manager.token`, el popup lo
envía como `X-CXT-Token`. Sin token, el gestor sigue exigiendo loopback y origen de
extensión.

---

## Mesa editorial

La mesa editorial es una interfaz para investigar, redactar y verificar publicaciones
antes de programarlas. No publica directamente: deja un comando para el scheduler.

### Arrancar el servidor

```bash
cd /home/x/.cxt

# Comprobar configuración
python3 editorial_server.py --check

# Arrancar en 127.0.0.1:8795
python3 editorial_server.py
```

Opciones:

```bash
python3 editorial_server.py --host 127.0.0.1 --port 8795
```

### DeepSeek

La generación usa DeepSeek. La clave se busca en:

1. `DEEPSEEK_API_KEY`.
2. La clave `provider.deepseek.options.apiKey` de
   `~/.config/opencode/opencode.jsonc`.

Modelo: `deepseek-flash`.

Sin clave, la generación falla con "DeepSeek no configurado"; la investigación RSS y
otras partes pueden seguir funcionando.

### Abrir la interfaz

1. Arranca `editorial_server.py`.
2. Abre el popup de CXT.
3. Ve a **Facebook → Publicar**.
4. Pulsa **ABRIR MESA EDITORIAL**.

La UI es `extension/editorial.html` + `editorial.js` y llama a
`http://127.0.0.1:8795`. Si el servidor no está arrancado, avisa.

### Flujo

1. Asignación o instrucción.
2. Investigación con fuentes RSS públicas.
3. Brief.
4. Ángulos.
5. Borrador.
6. Verificación y sello.
7. Aprobación.

La UI permite ajustar frescura, fuentes mínimas, máximo de caracteres, tono, audiencia,
"debe incluir" y "no debe incluir". Modos: `editorial`, `autonomous`, `research` y
`ghost`.

### Publicación segura

Para que la mesa pueda publicar se necesitan **todas** estas condiciones:

- `CXT_EDITORIAL_ALLOW_PUBLISH=1`.
- Sello verde (`stamp.color == "green"` y `publishable`).
- Texto idéntico al borrador aprobado.
- `approve: true` en la petición.

La mesa no llama a Facebook: escribe `~/.cxt/schedule.cmd.json` con
`action: publish_now`, `topic: fixed`, el texto y los metadatos. El scheduler lo
consume. Si ya hay un comando pendiente, devuelve `409 cmd_pending`.

### Token y orígenes

- Token opcional en `~/.cxt/editorial.token`.
- Header `X-CXT-Token`.
- Sin `Origin` o con `chrome-extension://` se acepta.
- Con `CXT_EDITORIAL_DEV=1` también se aceptan orígenes locales de desarrollo.

### Endpoints

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/health` | Salud, versión, DeepSeek configurado, base. |
| `GET` | `/api/audit?limit=` | Historial. |
| `GET` | `/api/run?id=` | Consultar una ejecución. |
| `POST` | `/api/run` | Crear/ejecutar. |
| `POST` | `/api/research` | Investigar. |
| `POST` | `/api/draft` | Generar borrador. |
| `POST` | `/api/verify` | Verificar y sellar. |
| `POST` | `/api/publish` | Aprobar y dejar comando al scheduler. |

---

## Scraping

El área **Scraping** extrae contenido de la página que ya tienes abierta. Es
multi-sitio: funciona en cualquier web, no solo en Facebook.

### Interfaz

- **URL** o, si la dejas vacía, la pestaña activa.
- **Selector CSS** opcional para acotar.
- **Solo área principal**: usa `main`, `article` o `[role=main]` si existen; si no,
  `body`.
- **EXTRAER** ejecuta `scrape.page` y muestra una vista previa.
- **EXPORTAR** descarga cada formato marcado:
  - Markdown: `scrape.markdown`.
  - Texto: `scrape.page`.
  - Tablas: `scrape.tables`.
  - PDF: impresión por CDP.
- Nombre base común: `<slug>-<YYYYMMDD-HHMM>.<ext>`.
- El PDF por CDP imprime la página completa y es *best-effort*: en páginas enormes puede
  tardar o fallar, pero no cuelga.

### Operaciones

| Operación | Argumentos | Resultado |
|---|---|---|
| `scrape.page` | `tabId`, `selector?`, `main?`, `max?` | `{url,title,text,chars,mode,frames}` |
| `scrape.markdown` | `tabId`, `selector?`, `main?`, `max?` | `{url,title,markdown,chars}` |
| `scrape.tables` | `tabId` | `{url,tables:[{caption,headers,rows}],truncated}` |
| `scrape.links` | `tabId` | `{url,links:[{text,href,sameOrigin}],truncated}` |

**Topes:** texto y Markdown 200000 caracteres; Markdown que los supera devuelve
`TOO_BIG`. Tablas: 500 filas x 50 columnas. Enlaces: 2000. Tablas y enlaces se recortan
con `truncated: true` y también hasta que su JSON baja de 800000 caracteres.

### CLI `scrape.py`

```bash
# Por defecto ejecuta page,markdown,tables,links y escribe Markdown en ~/.cxt/out
python3 scrape.py

# Varios formatos
python3 scrape.py --format md,txt,csv,pdf --out ~/.cxt/out

# Navegar antes de extraer
python3 scrape.py --url "https://example.com" --format md,txt

# Pestaña concreta
python3 scrape.py --tab 123 --format md,txt

# Probar sin extensión con un bundle offline
python3 scrape.py --page-json bundle.json --format md
```

Banderas:

- `--url`: navega antes de extraer.
- `--tab`: id de pestaña; si falta, la única activa.
- `--ops`: `page,markdown,tables,links`.
- `--format`: `md,txt,csv,pdf`.
- `--out`: directorio de salida; defecto `$CXT_DIR/out`.
- `--page-json`: bundle offline con forma
  `{"page":{...},"markdown":{...},"tables":{...},"links":{...}}`.

**Códigos de salida:** `0` ok, `1` uso/argumentos, `2` datos faltantes (`NO_TAB`,
`TAB_AMBIGUA`, `MISSING_OP`), `3` `PDF_UNAVAILABLE`, `4` error de escritura, `5` error
de la extensión.

`scrape.py` es el que escribe archivos. El CLI `bin/cxt` no escribe ficheros.

### Errores de scraping

- `TOO_BIG`: el Markdown supera 200000 caracteres.
- `TOO_BIG_STORAGE`: el resultado no cabe en el almacenamiento de la extensión.
- `SELECTOR_INVALID`: selector CSS inválido.
- `NO_TAB`: la pestaña no existe, está navegando o la inyección falla.
- `TAB_AMBIGUA`: hay más de una pestaña activa y no pasaste `--tab`.
- `MISSING_OP`: falta la operación que necesita el formato pedido.
- `PDF_UNAVAILABLE`: no hay motor de PDF local.
- `NOT_HTML`: el contenido no es HTML/XHTML.
- `UNSUPPORTED_URL`: `chrome://`, `chrome-extension:`, Web Store, etc.
- `PDF_TOO_BIG`: el PDF en base64 supera 12000000 caracteres.

### Límites de scraping

- Una sola página, iniciada por ti y sobre tu propia sesión.
- No hace crawl masivo.
- No salta `robots.txt` ni CAPTCHA.
- El texto y el Markdown recorren shadow roots abiertos. Los `frames` solo se cuentan;
  el contenido de un iframe de otro origen no se lee.
- URLs no soportadas: `chrome://`, `chrome-extension:`, `edge:`, `about:`,
  `view-source:`, `devtools:` y Chrome Web Store.
- PDF por CDP: tope de 12000000 caracteres en base64.

---

## Flujos de Facebook

Los scripts de Facebook viven **directamente en la raíz** de esta instalación. No hay
carpeta `scripts/`.

> Automatizar Facebook puede violar sus Términos de Servicio y hacer que bloqueen tu
> cuenta. Úsalo solo con cuentas tuyas y bajo tu responsabilidad. Empieza siempre con
> `--dry` o con el modo simulación del popup.

### Pestaña objetivo

```bash
export CXT_TAB=123
```

Si no lo defines, los scripts buscan la primera pestaña de Facebook. Para automatizar
sin tocar tu pestaña personal, el stack puede crear una pestaña propia
(`tab.ensure_auto`) y pasar su `CXT_TAB` al hijo.

### Publicar

```bash
python3 clone_publish.py mi_texto.txt
```

### Scheduler

```bash
python3 scheduler.py
```

Lee `CXT_CITY` y `CXT_TAGS`. Publica N posts cada X minutos y se detiene solo.

### Grupos

```bash
python3 groups_scan.py                 # -> groups.json
python3 groups_share.py --list --filter playa
python3 groups_share.py --ids 123,456 --text "hola" --dry
python3 groups_share.py --ids 123,456 --text "hola"
```

### Compartir un post existente

```bash
python3 share_post.py --ids "Grupo A,Grupo B" --dry
python3 share_post.py --url "https://www.facebook.com/..." --ids "Grupo A" --no-dry
```

### Comentar

```bash
python3 comment.py --url "https://..." --text "buen post" --dry
python3 comment.py --url "https://..." --text "buen post" --no-dry
python3 comment.py --delete "texto a buscar"
```

Los paneles del popup hacen lo mismo a través del daemon: `/schedule`,
`/sharegroups`, `/sharepost`, `/comment` y `/groupscan`.

---

## API local

El daemon habla HTTP en loopback. No hay TLS porque no sale de tu máquina.

### Endpoints principales

| Método | Ruta | Uso |
|---|---|---|
| `GET` | `/health` | Salud general. |
| `GET` | `/selftest` | Scripts, grupos y jobs. |
| `POST` | `/cmd` | Enviar una operación y esperar resultado. |
| `POST` | `/state` | Activar/desactivar la extensión. |
| `GET/POST` | `/schedule` | Estado/configuración del auto-publicar. |
| `GET/POST` | `/sharegroups` | Compartir en grupos. |
| `GET/POST` | `/sharepost` | Compartir un post. |
| `GET/POST` | `/autopost` | Cola de automatización: encolar, pausar, cancelar, reintentar, siguiente y ritmo. |
| `GET/POST` | `/comment` | Comentar. |
| `GET/POST` | `/groupscan` | Escanear grupos. |
| `GET/POST` | `/log?job=&n=` | Últimas líneas del log; con `clear:true` limpia uno o todos. |
| `GET` | `/stat` | Estadísticas. |
| `GET` | `/next` | Long-poll de la extensión. |
| `POST` | `/ack` | Confirmación de la extensión. |
| `POST` | `/result` | Resultado de la extensión. |

### Ejemplo con curl

Sin token:

```bash
curl -s -X POST http://127.0.0.1:8799/cmd \
  -H 'Content-Type: application/json' \
  -d '{"op":"tabs","args":{}}'
```

Con token:

```bash
curl -s -X POST "http://127.0.0.1:8799/cmd?token=mi-token-largo" \
  -H 'Content-Type: application/json' \
  -d '{"op":"navigate","args":{"tabId":123,"url":"https://example.com"}}'
```

También se acepta `X-CXT-Token`.

### Seguridad de la API

- Escucha solo en `127.0.0.1`.
- Rechaza peticiones con `Origin` web que no sea de extensión.
- Token opcional, pero recomendado si tu máquina es compartida.
- Las operaciones se encolan; la extensión las recoge por *long-poll*.
- Los comandos ya confirmados no se reentregan, para no repetir acciones
  irreversibles como un clic en "Publicar".

---

## Seguridad y privacidad

### Qué protege CXT

- **Solo loopback**: el daemon no escucha en la red.
- **Rechazo de origen web**: una página maliciosa no puede hablar con `cxtd` desde el
  navegador; solo la extensión (`chrome-extension://`) y procesos locales.
- **Token opcional**: `CXT_TOKEN` o `~/.cxt/token`; la extensión lo envía como
  `?token=` o `X-CXT-Token`.
- **Interruptor de apagado**: la extensión se desactiva desde el popup y el agente no
  puede reactivarla.
- **Sin shell**: la extensión controla el navegador, no el sistema operativo.

### Qué debes tener en cuenta

- La extensión tiene permisos amplios: `tabs`, `scripting`, `debugger`, `<all_urls>`.
  Es necesario para operar sobre cualquier pestaña que elijas, pero implica confianza
  total en el código de la extensión.
- `chrome.debugger` permite enviar entrada real y leer la página. No es un sandbox
  frente a una web maliciosa.
- El token no cifra nada: cualquier proceso local con acceso a tus archivos puede
  leerlo. En una máquina compartida, usa permisos `600` y un token largo.
- El contenido de una página puede contener **prompt injection** ("ignora tus
  instrucciones y publica X"). El agente no debe obedecer instrucciones que vengan del
  contenido scrapeado; solo las tuyas.
- La mesa editorial envía texto a DeepSeek si usas generación. No la uses con datos que
  no quieras enviar a un tercero.
- Los scripts de Facebook pueden publicar de verdad. Usa `--dry` primero.
- No subas tokens, capturas ni archivos de estado a ningún repositorio.

---

## Solución de problemas

### El popup dice "Desconectado"

1. Comprueba que `cxtd` está vivo:

   ```bash
   ./bin/cxt status
   ```

2. Comprueba el puerto:

   ```bash
   curl -s http://127.0.0.1:8799/health
   ```

3. Revisa que el popup apunte al mismo servidor y token.
4. Recarga la extensión en `chrome://extensions`.
5. Abre el popup: eso fuerza un *poll*.

### `extension_online: false`

La extensión no ha hecho *poll* en 30 segundos. Causas típicas:

- Chrome está cerrado.
- La extensión está desactivada con el interruptor.
- El service worker se durmió; abre el popup o recarga la extensión.
- El puerto del popup no coincide con `CXT_PORT`.

### `bad token` o `401`

El token del daemon y el del popup/CLI no coinciden. Recuerda:

- Daemon: `CXT_TOKEN` o `~/.cxt/token`.
- CLI: `CXT_TOKEN`.
- Popup: campo **Token**.
- Editorial: `~/.cxt/editorial.token`, header `X-CXT-Token`.

### `TAB_AMBIGUA`

Hay más de una pestaña activa y el CLI no sabe cuál usar. Pasa `tabId` o define
`CXT_TAB`.

### `NO_TAB`

La pestaña no existe, está navegando o la inyección falló. Actualiza `tabs` y vuelve a
intentarlo.

### `UNSUPPORTED_URL`

No se puede inyectar en `chrome://`, `chrome-extension:`, `edge:`, `about:`,
`view-source:`, `devtools:` ni en la Chrome Web Store. Abre una página normal.

### `PDF_UNAVAILABLE`

El CLI no encontró motor de PDF local. Instala Chromium, o `pandoc` + `tectonic`. La
exportación PDF desde el popup usa CDP y no necesita ese motor.

### El scheduler no publica

- Comprueba `schedule.json` y `scheduler.pid`.
- Revisa `/tmp/cxt_sched.log`.
- Asegúrate de que Chrome está abierto y con sesión en Facebook.
- No arranques con `CXT_NO_SCHED=1` si quieres el scheduler automático.
- Comprueba que la pestaña objetivo sigue existiendo.

### El gestor de perfiles no aparece

- Arranca `python3 profile_manager.py`.
- Comprueba `http://127.0.0.1:8798`.
- Revisa `~/.cxt/logs/profile_manager.log`.
- Si definiste token, cópialo en **Token del gestor**.
- Sin gestor puedes seguir usando `cxtd` manual.

### La mesa editorial no genera

- Ejecuta `python3 editorial_server.py --check`.
- Comprueba `deepseek_configured`.
- Define `DEEPSEEK_API_KEY` o la clave en la configuración de opencode.
- Revisa que el servidor esté en `127.0.0.1:8795`.

### La mesa no publica

- `CXT_EDITORIAL_ALLOW_PUBLISH=1`.
- Sello verde y `publishable`.
- Texto idéntico al aprobado.
- No debe haber otro `schedule.cmd.json` pendiente.
- El scheduler debe estar vivo.

### Puerto ocupado

```bash
ss -ltnp | grep -E '8795|8798|8799'
```

Cambia el puerto con `CXT_PORT`, `--port` o la configuración del popup, y actualiza el
token/servidor en consecuencia.

### Facebook cambió el DOM

Los selectores de los scripts de Facebook son específicos del sitio y se rompen cuando
Facebook cambia el compositor. Actualiza los selectores o usa la mesa editorial/popup
con vista previa y modo simulación. No ejecutes a ciegas.

---

## Pruebas

### Sin navegador

```bash
python3 test_stack.py
```

Arranca un `cxtd` aislado en el puerto 8796 con `CXT_DIR` temporal y
`CXT_NO_SCHED=1`; comprueba `/health`, `/selftest` y el CLI. No necesita Chrome.

### Con extensión simulada

```bash
bash test_bridge.sh
```

Usa un daemon y un mock de la extensión. Por defecto ocupa el puerto **8798**, que es
también el del gestor de perfiles. Si el gestor está arrancado:

```bash
CXT_TEST_PORT=8797 bash test_bridge.sh
```

### Auto-verificación

```bash
./bin/cxt selftest
```

Informa `cxt_dir`, scripts que faltan, `groups_count` y si cada job está vivo.

### Tests Python

```bash
python3 -m pytest -q
```

Algunos tests usan `CXT_DIR` temporal y no tocan tu estado real. Si fallan por falta de
navegador, ejecuta primero los tests sin navegador de arriba.

### Scraping end-to-end

```bash
bash test_scraping_e2e.sh
```

Usa un puerto y un `CXT_DIR` temporales, y el mock de extensión.

---

## Límites conocidos

- **Linux solamente** en la práctica. macOS y Windows no están soportados.
- **Chrome/Chromium**; Firefox y Safari no.
- La extensión se carga descomprimida; no hay actualización automática.
- Los selectores de Facebook son frágiles y dependen del DOM actual.
- El scheduler necesita Chrome abierto y la sesión iniciada.
- `chrome.debugger` puede entrar en conflicto con DevTools y muestra un aviso.
- El scraping es de una página; no hay crawl masivo.
- Los iframes de otro origen no se leen; solo se cuentan.
- El PDF por CDP es *best-effort* en páginas enormes.
- La mesa editorial depende de DeepSeek para generar; sin clave no genera.
- No hay autenticación multiusuario: es una herramienta local de un solo usuario.
- El repo incluye `install.sh`, pero la instalación local en `/home/x/.cxt` también
  funciona solo con `bash build.sh`.

---

## Estructura del proyecto

```
CXT/
├── build.sh                 # compila bin/cxtd y bin/cxt
├── install.sh               # compila y copia el runtime a CXT_DIR
├── go.mod                   # module cxt, go 1.26
├── LICENSE                  # MIT
├── .gitignore               # excluye estado, secretos, backups y capturas
├── server/                  # daemon HTTP y cola de comandos
├── cli/                     # CLI cxt
├── extension/               # extensión MV3, popup, mesa editorial
│   ├── manifest.json
│   ├── background.js
│   ├── popup.html / popup.js
│   └── editorial.html / editorial.js
├── bin/                     # binarios compilados (se genera, no se sube)
├── cxtlib.py                # utilidades compartidas de los scripts
├── autopost.py              # worker residente de la cola de automatización
├── scheduler.py             # auto-publicar
├── clone_publish.py         # publicar un texto
├── groups_scan.py           # escanear grupos
├── groups_share.py          # compartir en grupos
├── share_post.py            # compartir un post existente
├── comment.py               # comentar / borrar comentario
├── scrape.py                # scraping a md/txt/csv/pdf
├── profile_manager.py       # gestor de perfiles (opcional)
├── editorial_server.py      # API de la mesa editorial
├── editorial.py / editorial_core.py
├── news.sh                  # titulares vía Google News RSS
├── test_stack.py
├── test_bridge.sh
├── mock_ext.mjs
├── fixtures/                # datos de prueba
└── *.json, *.db, *.pid      # estado local, ignorado por git
```

No hay carpeta `scripts/`: los scripts están en la raíz porque la raíz es también
`CXT_DIR` en la instalación local.

---

## Licencia

CXT se distribuye bajo **MIT**. Consulta el archivo [`LICENSE`](LICENSE).

El uso de CXT para automatizar Facebook, X, Instagram u otras plataformas puede violar
sus Términos de Servicio. Úsalo con tus propias cuentas y bajo tu responsabilidad.
