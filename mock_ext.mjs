// Mock del service worker de la extensión, para test del puente sin Chrome.
const SERVER = process.env.CXT_SERVER || "http://127.0.0.1:8798";
const TOKEN = process.env.CXT_TOKEN || "tt";
const qs = (p) => SERVER + p + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function handle(op, args) {
  if (op === "ping") return { pong: true, ts: 1 };
  if (op === "echo") return args;
  if (op === "sum") return { sum: (args.a || 0) + (args.b || 0) };
  if (op === "read") return { url: "http://mock/", title: "Mock", text: "hola" };
  if (op === "scrape.page") return { url: "http://mock/", title: "Mock", text: "hola", chars: 4, mode: "main", frames: 0 };
  if (op === "scrape.markdown") return { url: "http://mock/", title: "Mock", markdown: "# Mock", chars: 6 };
  if (op === "scrape.tables") return { url: "http://mock/", tables: [{ caption: "c", headers: ["a","b"], rows: [["1","2"]] }], truncated: false };
  if (op === "scrape.links") return { url: "http://mock/", links: [{ text: "x", href: "http://mock/x", sameOrigin: true }], truncated: false };
  return { error: "op desconocida" };
}

async function main() {
  for (;;) {
    let cmd = null;
    try {
      const res = await fetch(qs("/next"), { cache: "no-store" });
      if (res.status === 204) continue;
      if (!res.ok) {
        await sleep(200);
        continue;
      }
      cmd = await res.json();
    } catch (e) {
      await sleep(200);
      continue;
    }
    const data = await handle(cmd.op, cmd.args || {});
    const ok = !(data && data.error);
    try {
      await fetch(qs("/result"), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id: cmd.id, ok, data, error: (data && data.error) || "" }),
      });
    } catch (e) {}
  }
}

main();
