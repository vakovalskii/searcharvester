import { describe, expect, it } from "vitest";
import { linkCitations, parseReferences } from "./citations";

const REPORT = `## TL;DR
Qwen needs 21.9 GB [12], bare weights 16.3 GB [13][14]. See also [2, 3] and [99].
A link [text](https://x.io) and a code block:
\`\`\`
arr[1]
\`\`\`

## References

[2] BenchLM.ai comparison — https://benchlm.ai/compare
[3] Aurora blog — https://aurorainference.com/blog/x.
[12] llmrun.dev table — https://llmrun.dev/model/q
[13] knightli.com guide — https://knightli.com/en/2026/05/01/t/
[14] willitrunai.com estimates — [tentative — single source]
`;

describe("citations", () => {
  it("reads numbered references with and without a URL", () => {
    const refs = parseReferences(REPORT);
    expect(refs.get(3)).toEqual({ n: 3, title: "Aurora blog", url: "https://aurorainference.com/blog/x" });
    expect(refs.get(14)!.url).toBeNull();
    expect(refs.has(99)).toBe(false);
  });

  it("links markers to their sources and leaves the rest alone", () => {
    const out = linkCitations(REPORT);
    expect(out).toContain('21.9 GB [\\[12\\]](https://llmrun.dev/model/q "llmrun.dev table")');
    expect(out).toContain('[\\[13\\]](https://knightli.com/en/2026/05/01/t/ "knightli.com guide")\\[14\\]');
    expect(out).toContain("[\\[2\\]](https://benchlm.ai/compare");
    expect(out).toContain("and [99].");                       // no such reference: as written
    expect(out).toContain("[text](https://x.io)");            // a real markdown link stays
    expect(out).toContain("arr[1]");                          // code is not touched
  });

  it("puts every reference on its own line", () => {
    const out = linkCitations(REPORT);
    expect(out).toContain("- **[12]** llmrun.dev table — [https://llmrun.dev/model/q](https://llmrun.dev/model/q)");
    expect(out).toContain("- **[14]** willitrunai.com estimates — [tentative — single source]");
  });

  it("a report without references is returned as is", () => {
    expect(linkCitations("plain [1] text")).toBe("plain [1] text");
  });
});
