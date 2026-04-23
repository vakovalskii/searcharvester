import {
  Search,
  FileText,
  AlertTriangle,
  CheckCircle2,
  Terminal,
  Save,
  Cpu,
  Container as ContainerIcon,
  Users,
  MessageSquare,
  BrainCog,
  Loader2,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import { AgentEvent } from "../lib/api";

interface Props {
  events: AgentEvent[];
}

/**
 * Hierarchical timeline:
 *   Orchestrator → Lead agent → (on delegate_task) sub-agent cards in parallel
 *
 * Sub-agent events are rendered inline under the lead's delegate_task tool_call
 * so the tree structure is obvious.
 */
export default function LogTimeline({ events }: Props) {
  if (events.length === 0) {
    return <div className="text-slate-500 text-sm py-2">Awaiting events…</div>;
  }

  // Group: lead events in order, sub-agent events grouped by agent_id under
  // their originating delegate_task tool_call.
  const leadEvents = events.filter((e) => e.agent_id === "lead");
  const subEvents = events.filter((e) => e.agent_id !== "lead");

  // Group sub-agent events by agent_id (globally unique across batches).
  // Track delegate_call_id separately so we can match buckets back to the
  // lead's delegate_task tool_call for inline rendering.
  const subsBySubId = new Map<string, {
    subId: string;
    events: AgentEvent[];
    goal: string;
    status: string;
    callId: string;
  }>();
  for (const ev of subEvents) {
    const subId = ev.agent_id;
    const bucket = subsBySubId.get(subId) ?? {
      subId,
      events: [],
      goal: "",
      status: "running",
      callId: "",
    };
    bucket.events.push(ev);
    if (ev.type === "spawn" && typeof ev.payload.goal === "string") {
      bucket.goal = ev.payload.goal;
    }
    if (typeof ev.payload.delegate_call_id === "string" && !bucket.callId) {
      bucket.callId = ev.payload.delegate_call_id as string;
    }
    if (ev.type === "done") {
      bucket.status = String(ev.payload.status ?? "completed");
    }
    subsBySubId.set(subId, bucket);
  }

  const subsForCall = (toolCallId: string | undefined) => {
    if (!toolCallId) return [];
    const out = [];
    for (const bucket of subsBySubId.values()) {
      if (bucket.callId === toolCallId) out.push(bucket);
    }
    out.sort((a, b) => {
      const ai = spawnTaskIndex(a.events);
      const bi = spawnTaskIndex(b.events);
      return ai - bi;
    });
    return out;
  };

  const toolCallCount = leadEvents.filter((e) => e.type === "tool_call").length;
  const totalSubs = subsBySubId.size;

  return (
    <div className="space-y-3">
      {/* Summary strip */}
      <div className="flex items-start gap-2 px-2.5 py-1.5 rounded-md bg-base-800/60 border border-base-700 text-xs text-slate-400">
        <Cpu size={12} className="shrink-0 mt-0.5 text-slate-500" />
        <div className="leading-relaxed">
          <span className="text-slate-300 font-semibold">{events.length}</span>{" "}
          events · <span className="text-slate-300">{toolCallCount}</span>{" "}
          lead tool calls
          {totalSubs > 0 && (
            <>
              {" · "}
              <span className="text-cyan-400 font-semibold">
                {totalSubs} sub-agent{totalSubs === 1 ? "" : "s"} spawned
              </span>
            </>
          )}
        </div>
      </div>

      {/* Lead timeline */}
      <ol className="space-y-1.5 text-sm">
        {leadEvents.map((ev, i) => (
          <li key={i}>
            <div className="flex items-start gap-2.5">{renderEvent(ev)}</div>
            {ev.type === "tool_call" && isDelegateTitle(ev.payload.title as string | undefined) && (
              <SubAgentPanel buckets={subsForCall(ev.payload.id as string | undefined)} />
            )}
          </li>
        ))}
      </ol>
    </div>
  );
}

function spawnTaskIndex(evs: AgentEvent[]): number {
  const sp = evs.find((e) => e.type === "spawn");
  if (!sp) return 9999;
  const ti = sp.payload.task_index;
  return typeof ti === "number" ? ti : 9999;
}

function isDelegateTitle(title: string | undefined): boolean {
  if (!title) return false;
  return title.toLowerCase().includes("delegate");
}

function SubAgentPanel({
  buckets,
}: {
  buckets: { subId: string; events: AgentEvent[]; goal: string; status: string }[];
}) {
  if (buckets.length === 0) {
    return (
      <div className="ml-4 mt-2 mb-3 pl-3 border-l-2 border-cyan-900/40 text-xs text-slate-500 italic">
        waiting for sub-agent results…
      </div>
    );
  }
  return (
    <div className="ml-4 mt-2 mb-3 pl-3 border-l-2 border-cyan-900/40 space-y-2">
      <div className="text-xs text-cyan-500 uppercase tracking-wider font-semibold flex items-center gap-1.5">
        <Users size={12} /> {buckets.length} parallel sub-agent
        {buckets.length === 1 ? "" : "s"}
      </div>
      <div className="grid gap-2" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))" }}>
        {buckets.map((b) => (
          <SubAgentCard key={b.subId} bucket={b} />
        ))}
      </div>
    </div>
  );
}

