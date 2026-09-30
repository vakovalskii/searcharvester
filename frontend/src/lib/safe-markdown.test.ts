import { describe, expect, it } from "vitest";
import { mediaUrl } from "./api";

declare global {
  interface ImportMeta {
    glob: (pattern: string, opts: { query: string; import: string; eager: true }) => Record<string, string>;
  }
}

// Sources as text, by Vite: no Node APIs, so the image build type-checks it too.
const SOURCES = import.meta.glob("../**/*.tsx", { query: "?raw", import: "default", eager: true });

describe("one markdown policy", () => {
  it("renders agent markdown only through SafeMarkdown", () => {
    expect(Object.keys(SOURCES).length).toBeGreaterThan(5);
    const direct = Object.entries(SOURCES)
      .filter(([p, src]) => !p.endsWith("SafeMarkdown.tsx") && /from "react-markdown"/.test(src))
      .map(([p]) => p);
    expect(direct).toEqual([]);
  });
  it("sends pictures through the adapter", () => {
    expect(mediaUrl("0123456789abcdef", "https://x.io/a b.png?x=1&y=2"))
      .toMatch(/\/media\?job=0123456789abcdef&src=https%3A%2F%2Fx\.io%2Fa%20b\.png%3Fx%3D1%26y%3D2$/);
  });
});
