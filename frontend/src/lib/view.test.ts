import { describe, expect, it } from "vitest";
import fixture from "./__fixtures__/deep-job.json";
import twoRounds from "./__fixtures__/two-rounds-job.json";
import type { AgentEvent } from "./api";
import { agentFlows, agentReport, classifyTool, isService, reduce } from "./view";
import { addEvents, emptyRecord, lastSeq, orderedEvents, withJob, type Store } from "./store";

const events = fixture as unknown as AgentEvent[];

const ev = (seq: number, agent_id: string, type: AgentEvent["type"], payload: Record<string, unknown> = {},
            parent_id: string | null = agent_id === "lead" ? null : "lead"): AgentEvent =>
  ({ seq, ts: `2026-09-30T10:00:${String(seq).padStart(2, "0")}Z`, job_id: "j", agent_id, parent_id, type, payload });

describe("reduce on a real deep job (fixture from a live run)", () => {
  const view = reduce(events);

  it("has the lead first and one agent per sub-agent session", () => {
    expect(view.order[0]).toBe("lead");
    const subs = view.order.filter((id) => id !== "lead");
    expect(subs.length).toBeGreaterThanOrEqual(3);
    for (const id of subs) expect(view.agents.get(id)!.goal.length).toBeGreaterThan(10);
    const roles = new Set(subs.map((id) => view.agents.get(id)!.role));
    expect(roles.has("researcher")).toBe(true);
    expect(roles.has("critic")).toBe(true);
  });

  it("puts each sub-agent's own steps in its own chat", () => {
    for (const id of view.order.filter((x) => x !== "lead")) {
      const a = view.agents.get(id)!;
      const tools = a.items.filter((i) => i.kind === "tool");
      expect(tools.length).toBeGreaterThan(3);
      expect(tools.some((t) => t.kind === "tool" && t.tool === "search")).toBe(true);
      expect(a.tokensIn).toBeGreaterThan(0);
    }
  });

  it("pairs every finished tool call with its result", () => {
    for (const a of view.agents.values()) {
      const done = a.items.filter((i) => i.kind === "tool" && i.status !== "running");
      for (const t of done) if (t.kind === "tool") expect(t.result).not.toBeNull();
    }
  });

  it("collects sources with their readers", () => {
    expect(view.sources.size).toBeGreaterThan(0);
    for (const s of view.sources.values()) {
      expect(s.readers.length).toBeGreaterThan(0);
      expect(s.domain).not.toMatch(/^https?:/);
    }
  });

  it("reads depth, query and limits from the lead spawn", () => {
    expect(view.depth).toBe("deep");
    expect(view.query).toMatch(/SGLang/);
    expect(view.guard.limits.max_searches).toBeGreaterThan(0);
  });

  it("is deterministic and prefix-consistent (replay slider)", () => {
    const again = reduce(events);
    expect([...again.agents.keys()]).toEqual([...view.agents.keys()]);
    const half = reduce(events.slice(0, Math.floor(events.length / 2)));
    expect(half.lastSeq).toBeLessThan(view.lastSeq);
  });
});

describe("reduce on edge cases", () => {
  it("glues a provisional agent into its task on alias", () => {
    const v = reduce([
      ev(1, "lead", "spawn", { query: "q", depth: "deep" }),
      ev(2, "sub-db-aaaa", "spawn", { goal: "Researcher: x", unmatched: true }),
      ev(3, "sub-db-aaaa", "tool_call", { id: "c1", title: "terminal: python3 search.py --query \"x\"" }),
      ev(4, "sub-ab-1", "note", { kind: "alias", from: "sub-db-aaaa", to: "sub-ab-1" }),
      ev(5, "sub-ab-1", "tool_result", { id: "c1", status: "completed", content: "ok" }),
      ev(6, "sub-ab-1", "done", { status: "completed" }),
    ]);
    expect(v.agents.has("sub-db-aaaa")).toBe(false);
    const a = v.agents.get("sub-ab-1")!;
    expect(a.state).toBe("done");
    const tool = a.items.find((i) => i.kind === "tool");
    expect(tool && tool.kind === "tool" && tool.result).toBe("ok");
  });

  it("shows the guard stop on the lead and keeps the reason", () => {
    const v = reduce([
      ev(1, "lead", "spawn", { query: "q", limits: { max_llm_calls: 30 } }),
      ev(2, "lead", "note", { kind: "guard", action: "warn", reason: "llm_calls at 24 of 30", llm_calls: 24 }),
      ev(3, "lead", "note", { kind: "guard", action: "stop", reason: "llm_calls budget reached", llm_calls: 30 }),
      ev(4, "lead", "done", { status: "completed", stopped_by_guard: "llm_calls budget reached" }),
    ]);
    expect(v.guard.stoppedBy).toBe("llm_calls budget reached");
    expect(v.guard.counters.llm_calls).toBe(30);
    expect(v.guard.warnings).toHaveLength(1);
  });

  it("merges streamed lead message chunks into one bubble", () => {
    const v = reduce([ev(1, "lead", "message", { text: "Hel" }), ev(2, "lead", "message", { text: "lo" })]);
    const msgs = v.agents.get("lead")!.items.filter((i) => i.kind === "message");
    expect(msgs).toHaveLength(1);
    expect(msgs[0].kind === "message" && msgs[0].text).toBe("Hello");
  });

  it("hides service events but not guard notes", () => {
    expect(isService(ev(1, "lead", "commands"))).toBe(true);
    expect(isService(ev(1, "lead", "note", { kind: "UsageUpdate" }))).toBe(true);
    expect(isService(ev(1, "lead", "note", { kind: "permission" }))).toBe(true);
    expect(isService(ev(1, "lead", "note", { kind: "guard" }))).toBe(false);
  });

  it("classifies tool calls for chips", () => {
    expect(classifyTool("terminal: python3 /x/search.py --query \"vllm moe\" --max-results 5", null))
      .toEqual({ tool: "search", label: "vllm moe" });
    expect(classifyTool("terminal: python3 /x/extract.py --url \"https://www.docs.vllm.ai/a\" --size f", null).label)
      .toBe("docs.vllm.ai");
    expect(classifyTool("write_file: /srv/jobs/x/report.md", { path: "/srv/jobs/x/report.md" }))
      .toEqual({ tool: "write", label: "report.md" });
  });
});

