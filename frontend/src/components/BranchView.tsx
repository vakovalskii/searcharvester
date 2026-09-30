import type { Agent, ChatItem, JobView } from "../lib/view";
import { agentReport } from "../lib/view";

interface Props {
  view: JobView;
  hasReport: boolean;
  onOpenAgent: (id: string) => void;   // open a sub-agent as its own research
  onOpenLead: () => void;              // show the lead's chat
  onOpenReport: () => void;
}

const DOT: Record<string, string> = {
  starting: "bg-slate-500", thinking: "bg-violet-400", searching: "bg-sky-400", reading: "bg-emerald-400",
  writing: "bg-amber-400", delegating: "bg-pink-400", working: "bg-slate-400", done: "bg-emerald-500",
  failed: "bg-red-400", stopped: "bg-red-500",
};
const ACTIVE = new Set(["starting", "thinking", "searching", "reading", "writing", "delegating", "working"]);

function secs(a: string, b: string): string {
  const s = Math.max(0, (new Date(b).getTime() - new Date(a).getTime()) / 1000);
  const t = Math.round(s);
  return t >= 60 ? `${Math.floor(t / 60)}m ${t % 60}s` : `${t}s`;
}

function k(n: number): string {
  return n >= 1_000_000 ? `${(n / 1_000_000).toFixed(1)}M` : n >= 1000 ? `${Math.round(n / 1000)}k` : String(n);
}

function count(items: ChatItem[], tool: string): number {
  return items.filter((i) => i.kind === "tool" && i.tool === tool && i.status !== "failed").length;
}

/** What the lead did between two delegations: plan, review of the results, the report. */
function leadSegments(lead: Agent | undefined, roundCalls: string[]): ChatItem[][] {
  const segs: ChatItem[][] = [[]];
  for (const it of lead?.items ?? []) {
    if (it.kind === "tool" && roundCalls.includes(it.id)) segs.push([]);
    else segs[segs.length - 1].push(it);
  }
  return segs;
}

function LeadStep({ title, items, onClick }: { title: string; items: ChatItem[]; onClick: () => void }) {
  const tools = items.filter((i) => i.kind === "tool");
  if (tools.length === 0 && !items.some((i) => i.kind === "message" || i.kind === "note")) return null;
  const notes = items.filter((i) => i.kind === "note" && i.level !== "info");
  const labels = tools.slice(-4).map((t) => (t.kind === "tool" ? t.label : "")).filter(Boolean);
  return (
    <button onClick={onClick} className="w-full text-left rounded-lg border border-base-700 bg-base-900/40 px-3 py-2 hover:border-slate-500">
      <div className="flex items-center gap-2 text-xs">
        <span className="font-mono text-slate-300">lead</span>
        <span className="text-slate-500">{title}</span>
        <span className="ml-auto font-mono text-slate-500">{tools.length} steps</span>
      </div>
      {labels.length > 0 && (
        <div className="mt-1 text-[11px] font-mono text-slate-500 truncate">{labels.join(" · ")}</div>
      )}
      {notes.map((n, i) => n.kind === "note" && (
        <div key={i} className={`mt-1 text-[11px] ${n.level === "stop" ? "text-red-300" : "text-amber-300"}`}>{n.text}</div>
      ))}
    </button>
  );
}

