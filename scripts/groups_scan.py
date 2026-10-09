import json, os, subprocess, sys, time, unicodedata

HERE = os.path.dirname(os.path.abspath(__file__))
CXT = os.environ.get("CXT_BIN") or os.path.join(os.path.dirname(HERE), "bin", "cxt")
TAB = int(os.environ["CXT_TAB"])
OUT = os.path.join(HERE, "groups.json")
URL = "https://www.facebook.com/groups/joins/?nav_source=tab&ordering=viewer_added"

PASS_JS = r"""
(async () => {
  const m = {};
  const grab = () => {
    document.querySelectorAll('a[href*="/groups/"]').forEach(a => {
      let h = (a.href || '').split('?')[0];
      let mm = h.match(/\/groups\/([^\/]+)/);
      if (!mm) return;
      let id = decodeURIComponent(mm[1]);
      if (['joins','feed','discover','create','search','your_groups','mine'].includes(id)) return;
      let lines = (a.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
      let name = lines.find(x => x.length > 1 && !/^(ver grupo|ver todo|descubrir)$/i.test(x));
      if (name && (!m[id] || name.length > m[id].length)) m[id] = name;
    });
  };
  const sc = document.scrollingElement || document.documentElement;
  for (let i = 0; i < 48; i++) {
    grab();
    sc.scrollTop = sc.scrollHeight;
    window.scrollBy(0, 700);
    const btns = [...document.querySelectorAll('div[role="button"],a,span')].filter(b => /^Ver m[aá]s$/.test((b.innerText || '').trim()));
    if (btns.length) btns[btns.length - 1].click();
    await new Promise(r => setTimeout(r, 850));
  }
  grab();
  return JSON.stringify(m);
})()
"""

CATS = [
    ("playa", ["playa del carmen", "playense", "riviera maya", "riviera marya"]),
    ("guadalupan", ["guadalupe", "guadalupan", "cristo rey", "el peten", "villas riviera"]),
    ("empleo", ["empleo", "trabajo", "vacante", "bolsa de trabajo", "reclut"]),
    ("compraventa", ["compra", "venta", "mercado", "segunda mano", "comercio", "cambio"]),
    ("renta", ["renta", "rento", "departamento", "cuarto", "inmueble", "terreno"]),
    ("tech", ["codex", "program", "developer", " software", "tecnolog", "inteligencia artificial", "open source", "linux", "opencode"]),
    ("turistico", ["turismo", "turist", "hotel", "restaurante", "gastronom", "viaj"]),
]


def norm(s):
    s = unicodedata.normalize("NFD", s.lower())
    return "".join(c for c in s if unicodedata.category(c) != "Mn")


def categoriza(name):
    n = " " + norm(name) + " "
    for cat, keys in CATS:
        for k in keys:
            if k in n:
                return cat
    return "otros"


def call(op, args, timeout=70):
    p = subprocess.run([CXT, op, json.dumps(args)], capture_output=True, text=True, timeout=timeout)
    try:
        return json.loads(p.stdout.strip())
    except Exception:
        return {"_raw": p.stdout.strip(), "_err": p.stderr.strip()}


def one_pass():
    r = call("cdp", {"tabId": TAB, "method": "Runtime.evaluate",
                     "params": {"expression": PASS_JS, "returnByValue": True, "awaitPromise": True}})
    try:
        return json.loads(r["result"]["result"]["value"])
    except Exception:
        return {}


def main():
    reset = "--reset" in sys.argv
    got = {}
    if not reset and os.path.exists(OUT):
        try:
            for g in json.load(open(OUT, encoding="utf-8")).get("groups", []):
                got[g["id"]] = g["name"]
        except Exception:
            pass
    call("tab.activate", {"tabId": TAB})
    call("navigate", {"tabId": TAB, "url": URL, "timeout": 45000})
    time.sleep(5)
    for p in range(4):
        m = one_pass()
        before = len(got)
        for k, v in m.items():
            if k not in got or len(v) > len(got[k]):
                got[k] = v
        print(f"[pass {p}] +{len(got) - before} (total {len(got)})", file=sys.stderr)
        if len(got) == before and p >= 2:
            break
        time.sleep(1.5)
    groups = [{"id": k, "name": v, "category": categoriza(v), "href": "https://www.facebook.com/groups/" + k + "/"} for k, v in got.items()]
    groups.sort(key=lambda g: (g["category"], norm(g["name"])))
    cats = {}
    for g in groups:
        cats[g["category"]] = cats.get(g["category"], 0) + 1
    doc = {"scanned_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "count": len(groups), "categories": cats, "groups": groups}
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(json.dumps({"ok": True, "count": len(groups), "categories": cats, "out": OUT}, ensure_ascii=False))


if __name__ == "__main__":
    main()
