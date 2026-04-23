import { LogEvent, parseHermesLog } from "../lib/parseHermesLog";
import {
  Search,
  FileText,
  AlertTriangle,
  Clock,
  CheckCircle2,
  Terminal,
  Save,
  Cpu,
  Container as ContainerIcon,
  Info,
  Users,
} from "lucide-react";

interface Props {
  rawLog: string;
}

/**
 * Cleans up Hermes stdout into a vertical event stream.
 * Shows what the single agent inside the Hermes container is doing:
 * searches, extracts, report writes, LLM retries.
 */
export default function LogTimeline({ rawLog }: Props) {
  const events = parseHermesLog(rawLog);

  if (events.length === 0) {
    return (
      <div className="text-slate-500 text-sm py-2">
        No activity yet. The agent is booting…
      </div>
    );
  }

  const toolCalls = events.filter((e) =>
    ["search", "extract", "write_report", "other_tool"].includes(e.kind)
  ).length;
  const batches = events.filter((e) => e.kind === "delegate_batch_end").length;
  const subAgentsTotal = events
    .filter((e) => e.kind === "delegate_batch_end")
    // TS narrow
    .reduce((n, e) => n + (e.kind === "delegate_batch_end" ? e.total : 0), 0);
  const subAgentsOk = events.filter(
    (e) => e.kind === "subagent_done" && !e.error
  ).length;
  const subAgentsFail = events.filter(
    (e) => e.kind === "subagent_done" && e.error
  ).length;

  return (
    <div className="space-y-2">
      {(toolCalls > 0 || subAgentsTotal > 0) && (
        <div className="flex items-start gap-2 px-2.5 py-1.5 rounded-md bg-base-800/60 border border-base-700 text-xs text-slate-400">
          <Info size={12} className="shrink-0 mt-0.5 text-slate-500" />
          <div className="leading-relaxed">
            {subAgentsTotal > 0 ? (
              <>
                <span className="text-cyan-400 font-semibold">
                  {batches} delegate_task batch{batches > 1 ? "es" : ""}
                </span>
                {", "}
                <span className="text-cyan-300">{subAgentsTotal}</span> sub-agents
                total
                {subAgentsOk > 0 && <> · <span className="text-emerald-400">{subAgentsOk} ok</span></>}
                {subAgentsFail > 0 && <> · <span className="text-red-400">{subAgentsFail} failed</span></>}
                {toolCalls > 0 && <> · lead did <span className="text-slate-300">{toolCalls}</span> own tool calls</>}
              </>
            ) : (
              <>
                Lead agent running{" "}
                <span className="text-slate-300 font-semibold">{toolCalls}</span> tool
                calls sequentially. No <code className="font-mono">delegate_task</code>{" "}
                fired yet.
              </>
            )}
          </div>
        </div>
      )}

      <ol className="space-y-1.5 text-sm">
        {events.map((ev, i) => {
          // Visual separator before first agent tool call after orchestrator events
          const prev = i > 0 ? events[i - 1] : null;
          const prevIsOrch =
            prev &&
            (prev.kind === "orch_spawn" ||
              prev.kind === "agent_init" ||
              prev.kind === "orch_collect");
          const curIsAgentWork =
            ["search", "extract", "write_report", "other_tool"].includes(ev.kind);
          const showSeparator = prev && prevIsOrch && curIsAgentWork;
          return (
            <div key={i}>
              {showSeparator && (
                <div className="my-2 text-xs text-slate-600 uppercase tracking-widest flex items-center gap-2">
                  <span className="h-px flex-1 bg-base-700" />
                  <span>agent turns</span>
                  <span className="h-px flex-1 bg-base-700" />
                </div>
              )}
              <li className="flex items-start gap-2.5">
                {renderEvent(ev)}
              </li>
            </div>
          );
        })}
      </ol>
    </div>
  );
}