describe("data flows and drill-down", () => {
  const job = [
    ev(1, "lead", "spawn", { query: "q", depth: "deep" }),
    ev(2, "sub-a-1", "spawn", { goal: "Researcher: find x" }),
    ev(3, "sub-a-1", "tool_call", { id: "s1", title: "terminal: python3 search.py --query \"x\"" }),
    ev(4, "sub-a-1", "tool_result", { id: "s1", status: "completed", content: "https://ex.org/a" }),
    ev(5, "sub-a-1", "tool_call", { id: "e1", title: "terminal: python3 extract.py --url \"https://ex.org/a\"" }),
    ev(6, "sub-a-1", "tool_result", { id: "e1", status: "completed", content: "page" }),
    ev(7, "sub-a-1", "message", { text: "x is 42 [ex.org](https://ex.org/a)" }),
    ev(8, "sub-a-1", "done", { status: "completed" }),
  ];

  it("emits one flow per hop, in order, each with its own seq", () => {
    const v = reduce(job);
    expect(v.flows.map((f) => [f.kind, f.from, f.to])).toEqual([
      ["task", "lead", "sub-a-1"],
      ["query", "sub-a-1", "web"],
      ["results", "web", "sub-a-1"],
      ["fetch", "sub-a-1", "https://ex.org/a"],
      ["page", "https://ex.org/a", "sub-a-1"],
      ["result", "sub-a-1", "lead"],
    ]);
    expect(new Set(v.flows.map((f) => `${f.seq}-${f.kind}`)).size).toBe(v.flows.length);
    expect(v.flows.every((f) => f.agent === "sub-a-1")).toBe(true);
  });

  it("a failed sub-agent sends no findings back", () => {
    const v = reduce([...job.slice(0, 7), ev(8, "sub-a-1", "done", { status: "failed" })]);
    expect(v.flows.some((f) => f.kind === "result")).toBe(false);
  });

  it("flows follow an alias to the real agent id", () => {
    const v = reduce([
      ev(1, "lead", "spawn", { query: "q" }),
      ev(2, "sub-db-aaaa", "spawn", { goal: "Researcher: x", unmatched: true }),
      ev(3, "sub-db-aaaa", "tool_call", { id: "c1", title: "terminal: python3 search.py --query \"x\"" }),
      ev(4, "sub-ab-1", "note", { kind: "alias", from: "sub-db-aaaa", to: "sub-ab-1" }),
    ]);
    const nodes = v.flows.flatMap((f) => [f.from, f.to, f.agent]);
    expect(nodes).not.toContain("sub-db-aaaa");
    expect(agentFlows(v, "sub-ab-1").length).toBeGreaterThan(0);
  });

  it("budget bars keep moving between guard notes", () => {
    const v = reduce([
      ev(1, "lead", "note", { kind: "guard", action: "warn", reason: "r", searches: 0, extracts: 0 }),
      ...job.slice(1),
    ]);
    expect(v.guard.counters.searches).toBe(1);
    expect(v.guard.counters.extracts).toBe(1);
    const later = reduce([...job, ev(9, "lead", "note", { kind: "guard", action: "warn", reason: "r", searches: 7 })]);
    expect(later.guard.counters.searches).toBe(7);
  });

  it("drill-down shows the sub-agent's own answer as its report", () => {
    const v = reduce(job);
    expect(agentReport(v, "sub-a-1")).toContain("x is 42");
    expect(agentReport(reduce(job.slice(0, 6)), "sub-a-1")).toBeNull();
    expect(agentReport(v, "nobody")).toBeNull();
  });

  it("on the real job every sub-agent has a task in and its flows stay its own", () => {
    const v = reduce(events);
    for (const id of v.order.filter((x) => x !== "lead")) {
      const fl = agentFlows(v, id);
      expect(fl.some((f) => f.kind === "task" && f.to === id)).toBe(true);
      expect(fl.some((f) => f.kind === "query")).toBe(true);
      for (const f of fl) expect([f.from, f.to]).toContain(id);
    }
  });
});