function SubAgentCard({
  bucket,
}: {
  bucket: { subId: string; events: AgentEvent[]; goal: string; status: string };
}) {
  const { subId, events, goal, status } = bucket;
  const isDone = status === "completed";
  const isError = status === "error" || status === "failed";
  const isRunning = status === "running";

  const messageEv = events.find((e) => e.type === "message");
  const summary = (messageEv?.payload.text as string | undefined) ?? "";
  const doneEv = events.find((e) => e.type === "done");
  const diagnostic =
    (doneEv?.payload.note as string | undefined) ??
    (doneEv?.payload.error as string | undefined) ??
    "";

  return (
    <div
      className={`rounded-lg border bg-base-900/60 ${
        isDone
          ? "border-emerald-700/40"
          : isError
          ? "border-red-700/40"
          : "border-cyan-700/40"
      }`}
    >
      <div className="px-2.5 py-1.5 border-b border-base-800 flex items-center gap-2">
        <div className="shrink-0 text-cyan-400">
          <Users size={12} />
        </div>
        <div className="flex-1 min-w-0">
          <div className="text-cyan-300 font-mono text-xs truncate">
            {subId}
          </div>
        </div>
        <div className="shrink-0 flex items-center gap-1 text-xs">
          {isRunning && (
            <>
              <Loader2 size={10} className="animate-spin text-cyan-400" />
              <span className="text-cyan-400 uppercase tracking-wider font-semibold">
                live
              </span>
            </>
          )}
          {isDone && (
            <>
              <CheckCircle2 size={10} className="text-emerald-400" />
              <span className="text-emerald-400 uppercase tracking-wider font-semibold">
                done
              </span>
            </>
          )}
          {isError && (
            <>
              <AlertTriangle size={10} className="text-red-400" />
              <span className="text-red-400 uppercase tracking-wider font-semibold">
                fail
              </span>
            </>
          )}
        </div>
      </div>
      {goal && (
        <div className="px-2.5 py-1.5 border-b border-base-800 text-xs text-slate-400 leading-relaxed">
          <span className="text-slate-500 uppercase tracking-wider text-[10px] mr-1">
            goal
          </span>
          {truncate(goal, 200)}
        </div>
      )}
      {summary && (
        <div className="px-2.5 py-2 text-xs text-slate-300 leading-relaxed max-h-64 overflow-y-auto subagent-md">
          <ReactMarkdown>{summary}</ReactMarkdown>
        </div>
      )}
      {!summary && isRunning && (
        <div className="px-2.5 py-2 text-xs text-slate-500 italic">
          researching…
        </div>
      )}
      {!summary && (isDone || isError) && (
        <div className="px-2.5 py-2 text-xs text-slate-500 italic">
          {diagnostic
            ? <>no output · <span className="font-mono text-slate-400">{diagnostic}</span></>
            : "done · summary not captured"}
        </div>
      )}
    </div>
  );
}

function toolCallSubtitle(ev: AgentEvent): string {
  const p = ev.payload as Record<string, unknown>;
  const ri = p.raw_input as Record<string, unknown> | undefined;
  if (!ri) return "";
  if (typeof ri.query === "string") return `query="${truncate(ri.query, 80)}"`;
  if (typeof ri.url === "string") return ri.url as string;
  if (typeof ri.path === "string") return ri.path as string;
  if (typeof ri.command === "string") return truncate(ri.command as string, 90);
  if (typeof ri.name === "string") return ri.name as string;
  if (Array.isArray(ri.tasks)) return `${ri.tasks.length} sub-task${ri.tasks.length === 1 ? "" : "s"}`;
  return truncate(JSON.stringify(ri), 90);
}

