/**
 * The whole screen of one job as a pure function of its events (stage B of
 * docs/ui-and-visualization.md). Graph, chats, sources and budgets all come from
 * `reduce(events)`, so a replay slider later is just `reduce(events.slice(0, t))`.
 */
import type { AgentEvent } from "./api";

export type AgentState =
  | "starting" | "thinking" | "searching" | "reading" | "writing"
  | "delegating" | "working" | "done" | "failed" | "stopped";

export interface ToolItem {
  kind: "tool";
  id: string;
  tool: string;          // search | extract | write | delegate | shell | other
  label: string;         // one line for the chip: query, domain, file
  args: unknown;
  result: string | null;
  status: "running" | "completed" | "failed";
  ts: string;
}

export type ChatItem =
  | { kind: "goal"; text: string; ts: string }
  | { kind: "thought"; text: string; ts: string }
  | { kind: "message"; text: string; ts: string }
  | { kind: "note"; text: string; level: "info" | "warn" | "stop"; ts: string }
  | ToolItem;

export interface Agent {
  id: string;
  parent: string | null;
  role: string;
  goal: string;
  state: AgentState;
  items: ChatItem[];
  toolCalls: number;
  tokensIn: number;
  tokensOut: number;
  unmatched: boolean;
  lastTs: string;
}

export interface Source {
  url: string;
  domain: string;
  readers: string[];     // agent ids that ran extract on it
  inReport: boolean;
}

/** One piece of data moving between two nodes: what the graph animates. */
export interface Flow {
  seq: number;
  ts: string;
  from: string;   // agent id, "web", or a source url
  to: string;
  kind: "task" | "result" | "query" | "results" | "fetch" | "page";
  agent: string;  // the agent this flow belongs to (for focus mode)
}

export interface GuardView {
  counters: Record<string, number>;
  limits: Record<string, number>;
  stoppedBy: string | null;
  warnings: string[];
}

export interface JobView {
  query: string;
  depth: string;
  agents: Map<string, Agent>;
  order: string[];       // lead first, then sub-agents in spawn order
  sources: Map<string, Source>;
  guard: GuardView;
  status: string | null; // lead's terminal status once done
  lastSeq: number;
  flows: Flow[];
}

/** Events a person does not need to read; hidden unless "service events" is on. */
export function isService(ev: AgentEvent): boolean {
  if (ev.type === "commands" || ev.type === "usage" || ev.type === "plan") return true;
  if (ev.type === "note") {
    const kind = String(ev.payload.kind ?? "");
    return kind !== "guard" && kind !== "steer" && kind !== "queued";
  }
  return false;
}

const URL_RE = /https?:\/\/[^\s)\]>"'`]+/g;

function domainOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function roleOf(goal: string, id: string): string {
  if (id === "lead") return "lead";
  const g = goal.toLowerCase();
  if (g.includes("critic") || g.includes("критик")) return "critic";
  if (g.includes("fact") || g.includes("проверк")) return "fact-checker";
  if (g.includes("research") || g.includes("исслед")) return "researcher";
  return "agent";
}

