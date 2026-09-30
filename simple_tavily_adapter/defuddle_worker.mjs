// Defuddle (github.com/kepano/defuddle) as a long-lived worker for reader.py.
// One JSON object per line on stdin: {"html", "url", "markdown"}; one per line on
// stdout: {"title", "content"} or {"error"}. A worker instead of `npx defuddle`
// per page: node plus the module load cost ~0.4 s, a line round trip ~10-50 ms.
import { createInterface } from "node:readline";
import { parseHTML } from "linkedom";
import { Defuddle } from "defuddle/node";

// Defuddle logs its own diagnostics; stdout carries only the protocol.
console.log = console.info = console.warn = console.debug = () => {};

const out = (obj) => process.stdout.write(JSON.stringify(obj) + "\n");

for await (const line of createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  if (!line.trim()) continue;
  try {
    const req = JSON.parse(line);
    const { document } = parseHTML(req.html || "");
    const r = await Defuddle(document, req.url || "", { markdown: !!req.markdown });
    out({ title: r.title || "", content: r.content || "" });
  } catch (e) {
    out({ error: String((e && e.message) || e).slice(0, 300) });
  }
}