function AgentCard({ a, view, onOpen }: { a: Agent; view: JobView; onOpen: () => void }) {
  const pages = [...view.sources.values()].filter((s) => s.readers.includes(a.id));
  const cited = pages.filter((s) => s.inReport).length;
  const answer = agentReport(view, a.id);
  const active = ACTIVE.has(a.state);
  return (
    <button onClick={onOpen}
            className={`text-left rounded-lg border px-3 py-2.5 bg-base-900/60 hover:border-accent-500/70 transition-colors
                        ${active ? "border-sky-500/50" : a.state === "failed" || a.state === "stopped" ? "border-red-500/40" : "border-base-700"}`}>
      <div className="flex items-center gap-2">
        <span className={`w-2 h-2 rounded-full ${DOT[a.state]} ${active ? "animate-pulse" : ""}`} />
        <span className="text-sm font-medium text-slate-100 capitalize">{a.role}</span>
        <span className="ml-auto text-[11px] font-mono text-slate-500">{a.state}</span>
      </div>
      <div className="mt-1 text-xs text-slate-400 line-clamp-2">{a.goal.replace(/^[^:]{1,30}:\s*/, "") || "task not known yet"}</div>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] font-mono text-slate-500">
        <span>{count(a.items, "search")} searches</span>
        <span>{pages.length} pages{cited ? ` · ${cited} cited` : ""}</span>
        <span>{k(a.tokensIn)} tok</span>
        <span>{secs(a.startTs, a.lastTs)}</span>
      </div>
      {answer && <div className="mt-2 text-[11px] text-slate-400 line-clamp-3 border-t border-base-800 pt-1.5">{answer.slice(0, 300)}</div>}
      <div className="mt-1.5 text-[11px] text-accent-400">open this research →</div>
    </button>
  );
}

/**
 * The research as it went: the lead plans, sends a round of sub-agents out,
 * reads what came back, maybe sends another round, then writes the report.
 * Top to bottom, one row per round.
 */
export default function BranchView({ view, hasReport, onOpenAgent, onOpenLead, onOpenReport }: Props) {
  const lead = view.agents.get("lead");
  const segs = leadSegments(lead, view.rounds.map((r) => r.callId));
  const stray = view.order.filter((id) => id !== "lead" && !view.rounds.some((r) => r.agents.includes(id)));
  return (
    <ol className="relative space-y-3 pl-5 before:absolute before:left-1.5 before:top-2 before:bottom-2 before:w-px before:bg-base-700">
      <li className="relative">
        <span className="absolute -left-5 top-3 w-3 h-3 rounded-full border-2 border-slate-300 bg-base-950" />
        <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1">question</div>
        <div className="text-sm text-slate-200">{view.query}</div>
        <div className="mt-2"><LeadStep title="plans the research" items={segs[0] ?? []} onClick={onOpenLead} /></div>
      </li>

      {view.rounds.map((r, i) => (
        <li key={r.callId} className="relative">
          <span className="absolute -left-5 top-1 w-3 h-3 rounded-full border-2 border-pink-400 bg-base-950" />
          <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1.5">
            round {r.index} · {r.agents.length} {r.agents.length === 1 ? "branch" : "branches"} in parallel
          </div>
          <div className="grid gap-2" style={{ gridTemplateColumns: `repeat(${Math.min(Math.max(r.agents.length, 1), 3)}, minmax(0, 1fr))` }}>
            {r.agents.map((id) => <AgentCard key={id} a={view.agents.get(id)!} view={view} onOpen={() => onOpenAgent(id)} />)}
            {r.agents.length === 0 && <div className="text-xs text-slate-500">starting sub-agents…</div>}
          </div>
          <div className="mt-2">
            <LeadStep title={i === view.rounds.length - 1 ? "reads the findings and writes the report" : "reads the findings and plans the next round"}
                      items={segs[i + 1] ?? []} onClick={onOpenLead} />
          </div>
        </li>
      ))}

      {stray.length > 0 && (
        <li className="relative">
          <span className="absolute -left-5 top-1 w-3 h-3 rounded-full border-2 border-slate-500 bg-base-950" />
          <div className="text-[11px] uppercase tracking-wide text-slate-500 mb-1.5">not tied to a round yet</div>
          <div className="grid grid-cols-3 gap-2">
            {stray.map((id) => <AgentCard key={id} a={view.agents.get(id)!} view={view} onOpen={() => onOpenAgent(id)} />)}
          </div>
        </li>
      )}

      <li className="relative">
        <span className={`absolute -left-5 top-1 w-3 h-3 rounded-full border-2 ${hasReport ? "border-emerald-400 bg-emerald-400" : "border-slate-600 bg-base-950"}`} />
        {hasReport
          ? <button onClick={onOpenReport} className="text-sm text-emerald-300 hover:text-emerald-200">report is ready → read it</button>
          : <div className="text-sm text-slate-500">report: not yet</div>}
      </li>
    </ol>
  );
}
