/**
 * Numbered citations of a report: `[3]` in the text, `[3] Title — https://...`
 * under References. Markdown knows neither, so the markers stay plain text
 * and the reference lines (single newlines) melt into one paragraph. This
 * turns each marker into a link to its source and each reference into its
 * own list item. A marker without a reference, or a reference without a URL,
 * stays as written: nothing is invented.
 */

export interface Reference {
  n: number;
  title: string;
  url: string | null;
}

const REF_LINE = /^\s*(?:[-*]\s+)?\[(\d{1,3})\][.:]?\s+(.+?)\s*$/;
const URL_IN = /https?:\/\/[^\s)\]>"'`]+/;
// [3]  [3, 5]  [3-5]  [3–5] — but not a markdown link [text](...) or [3]: def
const MARKER = /\[(\d{1,3}(?:\s*[,;–-]\s*\d{1,3})*)\](?![(:])/g;

export function parseReferences(report: string): Map<number, Reference> {
  const refs = new Map<number, Reference>();
  for (const line of report.split("\n")) {
    const m = REF_LINE.exec(line);
    if (!m) continue;
    const n = Number(m[1]);
    const raw = URL_IN.exec(m[2])?.[0] ?? null;
    const url = raw ? raw.replace(/[.,;]+$/, "") : null;
    const title = (raw ? m[2].replace(raw, "") : m[2]).replace(/[\s—–:-]+$/, "").replace(/^[\s—–:-]+/, "").trim();
    if (!refs.has(n)) refs.set(n, { n, title: title || url || "", url });
  }
  return refs;
}

function expand(spec: string): number[] {
  const out: number[] = [];
  for (const part of spec.split(/\s*[,;]\s*/)) {
    const r = /^(\d+)\s*[–-]\s*(\d+)$/.exec(part);
    if (r) {
      const a = Number(r[1]), b = Number(r[2]);
      if (b >= a && b - a <= 20) for (let i = a; i <= b; i++) out.push(i);
      else return [];
    } else out.push(Number(part));
  }
  return out;
}

const esc = (s: string) => s.replace(/"/g, "'");

/** The report with linked markers and one line per reference. */
export function linkCitations(report: string): string {
  const refs = parseReferences(report);
  if (refs.size === 0) return report;
  let fenced = false;
  return report.split("\n").map((line) => {
    if (/^\s*```/.test(line)) { fenced = !fenced; return line; }
    if (fenced) return line;
    const ref = REF_LINE.exec(line);
    if (ref && refs.has(Number(ref[1]))) {
      const r = refs.get(Number(ref[1]))!;
      if (!r.url) return `- **[${r.n}]** ${r.title}`;
      return `- **[${r.n}]** ${r.title && r.title !== r.url ? `${r.title} — ` : ""}[${r.url}](${r.url})`;
    }
    return line.replace(MARKER, (whole, spec: string) => {
      const ns = expand(spec);
      if (ns.length === 0 || ns.some((n) => !refs.has(n))) return whole;
      return ns.map((n) => {
        const r = refs.get(n)!;
        return r.url ? `[\\[${n}\\]](${r.url} "${esc(r.title)}")` : `\\[${n}\\]`;
      }).join("");
    });
  }).join("\n");
}