/** What a tool call is, from its title/command, for the chip and the node state. */
export function classifyTool(title: string, args: unknown): { tool: string; label: string } {
  const a = (args && typeof args === "object" ? (args as Record<string, unknown>) : {}) as Record<string, unknown>;
  const text = `${title} ${typeof a.command === "string" ? a.command : ""}`;
  const q = /--query\s+(?:\\?["'])?(.+?)(?:\\?["'])?(?:\s+--|$)/.exec(text);
  if (text.includes("search.py")) return { tool: "search", label: q ? q[1] : "search" };
  const u = /--url\s+(?:\\?["'])?(\S+?)(?:\\?["'])?(?:\s|$)/.exec(text);
  if (text.includes("extract.py")) return { tool: "extract", label: u ? domainOf(u[1]) : "page" };
  if (/delegate/i.test(title)) return { tool: "delegate", label: title.replace(/^delegate[_ ]task:\s*/i, "") };
  if (/write_file|patch/i.test(title)) {
    const file = typeof a.path === "string" ? a.path : title.split(/[:\s]/).pop() ?? "";
    return { tool: "write", label: String(file).split("/").pop() ?? "" };
  }
  if (/^terminal|execute/i.test(title)) return { tool: "shell", label: title.replace(/^terminal:\s*/, "") };
  return { tool: "other", label: title };
}

function stateForTool(tool: string): AgentState {
  return tool === "search" ? "searching" : tool === "extract" ? "reading"
    : tool === "write" ? "writing" : tool === "delegate" ? "delegating" : "working";
}

function newAgent(id: string, parent: string | null, goal: string, ts: string, unmatched = false): Agent {
  return {
    id, parent, goal, role: roleOf(goal, id), state: "starting", items: goal ? [{ kind: "goal", text: goal, ts }] : [],
    toolCalls: 0, tokensIn: 0, tokensOut: 0, unmatched, lastTs: ts,
  };
}

export function reduce(events: AgentEvent[]): JobView {
  const view: JobView = {
    query: "", depth: "", agents: new Map(), order: [], sources: new Map(),
    guard: { counters: {}, limits: {}, stoppedBy: null, warnings: [] }, status: null, lastSeq: 0, flows: [],
  };
  const flow = (ev: AgentEvent, from: string, to: string, kind: Flow["kind"], agentId: string) =>
    view.flows.push({ seq: ev.seq ?? 0, ts: ev.ts, from, to, kind, agent: agentId });
  const callTarget = new Map<string, { tool: string; url?: string }>(); // tool call id -> what it touched
  const alias = new Map<string, string>(); // provisional sub-db-* id -> canonical id
  const canon = (id: string) => alias.get(id) ?? id;

  const agent = (id: string, parent: string | null, ts: string): Agent => {
    let a = view.agents.get(id);
    if (!a) {
      a = newAgent(id, parent, "", ts);
      view.agents.set(id, a);
      view.order.push(id);
    }
    return a;
  };

  for (const ev of events) {
    view.lastSeq = Math.max(view.lastSeq, ev.seq ?? 0);
    const p = ev.payload ?? {};

    // Alias first: it re-homes everything the provisional id collected so far.
    if (ev.type === "note" && p.kind === "alias") {
      const from = String(p.from), to = String(p.to);
      alias.set(from, to);
      const src = view.agents.get(from);
      if (src) {
        const dst = view.agents.get(to);
        if (dst) {
          dst.items = [...dst.items, ...src.items.filter((i) => i.kind !== "goal")];
          dst.toolCalls += src.toolCalls;
          dst.tokensIn = Math.max(dst.tokensIn, src.tokensIn);
          dst.tokensOut = Math.max(dst.tokensOut, src.tokensOut);
          dst.state = src.state;
          dst.lastTs = src.lastTs;
          if (!dst.goal) { dst.goal = src.goal; dst.role = roleOf(src.goal, to); dst.items.unshift(...src.items.filter((i) => i.kind === "goal")); }
        } else {
          view.agents.set(to, { ...src, id: to, unmatched: false });
          view.order[view.order.indexOf(from)] = to;
        }
        view.agents.delete(from);
        view.order = view.order.filter((x) => x !== from);
        for (const s of view.sources.values()) s.readers = s.readers.map((r) => (r === from ? to : r));
        for (const f of view.flows) {
          if (f.from === from) f.from = to;
          if (f.to === from) f.to = to;
          if (f.agent === from) f.agent = to;
        }
      }
      continue;
    }

    const id = canon(ev.agent_id);
    const parent = ev.parent_id ? canon(ev.parent_id) : null;

    switch (ev.type) {
      case "spawn": {
        if (id === "lead") {
          view.query = String(p.query ?? "");
          view.depth = String(p.depth ?? "");
          view.guard.limits = (p.limits as Record<string, number>) ?? {};
          agent("lead", null, ev.ts).state = "thinking";
        } else {
          const goal = String(p.goal ?? "");
          const existing = view.agents.get(id);
          if (existing) {
            if (!existing.goal && goal) {
              existing.goal = goal; existing.role = roleOf(goal, id);
              existing.items.unshift({ kind: "goal", text: goal, ts: ev.ts });
            }
          } else {
            view.agents.set(id, newAgent(id, parent ?? "lead", goal, ev.ts, Boolean(p.unmatched)));
            view.order.push(id);
            flow(ev, parent ?? "lead", id, "task", id); // one task hop per agent, even if we learned of it late
          }
        }
        break;
      }
      case "thought": {
        const a = agent(id, parent, ev.ts);
        const text = String(p.text ?? "");
        const last = a.items[a.items.length - 1];
        if (last && last.kind === "thought") last.text += text; else a.items.push({ kind: "thought", text, ts: ev.ts });
        a.state = "thinking"; a.lastTs = ev.ts;
        break;
      }
      case "message": {
        const a = agent(id, parent, ev.ts);
        const text = String(p.text ?? "");
        const last = a.items[a.items.length - 1];
        if (last && last.kind === "message" && id === "lead") last.text += text; // streamed chunks
        else a.items.push({ kind: "message", text, ts: ev.ts });
        a.lastTs = ev.ts;
        for (const url of text.match(URL_RE) ?? []) {
          const s = view.sources.get(url);
          if (s) s.inReport = s.inReport || id === "lead";
        }
        break;
      }
      case "tool_call": {
        const a = agent(id, parent, ev.ts);
        const title = String(p.title ?? p.tool ?? "tool");
        const { tool, label } = classifyTool(title, p.raw_input);
        a.items.push({ kind: "tool", id: String(p.id ?? `${ev.seq}`), tool, label, args: p.raw_input ?? p.preview ?? null,
                       result: null, status: "running", ts: ev.ts });
        a.toolCalls += 1; a.state = stateForTool(tool); a.lastTs = ev.ts;
        callTarget.set(String(p.id ?? ""), { tool });
        if (tool === "search") flow(ev, id, "web", "query", id);
        if (tool === "extract") {
          const m = /--url\s+(?:\\?["'])?(\S+?)(?:\\?["'])?(?:\s|$)/.exec(`${title} ${JSON.stringify(p.raw_input ?? "")}`);
          if (m) {
            const url = m[1].replace(/\\+$/, "");
            const s = view.sources.get(url) ?? { url, domain: domainOf(url), readers: [], inReport: false };
            if (!s.readers.includes(id)) s.readers.push(id);
            view.sources.set(url, s);
            callTarget.set(String(p.id ?? ""), { tool, url });
            flow(ev, id, url, "fetch", id);
          }
        }
        break;
      }
      case "tool_result": {
        const a = agent(id, parent, ev.ts);
        const item = [...a.items].reverse().find((i): i is ToolItem => i.kind === "tool" && i.id === String(p.id));
        if (item) {
          item.result = String(p.content ?? "");
          item.status = p.status === "failed" ? "failed" : "completed";
        }
        const target = callTarget.get(String(p.id ?? ""));
        if (target?.tool === "search" && p.status !== "failed") flow(ev, "web", id, "results", id);
        if (target?.tool === "extract" && target.url && p.status !== "failed") flow(ev, target.url, id, "page", id);
        if (a.state !== "done") a.state = "thinking";
        a.lastTs = ev.ts;
        break;
      }
      case "usage": {
        const a = agent(id, parent, ev.ts);
        a.tokensIn = Number(p.input_tokens ?? a.tokensIn);
        a.tokensOut = Number(p.output_tokens ?? a.tokensOut);
        break;
      }
      case "note": {
        if (p.kind === "guard") {
          const counters: Record<string, number> = {};
          for (const k of ["llm_calls", "input_tokens", "output_tokens", "searches", "extracts", "duplicates"]) {
            if (typeof p[k] === "number") counters[k] = p[k] as number;
          }
          view.guard.counters = { ...view.guard.counters, ...counters };
          const reason = String(p.reason ?? "");
          if (p.action === "warn") view.guard.warnings.push(reason);
          if (p.action === "stop" && !view.guard.stoppedBy) view.guard.stoppedBy = reason;
          const level = p.action === "stop" ? "stop" : p.action === "warn" ? "warn" : "info";
          agent("lead", null, ev.ts).items.push({ kind: "note", text: `${p.action}: ${reason}`, level, ts: ev.ts });
          if (p.action === "stop") agent("lead", null, ev.ts).state = "stopped";
        } else if (p.kind === "usage") {
          view.guard.counters = { ...view.guard.counters, ...(p as Record<string, number>) };
        }
        break;
      }
      case "done": {
        const a = agent(id, parent, ev.ts);
        const st = String(p.status ?? "completed");
        a.state = st === "completed" ? "done" : view.guard.stoppedBy && id === "lead" ? "stopped" : st === "unknown" ? "done" : "failed";
        a.lastTs = ev.ts;
        if (id !== "lead" && st === "completed") flow(ev, id, "lead", "result", id);
        if (id === "lead") {
          view.status = st;
          if (p.guard && typeof p.guard === "object") view.guard.counters = { ...view.guard.counters, ...(p.guard as Record<string, number>) };
          if (p.limits && typeof p.limits === "object") view.guard.limits = p.limits as Record<string, number>;
          if (typeof p.stopped_by_guard === "string") view.guard.stoppedBy = p.stopped_by_guard;
        }
        break;
      }
    }
  }
  // lead first
  view.order = ["lead", ...view.order.filter((x) => x !== "lead")].filter((x) => view.agents.has(x));
  // The guard publishes its counters only with a warning or at the end, so between
  // those the bars would freeze. What the events show is a floor for them.
  const seen = { searches: 0, extracts: 0, input_tokens: 0, output_tokens: 0 };
  const urls = new Set<string>();
  for (const a of view.agents.values()) {
    seen.input_tokens += a.tokensIn;
    seen.output_tokens += a.tokensOut;
    for (const it of a.items) {
      if (it.kind !== "tool" || it.status === "failed") continue;
      if (it.tool === "search") seen.searches++;
    }
  }
  for (const f of view.flows) if (f.kind === "fetch") urls.add(f.to);
  seen.extracts = urls.size;
  for (const [k, v] of Object.entries(seen)) {
    view.guard.counters[k] = Math.max(view.guard.counters[k] ?? 0, v);
  }
  return view;
}

/** Mark sources cited in the final report. */
export function markReportSources(view: JobView, report: string | null): JobView {
  if (!report) return view;
  const cited = new Set(report.match(URL_RE) ?? []);
  for (const s of view.sources.values()) if (cited.has(s.url)) s.inReport = true;
  return view;
}

/** What a sub-agent handed back: its last message, or null while it has not answered yet. */
export function agentReport(view: JobView, id: string): string | null {
  const a = view.agents.get(id);
  if (!a) return null;
  for (let i = a.items.length - 1; i >= 0; i--) {
    const it = a.items[i];
    if (it.kind === "message" && it.text.trim()) return it.text;
  }
  return null;
}

/** Everything one agent sent or received, in order: the drill-down timeline. */
export function agentFlows(view: JobView, id: string): Flow[] {
  return view.flows.filter((f) => f.agent === id);
}