function renderEvent(ev: AgentEvent) {
  const p = ev.payload as Record<string, unknown>;

  switch (ev.type) {
    case "spawn":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-fuchsia-400">
            <ContainerIcon size={14} />
          </div>
          <div className="flex-1 min-w-0">
            <span className="text-fuchsia-400 font-semibold uppercase text-xs mr-2 tracking-wider">
              orchestrator
            </span>
            <span className="text-slate-300">
              spawned agent{" "}
              <span className="font-mono text-slate-400">{ev.agent_id}</span>
              {p.query ? ` · ${truncate(String(p.query), 80)}` : ""}
            </span>
          </div>
        </>
      );

    case "commands":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-slate-500">
            <Cpu size={14} />
          </div>
          <div className="flex-1 min-w-0 text-slate-500 text-xs">
            slash commands:{" "}
            <span className="font-mono text-slate-400">
              {Array.isArray(p.names) ? (p.names as string[]).join(", ") : ""}
            </span>
          </div>
        </>
      );

    case "thought":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-violet-400">
            <BrainCog size={13} />
          </div>
          <div className="flex-1 min-w-0 text-violet-300/80 italic text-xs">
            {truncate(String(p.text ?? ""), 160)}
          </div>
        </>
      );

    case "tool_call": {
      const title = String(p.title ?? "tool");
      const isDel = isDelegateTitle(title);
      const [icon, color] = toolIcon(title);
      return (
        <>
          <div className={`shrink-0 mt-0.5 ${color}`}>{icon}</div>
          <div className="flex-1 min-w-0">
            <div className="text-slate-200">
              <span className={`${color} font-semibold uppercase text-xs mr-2`}>
                {isDel ? "delegate" : shortTitle(title)}
              </span>
              <span className="font-mono text-slate-300 break-all">
                {truncate(toolCallSubtitle(ev), 160)}
              </span>
            </div>
          </div>
        </>
      );
    }

    case "tool_result": {
      const err = p.status && String(p.status).toLowerCase().includes("error");
      return (
        <>
          <div className={`shrink-0 mt-0.5 ml-4 ${err ? "text-red-400" : "text-emerald-400"}`}>
            {err ? <AlertTriangle size={12} /> : <CheckCircle2 size={12} />}
          </div>
          <div className="flex-1 min-w-0 text-xs font-mono text-slate-500 break-all">
            {err ? "error · " : ""}
            {truncate(String(p.content ?? "").replace(/\s+/g, " "), 140)}
          </div>
        </>
      );
    }

    case "message":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-sky-400">
            <MessageSquare size={13} />
          </div>
          <div className="flex-1 min-w-0 text-sky-300/90 text-xs">
            {truncate(String(p.text ?? ""), 200)}
          </div>
        </>
      );

    case "plan":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-amber-300">
            <FileText size={13} />
          </div>
          <div className="flex-1 min-w-0 text-amber-300/80 text-xs">
            plan updated ({Object.keys(p).length} fields)
          </div>
        </>
      );

    case "note":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-slate-500">
            <FileText size={13} />
          </div>
          <div className="flex-1 min-w-0 text-xs text-slate-500">
            {String((p.kind as string | undefined) ?? "note")}
          </div>
        </>
      );

    case "done": {
      const status = String(p.status ?? "done");
      const ok = status === "completed";
      return (
        <>
          <div className={`shrink-0 mt-0.5 ${ok ? "text-emerald-400" : "text-red-400"}`}>
            <CheckCircle2 size={14} />
          </div>
          <div
            className={`flex-1 font-semibold uppercase tracking-wider text-xs ${
              ok ? "text-emerald-400" : "text-red-400"
            }`}
          >
            {status}
            {p.error ? ` · ${String(p.error)}` : ""}
          </div>
        </>
      );
    }
  }
}

function toolIcon(title: string): [JSX.Element, string] {
  const t = title.toLowerCase();
  if (t.includes("delegate")) return [<Users size={14} />, "text-cyan-400"];
  if (t.includes("write") || t.includes("report")) return [<Save size={14} />, "text-emerald-400"];
  if (t.includes("terminal") || t.includes("bash") || t.includes("exec"))
    return [<Terminal size={14} />, "text-slate-400"];
  if (t.includes("extract") || t.includes("fetch") || t.includes("read"))
    return [<FileText size={14} />, "text-sky-400"];
  if (t.includes("search") || t.includes("query"))
    return [<Search size={14} />, "text-accent-400"];
  return [<Terminal size={14} />, "text-slate-400"];
}

function shortTitle(title: string): string {
  const m = title.match(/^([a-z0-9_-]+)/i);
  return (m?.[1] ?? title).slice(0, 14);
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n - 1) + "…";
}