function renderEvent(ev: LogEvent) {
  switch (ev.kind) {
    case "orch_spawn":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-fuchsia-400">
            <ContainerIcon size={14} />
          </div>
          <div className="flex-1 min-w-0">
            <span className="text-fuchsia-400 font-semibold uppercase text-xs mr-2 tracking-wider">
              orchestrator
            </span>
            <span className="text-slate-300">{ev.text}</span>
          </div>
        </>
      );
    case "agent_init":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-amber-300">
            <Cpu size={14} />
          </div>
          <div className="flex-1 min-w-0">
            <span className="text-amber-300 font-semibold uppercase text-xs mr-2 tracking-wider">
              agent
            </span>
            <span className="text-slate-300">{ev.text}</span>
          </div>
        </>
      );
    case "orch_collect":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-fuchsia-400">
            <ContainerIcon size={14} />
          </div>
          <div className="flex-1 min-w-0">
            <span className="text-fuchsia-400 font-semibold uppercase text-xs mr-2 tracking-wider">
              orchestrator
            </span>
            <span className="text-slate-300">{ev.text}</span>
          </div>
          <div className="shrink-0 flex items-center gap-1 text-xs">
            <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.6)]" />
            <span className="text-emerald-400 font-semibold uppercase tracking-wider">done</span>
          </div>
        </>
      );
    case "delegate_batch_start":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-cyan-400">
            <Users size={14} />
          </div>
          <div className="flex-1 min-w-0 text-cyan-400 font-semibold uppercase text-xs tracking-wider">
            delegate_task — dispatching parallel sub-agents…
          </div>
        </>
      );
    case "subagent_done":
      return (
        <>
          <div className={`shrink-0 mt-0.5 ml-4 ${ev.error ? "text-red-400" : "text-cyan-300"}`}>
            <Users size={12} />
          </div>
          <div className="flex-1 min-w-0">
            <span className={`${ev.error ? "text-red-400" : "text-cyan-300"} font-semibold uppercase text-xs mr-2 tracking-wider`}>
              sub-agent [{ev.index}/{ev.total}]
            </span>
            <span className="text-slate-200">{ev.goal}</span>
          </div>
          {renderDuration(ev.duration, ev.error)}
        </>
      );
    case "delegate_batch_end":
      return (
        <>
          <div className={`shrink-0 mt-0.5 ${ev.error ? "text-amber-400" : "text-cyan-400"}`}>
            <CheckCircle2 size={14} />
          </div>
          <div className="flex-1 min-w-0 text-cyan-300/80 text-xs">
            batch returned · <span className="text-slate-300">{ev.total}</span> sub-agents
            {ev.error && <span className="text-amber-400"> · with errors</span>}
          </div>
          {renderDuration(ev.duration)}
        </>
      );
    case "subagent_warn":
      return (
        <>
          <div className="shrink-0 mt-0.5 ml-4 text-amber-400">
            <AlertTriangle size={11} />
          </div>
          <div className="flex-1 min-w-0 text-amber-300/70 text-xs">
            <span className="font-mono text-xs text-slate-500 mr-1.5">{ev.subagent}</span>
            {ev.text}
          </div>
        </>
      );
    case "search":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-accent-400">
            <Search size={14} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-slate-200">
              <span className="text-accent-400 font-semibold uppercase text-xs mr-2">
                search
              </span>
              <span className="font-mono text-slate-300">"{ev.query}"</span>
              {ev.maxResults && (
                <span className="text-slate-500 ml-2 text-xs">
                  · max {ev.maxResults}
                </span>
              )}
            </div>
          </div>
          {renderDuration(ev.duration, ev.error)}
        </>
      );
    case "extract":
      return (
        <>
          <div className={`shrink-0 mt-0.5 ${ev.error ? "text-red-400" : "text-sky-400"}`}>
            <FileText size={14} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-slate-200">
              <span
                className={`${
                  ev.error ? "text-red-400" : "text-sky-400"
                } font-semibold uppercase text-xs mr-2`}
              >
                extract
                {ev.size ? ` ${ev.size.toUpperCase()}` : ""}
              </span>
              <span className="font-mono text-slate-300 break-all">{ev.url}</span>
            </div>
          </div>
          {renderDuration(ev.duration, ev.error)}
        </>
      );
    case "write_report":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-emerald-400">
            <Save size={14} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-slate-200">
              <span className="text-emerald-400 font-semibold uppercase text-xs mr-2">
                write
              </span>
              <span className="font-mono text-slate-300">/workspace/report.md</span>
            </div>
          </div>
          {renderDuration(ev.duration)}
        </>
      );
    case "other_tool":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-slate-500">
            <Terminal size={14} />
          </div>
          <div className="flex-1 min-w-0 text-slate-400 font-mono text-xs break-all">
            <span className="uppercase text-xs font-semibold mr-2">$</span>
            {ev.cmd}
          </div>
          {renderDuration(ev.duration, ev.error)}
        </>
      );
    case "api_retry":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-amber-400">
            <AlertTriangle size={14} />
          </div>
          <div className="flex-1 text-amber-300/90 text-sm">
            LLM error{" "}
            <span className="text-slate-500">
              (attempt {ev.attempt}/{ev.of})
            </span>
            <span className="font-mono text-slate-500 ml-2 text-xs">{ev.error}</span>
          </div>
        </>
      );
    case "waiting_retry":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-slate-500">
            <Clock size={14} />
          </div>
          <div className="flex-1 text-slate-400 text-xs">
            retrying in {ev.seconds}s…
          </div>
        </>
      );
    case "report_saved":
      return (
        <>
          <div className="shrink-0 mt-0.5 text-emerald-400">
            <CheckCircle2 size={14} />
          </div>
          <div className="flex-1 text-emerald-400 font-semibold">
            REPORT_SAVED · handing off to orchestrator
          </div>
        </>
      );
    case "note":
      return <div className="text-slate-400">{ev.text}</div>;
  }
}

function renderDuration(duration: string | undefined, error?: boolean) {
  if (!duration && !error) {
    // Even with no duration we want to show a status pill.
    return (
      <div className="shrink-0 flex items-center gap-1.5 text-xs">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.6)]" />
        <span className="text-emerald-400 font-semibold uppercase tracking-wider">ok</span>
      </div>
    );
  }
  if (error) {
    return (
      <div className="shrink-0 flex items-center gap-2 text-xs font-mono">
        <span className="text-slate-500">{duration}</span>
        <span className="flex items-center gap-1 text-red-400">
          <span className="w-1.5 h-1.5 rounded-full bg-red-500 shadow-[0_0_6px_rgba(239,68,68,0.6)]" />
          <span className="font-semibold uppercase tracking-wider">fail</span>
        </span>
      </div>
    );
  }
  return (
    <div className="shrink-0 flex items-center gap-2 text-xs font-mono">
      <span className="text-slate-500">{duration}</span>
      <span className="flex items-center gap-1 text-emerald-400">
        <span className="w-1.5 h-1.5 rounded-full bg-emerald-500 shadow-[0_0_6px_rgba(16,185,129,0.6)]" />
        <span className="font-semibold uppercase tracking-wider">ok</span>
      </span>
    </div>
  );
}