describe("per-job store", () => {
  it("dedupes a replayed stream by seq", () => {
    let s: Store = new Map();
    s = addEvents(s, "a", [ev(1, "lead", "spawn"), ev(2, "lead", "message", { text: "x" })]);
    s = addEvents(s, "a", [ev(1, "lead", "spawn"), ev(2, "lead", "message", { text: "x" }), ev(3, "lead", "done")]);
    expect(orderedEvents(s.get("a")).map((e) => e.seq)).toEqual([1, 2, 3]);
    expect(lastSeq(s.get("a"))).toBe(3);
  });

  it("a late response for job A does not touch job B", () => {
    let s: Store = new Map([["a", emptyRecord("a", "qa")], ["b", emptyRecord("b", "qb")]]);
    const b = s.get("b");
    s = withJob(s, "a", (r) => ({ ...r, report: "report of A", status: "completed" }));
    s = addEvents(s, "a", [ev(1, "lead", "done")]);
    expect(s.get("b")).toBe(b); // same object: untouched
    expect(s.get("a")!.report).toBe("report of A");
  });

  it("a no-op replay keeps the record identity (no re-render)", () => {
    let s: Store = addEvents(new Map(), "a", [ev(1, "lead", "spawn")]);
    const before = s.get("a");
    s = addEvents(s, "a", [ev(1, "lead", "spawn")]);
    expect(s.get("a")).toBe(before);
  });
});

describe("branches of a two-round job (fixture from a live run)", () => {
  const job = (twoRounds as unknown) as AgentEvent[];
  const v = reduce(job);

  it("splits sub-agents by the delegation that started them", () => {
    expect(v.rounds.map((r) => r.agents.length)).toEqual([3, 2]);
    expect(v.rounds[0].agents.map((id) => v.agents.get(id)!.role)).toEqual(["researcher", "researcher", "researcher"]);
    expect(v.rounds[1].agents.map((id) => v.agents.get(id)!.role).sort()).toEqual(["critic", "fact-checker"]);
  });

  it("glues the session we could not match live into its empty task", () => {
    expect(v.order.some((id) => id.startsWith("sub-db-"))).toBe(false);
    for (const id of v.rounds[0].agents) {
      expect(v.agents.get(id)!.items.filter((i) => i.kind === "tool").length).toBeGreaterThan(10);
    }
    for (const f of v.flows) expect(f.agent.startsWith("sub-db-")).toBe(false);
  });

  it("after the end the budget shows the guard's own totals, not refused calls", () => {
    expect(v.guard.counters.searches).toBe(40);
    expect(v.guard.counters.extracts).toBe(46);
  });

  it("while running, counters never pass their limit", () => {
    const cut = job.filter((e) => !(e.type === "done" && e.agent_id === "lead"));
    const r = reduce(cut);
    expect(r.guard.counters.searches).toBeLessThanOrEqual(r.guard.limits.max_searches);
  });

  it("puts a denied write into the chat of the agent that tried it", () => {
    const r = reduce([
      ev(1, "lead", "spawn", { query: "q" }),
      ev(2, "sub-a-1", "spawn", { goal: "Researcher: a" }),
      ev(3, "lead", "note", { kind: "permission", allowed: false, reason: "outside the job workspace: /tmp/fetch.py" }),
      ev(4, "lead", "tool_result", { id: "edit-approval-2", status: "failed", content: "" }),
      ev(5, "sub-a-1", "tool_call", { id: "w1", title: "write_file: /tmp/fetch.py" }),
      ev(6, "sub-a-1", "tool_result", { id: "w1", status: "failed", content: "Edit approval denied" }),
    ]);
    const sub = r.agents.get("sub-a-1")!;
    expect(sub.items.some((i) => i.kind === "note" && i.text.includes("/tmp/fetch.py"))).toBe(true);
    const lead = r.agents.get("lead")!;
    expect(lead.items.some((i) => i.kind === "note" && i.text.includes("blocked"))).toBe(false);
    expect(lead.state).not.toBe("failed");
  });

  it("does not glue when a round has two candidates", () => {
    const r = reduce([
      ev(1, "lead", "spawn", { query: "q" }),
      ev(2, "lead", "tool_call", { id: "d1", title: "delegate_task: 2 tasks" }),
      ev(3, "sub-a-1", "spawn", { goal: "Researcher: a", delegate_call_id: "d1" }),
      ev(4, "sub-a-2", "spawn", { goal: "Researcher: b", delegate_call_id: "d1" }),
      ev(5, "sub-db-x", "spawn", { goal: "", unmatched: true }),
      ev(6, "sub-db-x", "tool_call", { id: "c1", title: "terminal: python3 search.py --query \"x\"" }),
    ]);
    expect(r.agents.has("sub-db-x")).toBe(true);
    expect(r.rounds[0].agents).toContain("sub-db-x");
  });
});
