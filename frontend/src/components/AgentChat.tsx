import { useEffect, useRef, useState } from "react";
import { ChevronRight, FileText, Globe, Search, Terminal, Users, AlertTriangle } from "lucide-react";
import type { Agent, ChatItem, JobView, ToolItem } from "../lib/view";
import { ModelChip } from "./BranchView";

interface Props {
  view: JobView;
  selected: string;
  onSelect: (id: string) => void;
  highlightSource?: (url: string | null) => void;
}

const ICON: Record<string, JSX.Element> = {
  search: <Search size={12} />, extract: <Globe size={12} />, write: <FileText size={12} />,
  delegate: <Users size={12} />, shell: <Terminal size={12} />, other: <Terminal size={12} />,
};

function ToolChip({ t }: { t: ToolItem }) {
  const [open, setOpen] = useState(false);
  const color = t.status === "failed" ? "border-red-500/40 text-red-300"
    : t.status === "running" ? "border-sky-500/40 text-sky-300" : "border-base-600 text-slate-300";
  return (
    <div className={`rounded-md border ${color} bg-base-900/60 text-xs`}>
      <button onClick={() => setOpen(!open)} className="w-full flex items-center gap-2 px-2 py-1 text-left">
        <ChevronRight size={12} className={`transition-transform ${open ? "rotate-90" : ""}`} />
        {ICON[t.tool] ?? ICON.other}
        <span className="font-mono text-[11px] uppercase text-slate-500">{t.tool}</span>
        <span className="truncate">{t.label}</span>
        {t.status === "running" && <span className="ml-auto h-1.5 w-1.5 rounded-full bg-sky-400 animate-pulse" />}
      </button>
      {open && (
        <div className="px-2 pb-2 space-y-1">
          {t.args != null && (
            <pre className="whitespace-pre-wrap break-all text-[11px] text-slate-400 bg-base-950 rounded p-1.5 max-h-40 overflow-auto">
              {typeof t.args === "string" ? t.args : JSON.stringify(t.args, null, 2)}
            </pre>
          )}
          {t.result != null && (
            <pre className="whitespace-pre-wrap break-all text-[11px] text-slate-300 bg-base-950 rounded p-1.5 max-h-60 overflow-auto">
              {t.result}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}

function Item({ it }: { it: ChatItem }) {
  switch (it.kind) {
    case "goal":
      return <div className="rounded-lg bg-accent-500/15 border border-accent-500/30 px-3 py-2 text-sm text-slate-100 whitespace-pre-wrap">{it.text}</div>;
    case "thought":
      return (
        <details className="text-xs text-slate-500">
          <summary className="cursor-pointer select-none">thinking… <span className="text-slate-600">{it.text.length.toLocaleString()} chars</span></summary>
          <div className="whitespace-pre-wrap mt-1">{it.text}</div>
        </details>
      );
    case "message":
      return <div className="rounded-lg bg-base-800 px-3 py-2 text-sm text-slate-200 whitespace-pre-wrap break-words">{it.text}</div>;
    case "note":
      return (
        <div className={`flex items-start gap-1.5 text-xs ${it.level === "stop" ? "text-red-300" : it.level === "warn" ? "text-amber-300" : "text-slate-400"}`}>
          <AlertTriangle size={12} className="mt-0.5 shrink-0" /> <span>{it.text}</span>
        </div>
      );
    case "tool":
      return <ToolChip t={it} />;
  }
}

function tabLabel(a: Agent): string {
  if (a.id === "lead") return "lead";
  const m = /(\d+)\s*[—:-]/.exec(a.goal) ?? /sub-question\s*(\d+)/i.exec(a.goal);
  return m ? `${a.role} ${m[1]}` : a.role;
}

/** Right column: a chat per agent; the goal first, tool calls as chips. */
export default function AgentChat({ view, selected, onSelect }: Props) {
  const agent = view.agents.get(selected) ?? view.agents.get("lead");
  const bottom = useRef<HTMLDivElement>(null);
  const [follow, setFollow] = useState(true);
  const items = agent?.items ?? [];
  useEffect(() => {
    if (follow) bottom.current?.scrollIntoView({ block: "end" });
  }, [items.length, follow, selected]);

  return (
    <section className="flex flex-col h-full min-h-0">
      <div className="flex gap-1 overflow-x-auto px-2 pt-2 border-b border-base-800" role="tablist">
        {view.order.map((id) => {
          const a = view.agents.get(id)!;
          return (
            <button key={id} role="tab" aria-selected={id === agent?.id} onClick={() => onSelect(id)}
                    className={`shrink-0 px-2.5 py-1.5 text-xs rounded-t-md border-b-2 font-mono
                      ${id === agent?.id ? "border-accent-500 text-slate-100" : "border-transparent text-slate-500 hover:text-slate-300"}`}>
              {tabLabel(a)} <span className="text-slate-600">{a.toolCalls || ""}</span>
            </button>
          );
        })}
      </div>
      {agent && (
        <div className="px-3 py-1.5 text-[11px] text-slate-500 font-mono flex gap-3 border-b border-base-800">
          <span>{agent.state}</span>
          {agent.model && <ModelChip a={agent} />}
          <span>{agent.toolCalls} calls</span>
          {(agent.tokensIn > 0 || agent.tokensOut > 0) && <span>{agent.tokensIn.toLocaleString()} in · {agent.tokensOut.toLocaleString()} out</span>}
          <label className="ml-auto flex items-center gap-1 cursor-pointer">
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} /> follow
          </label>
        </div>
      )}
      <div className="flex-1 overflow-y-auto p-3 space-y-2" onWheel={(e) => { if (e.deltaY < 0) setFollow(false); }}>
        {items.length === 0 && <div className="text-xs text-slate-500">No steps yet</div>}
        {items.map((it, i) => <Item key={i} it={it} />)}
        <div ref={bottom} />
      </div>
    </section>
  );
}
