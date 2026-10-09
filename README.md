# CXT — Control your real Chrome from a local agent

CXT lets a local process (an AI agent, a script, your terminal) drive **your
real Chrome session** — the one already logged in — through a tiny local bridge
and a Chrome extension. No remote debugging port, no separate browser.

```
AI agent / CLI  ──HTTP──▶  cxtd (127.0.0.1:8799)  ──long-poll──▶  Chrome extension  ──▶  the tab you choose
```

- **Daemon (`cxtd`)**: local HTTP server, queue + long-poll. No shell access.
- **CLI (`cxt`)**: send one op and print the JSON result.
- **Extension (MV3)**: executes ops in tabs (click, type, scroll, screenshot…)
  and can click through the **shadow DOM** and via **CDP** (`chrome.debugger`)
  when a site needs *trusted* input.

## Why

Web UIs force you to click like a human. CXT gives an agent a small, scriptable
surface over the browser you already use, with a hard **kill switch** you own.

## Requirements

- Linux or macOS
- Go 1.26+, Python 3, Node 18+ (only for tests)
- Google Chrome / Chromium (MV3)

## Install (fast)

```bash
git clone git@github.com:santanaoliva-u/CXT.git
cd CXT
bash install.sh
```

`install.sh` builds `bin/cxtd` and `bin/cxt` and prints the 3 remaining steps.
Full manual equivalent:

```bash
./build.sh                 # -> bin/cxtd, bin/cxt
./bin/cxtd &               # start the local bridge
# Chrome: chrome://extensions -> Developer mode -> Load unpacked -> select ./extension
```

## Quick start

```bash
./bin/cxt status                                   # {"enabled":true,"extension_online":true,...}
./bin/cxt tabs                                     # list tabs (id, url, title)
TAB=<facebook-tab-id>
./bin/cxt navigate "{\"tabId\":$TAB,\"url\":\"https://example.com\"}"
./bin/cxt snapshot "{\"tabId\":$TAB}"              # compact a11y-like snapshot with refs @eN
./bin/cxt cdpclicktext "{\"tabId\":$TAB,\"text\":\"Sign in\"}"   # trusted click via CDP
./bin/cxt read "{\"tabId\":$TAB}"                  # page text (trimmed)
./bin/cxt cdp "{\"method\":\"Page.captureScreenshot\",\"params\":{\"format\":\"png\"},\"tabId\":$TAB}"
```

Any op: `./bin/cxt <op> '<json args>'`. The CLI accepts free text too:
`./bin/cxt echo hola`.

## Ops (subset)

| Op | Purpose |
|---|---|
| `status` / `ping` | daemon + extension health |
| `tabs`, `tab.new`, `tab.activate`, `tab.close` | tab management |
| `navigate`, `back`, `forward`, `reload` | navigation |
| `read` | visible text of the page/main area |
| `snapshot` | compact element list with refs `@eN` (a11y-ish) |
| `click`, `type`, `key`, `scroll`, `waitFor` | DOM interaction |
| `deepfind`, `find` | query elements (pierces shadow DOM) |
| `cdpclick`, `cdpclicktext`, `cdpclickref`, `cdptype`, `cdpkey`, `cdp` | **trusted** input / raw CDP |
| `probe`, `batch`, `macro` | cheap checks, batched ops, named recipes |
| `screenshot` | viewport PNG (base64) |

## Kill switch (you own it)

The extension has a big **ACTIVADA / DESACTIVADA** button. When disabled,
**nothing runs**: the daemon refuses every command and the extension stops
polling. It can only be re-armed from the popup (the agent cannot re-enable it).

## Auto-publish scheduler (optional)

`scripts/scheduler.py` reads `~/.cxt/schedule.json` and, at a fixed interval,
publishes N posts (text from `scripts/news.sh` headlines or a fixed text) using
`scripts/clone_publish.py`. It stops cleanly when the count is exhausted.
Control it from the extension popup (**INICIAR / DETENER**).

> These helpers are site-specific examples (they target a Facebook composer).
> Adapt selectors to your target. Automating a social network may violate its
> Terms of Service — use only with accounts you own and at your own risk.

## Share a post to many groups (optional)

`scripts/groups_scan.py` enumerates the groups you belong to (lazy-load scroll
inside the page) into `groups.json`, categorised by keywords. `scripts/groups_share.py`
publishes a text to the groups you pick (by id or by keyword filter), reusing the
composer flow with retries and a CDP dialog auto-accept hook.

```bash
export CXT_TAB=<facebook-tab-id>
python3 scripts/groups_scan.py                 # -> groups.json (auto-run by the sharer if missing)
python3 scripts/groups_share.py --list --filter playa
python3 scripts/groups_share.py --ids 123,456 --text "hello" --dry   # dry-run first
python3 scripts/groups_share.py --ids 123,456 --text "hello"          # real publish
```

The extension popup also exposes a **Compartir en grupos** panel (filter, limit,
text, *Simulación* checkbox, COMPARTIR / DETENER). The daemon endpoints are
`GET/POST /sharegroups`.

## Security

- Binds to `127.0.0.1` only; rejects web-page origins; optional token.
- The extension controls the **browser**, not the OS — no shell.
- Never commit secrets. `.gitignore` excludes binaries, screenshots and runtime
  state.

## License

MIT — see `LICENSE`.
