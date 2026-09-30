import type { Agent, AgentState, Flow, JobView, ToolItem } from "../lib/view";

interface Props {
  view: JobView;
  selected: string | null;
  focus: string | null;            // a sub-agent opened as its own research, or null for the whole job
  onSelect: (agentId: string) => void;
  onFocus: (agentId: string | null) => void;
  live: boolean;                   // animate data flows (off for finished jobs unless replaying)
}

const COLOR: Record<AgentState, string> = {
  starting: "#64748b", thinking: "#a78bfa", searching: "#38bdf8", reading: "#34d399", writing: "#fbbf24",
  delegating: "#f472b6", working: "#94a3b8", done: "#10b981", failed: "#f87171", stopped: "#ef4444",
};
const LABEL: Record<AgentState, string> = {
  starting: "starting", thinking: "thinking", searching: "searching", reading: "reading", writing: "writing",
  delegating: "delegating", working: "working", done: "done", failed: "failed", stopped: "stopped by guard",
};
const FLOW_COLOR: Record<Flow["kind"], string> = {
  task: "#f472b6", result: "#10b981", query: "#38bdf8", results: "#7dd3fc", fetch: "#34d399", page: "#a7f3d0",
};
const FLOW_LABEL: Record<Flow["kind"], string> = {
  task: "task", result: "findings", query: "query", results: "results", fetch: "open page", page: "page text",
};
const SHORT: Record<string, string> = { researcher: "research", critic: "critic", "fact-checker": "facts", agent: "agent" };
const ACTIVE = new Set<AgentState>(["thinking", "searching", "reading", "writing", "delegating", "working", "starting"]);
const W = 600, H = 360, CX = W / 2, CY = H / 2;

type P = { x: number; y: number };

function ring(n: number, rx: number, ry: number, offset = 0): P[] {
  return Array.from({ length: n }, (_, i) => {
    const a = -Math.PI / 2 + (2 * Math.PI * (i + offset)) / Math.max(n, 1);
    return { x: CX + rx * Math.cos(a), y: CY + ry * Math.sin(a) };
  });
}

