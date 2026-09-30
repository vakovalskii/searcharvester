import { afterEach, describe, expect, it, vi } from "vitest";
import { cancelJob, createResearch } from "./api";

describe("api: every state-changing call carries the client header (stage 0)", () => {
  afterEach(() => vi.unstubAllGlobals());

  const capture = () => {
    const calls: { url: string; init: RequestInit }[] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit) => {
      calls.push({ url, init });
      return new Response(JSON.stringify({ job_id: "0123456789abcdef", status: "queued" }), { status: 202 });
    }));
    return calls;
  };

  it("createResearch sends the header and the depth", async () => {
    const calls = capture();
    await createResearch("q", "deep");
    const h = calls[0].init.headers as Record<string, string>;
    expect(h["X-Searcharvester-Client"]).toBe("1");
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ query: "q", depth: "deep" });
  });

  it("createResearch defaults to quick", async () => {
    const calls = capture();
    await createResearch("q");
    expect(JSON.parse(String(calls[0].init.body)).depth).toBe("quick");
  });

  it("cancelJob sends the header", async () => {
    const calls = capture();
    await cancelJob("0123456789abcdef");
    expect(calls[0].init.method).toBe("DELETE");
    expect((calls[0].init.headers as Record<string, string>)["X-Searcharvester-Client"]).toBe("1");
  });
});