function short(n: number): string {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

/** A dot travelling from one node to another once, when the flow appears. */
function Packet({ f, a, b }: { f: Flow; a: P; b: P }) {
  const d = `M${a.x},${a.y} L${b.x},${b.y}`;
  const color = FLOW_COLOR[f.kind];
  return (
    <g className="flow-packet">
      <path d={d} stroke={color} strokeOpacity={0.35} strokeWidth={1.5} fill="none" className="flow-trail" />
      <circle r={f.kind === "task" || f.kind === "result" ? 5 : 3.5} fill={color}>
        <title>{FLOW_LABEL[f.kind]}</title>
        <animateMotion dur={f.kind === "task" || f.kind === "result" ? "1.6s" : "1.1s"} fill="freeze" path={d} />
        <animate attributeName="opacity" values="1;1;0" keyTimes="0;0.8;1" dur="1.8s" fill="freeze" />
      </circle>
    </g>
  );
}

function AgentNode({ a, p, selected, big, onClick }: { a: Agent; p: P; selected: boolean; big: boolean; onClick: () => void }) {
  const rad = big ? 34 : 27;
  const color = COLOR[a.state];
  return (
    <g transform={`translate(${p.x},${p.y})`} className="cursor-pointer" onClick={onClick}
       role="button" aria-label={`${a.role} ${LABEL[a.state]}`}>
      <title>{`${a.role}: ${LABEL[a.state]}\n${a.goal.slice(0, 220)}\n${a.toolCalls} tool calls · ${short(a.tokensIn)} in / ${short(a.tokensOut)} out${a.id !== "lead" ? "\nclick: open this research" : ""}`}</title>
      {ACTIVE.has(a.state) && <circle r={rad + 6} fill="none" stroke={color} strokeOpacity={0.5} strokeWidth={2} className="ag-pulse" />}
      <circle r={rad} fill="#151821" stroke={selected ? "#f8fafc" : color} strokeWidth={selected ? 3 : 2} />
      <text textAnchor="middle" dy="-0.15em" fontSize={big ? 12 : 8.5} fill="#e2e8f0" className="font-mono">
        {a.id === "lead" ? "lead" : SHORT[a.role] ?? a.role.slice(0, 6)}
      </text>
      <text textAnchor="middle" dy="1.25em" fontSize={7.5} fill={color} className="font-mono">
        {a.toolCalls > 0 ? `${a.toolCalls} calls` : LABEL[a.state].slice(0, 9)}
      </text>
    </g>
  );
}

/**
 * Whole job: lead in the centre, sub-agents on a ring, the web (search engine)
 * above, sources on the outer ring. Focus on one sub-agent: it takes the centre,
 * its queries and the pages it read around it, a dimmed lead to go back.
 * New data flows run along the edges as dots.
 */
export default function AgentGraph({ view, selected, focus, onSelect, onFocus, live }: Props) {
  const focusAgent = focus ? view.agents.get(focus) : undefined;
  const pos = new Map<string, P>();
  const WEB: P = { x: CX, y: 26 };
  pos.set("web", WEB);

  let agents: string[];
  let sources = [...view.sources.values()];
  let searches: { item: ToolItem; p: P }[] = [];

  if (focusAgent) {
    agents = [focusAgent.id];
    pos.set(focusAgent.id, { x: CX, y: CY + 10 });
    pos.set("lead", { x: 48, y: 40 });
    sources = sources.filter((s) => s.readers.includes(focusAgent.id));
    const items = focusAgent.items.filter((i): i is ToolItem => i.kind === "tool" && i.tool === "search");
    // two staggered rings so a dozen queries do not pile onto each other
    const outer = ring(items.length, 175, 112, 0.5), inner = ring(items.length, 108, 70, 0.5);
    searches = items.map((item, i) => ({ item, p: i % 2 ? inner[i] : outer[i] }))
      .map(({ item, p }) => ({ item, p: { x: p.x, y: p.y + 10 } }));
  } else {
    agents = view.order;
    pos.set("lead", { x: CX, y: CY + 10 });
    const subs = view.order.filter((id) => id !== "lead");
    ring(subs.length, 170, 105).forEach((p, i) => pos.set(subs[i], { x: p.x, y: p.y + 10 }));
  }
  sources = sources.slice(0, 48);
  ring(sources.length, 280, 158, 0.5).forEach((p, i) => pos.set(sources[i].url, { x: p.x, y: p.y + 10 }));

  const visibleFlows = (live ? view.flows.slice(-24) : [])
    .filter((f) => !focusAgent || f.agent === focusAgent.id)
    .filter((f) => pos.has(f.from) && pos.has(f.to));

  const threads = sources.flatMap((s) =>
    s.readers.filter((r) => pos.has(r)).map((r) => ({ s, a: pos.get(r)!, b: pos.get(s.url)! })));

  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="w-full h-auto select-none" aria-label="agent graph">
      <style>{`
        @keyframes agp { 0% { opacity: .9; transform: scale(1) } 100% { opacity: 0; transform: scale(1.35) } }
        .ag-pulse { transform-box: fill-box; transform-origin: center; animation: agp 1.4s ease-out infinite }
        @keyframes trail { 0% { opacity: .6 } 100% { opacity: 0 } }
        .flow-trail { animation: trail 2s ease-out forwards }
        @media (prefers-reduced-motion: reduce) { .ag-pulse, .flow-trail { animation: none } .flow-packet { display: none } }
      `}</style>

      {/* the web: where queries go */}
      <g transform={`translate(${WEB.x},${WEB.y})`}>
        <title>web search</title>
        <rect x={-30} y={-12} width={60} height={24} rx={12} fill="#0f172a" stroke="#38bdf8" strokeOpacity={0.6} />
        <text textAnchor="middle" dy="0.35em" fontSize={9} fill="#7dd3fc" className="font-mono">web</text>
      </g>

      {threads.map(({ s, a, b }) => (
        <line key={`t-${s.url}-${a.x}-${a.y}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y}
              stroke={s.inReport ? "#34d399" : "#334155"} strokeOpacity={s.inReport ? 0.75 : 0.45}
              strokeWidth={s.inReport ? 1.3 : 0.7} />
      ))}

      {!focusAgent && view.order.filter((id) => id !== "lead").map((id) => {
        const a = pos.get("lead")!, b = pos.get(id)!;
        return <line key={`l-${id}`} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke="#475569" strokeDasharray="3 4" />;
      })}

      {focusAgent && (
        <>
          <line x1={48} y1={40} x2={CX} y2={CY + 10} stroke="#475569" strokeDasharray="3 4" />
          {searches.map(({ item, p }) => (
            <g key={item.id}>
              <line x1={CX} y1={CY + 10} x2={p.x} y2={p.y} stroke="#38bdf8" strokeOpacity={0.35} />
              <line x1={p.x} y1={p.y} x2={WEB.x} y2={WEB.y} stroke="#38bdf8" strokeOpacity={0.12} />
              <g transform={`translate(${p.x},${p.y})`}>
                <title>{`query: ${item.label}`}</title>
                <rect x={-40} y={-8} width={80} height={16} rx={8} fill="#0b1220" stroke="#38bdf8" strokeOpacity={0.5} />
                <text textAnchor="middle" dy="0.35em" fontSize={7} fill="#bae6fd" className="font-mono">
                  {item.label.length > 17 ? `${item.label.slice(0, 16)}…` : item.label}
                </text>
              </g>
            </g>
          ))}
        </>
      )}

      {sources.map((s) => {
        const p = pos.get(s.url)!;
        return (
          <g key={s.url} transform={`translate(${p.x},${p.y})`}>
            <title>{`${s.domain}\n${s.url}${s.inReport ? "\ncited in the report" : ""}`}</title>
            <circle r={s.inReport ? 5 : 3.5} fill={s.inReport ? "#34d399" : "#64748b"} />
            {focusAgent && (
              <text x={7} dy="0.35em" fontSize={7} fill="#94a3b8" className="font-mono">{s.domain.slice(0, 22)}</text>
            )}
          </g>
        );
      })}

      {visibleFlows.map((f) => <Packet key={`f-${f.seq}-${f.kind}`} f={f} a={pos.get(f.from)!} b={pos.get(f.to)!} />)}

      {focusAgent && view.agents.get("lead") && (
        <g onClick={() => onFocus(null)} className="cursor-pointer" opacity={0.75}>
          <AgentNode a={view.agents.get("lead")!} p={{ x: 48, y: 40 }} selected={false} big={false} onClick={() => onFocus(null)} />
          <text x={48} y={78} textAnchor="middle" fontSize={8} fill="#94a3b8" className="font-mono">← whole job</text>
        </g>
      )}

      {agents.map((id) => {
        const a = view.agents.get(id)!;
        return (
          <AgentNode key={id} a={a} p={pos.get(id)!} selected={selected === id} big={id === "lead" || id === focus}
                     onClick={() => {
                       onSelect(id);
                       if (!focusAgent && id !== "lead") onFocus(id);
                     }} />
        );
      })}
    </svg>
  );
}
