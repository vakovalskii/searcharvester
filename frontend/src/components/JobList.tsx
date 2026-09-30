import { Plus } from "lucide-react";
import type { JobListItem } from "../lib/api";

interface Props {
  jobs: JobListItem[];
  activeId: string | null;
  onOpen: (job: JobListItem) => void;
  onNew: () => void;
}

const STATUS_DOT: Record<string, string> = {
  running: "bg-sky-400 animate-pulse",
  queued: "bg-slate-400",
  completed: "bg-emerald-400",
  failed: "bg-red-400",
  timeout: "bg-amber-400",
  cancelled: "bg-slate-500",
  interrupted: "bg-amber-600",
};

function ago(iso: string | null): string {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "now";
  if (s < 3600) return `${Math.floor(s / 60)}m`;
  if (s < 86400) return `${Math.floor(s / 3600)}h`;
  return `${Math.floor(s / 86400)}d`;
}

/** Left column: jobs as a chat list, newest first. */
export default function JobList({ jobs, activeId, onOpen, onNew }: Props) {
  return (
    <aside className="flex flex-col h-full min-h-0">
      <button
        onClick={onNew}
        className="m-3 flex items-center justify-center gap-2 rounded-lg border border-base-600
                   hover:border-accent-500 hover:text-slate-100 text-slate-300 text-sm py-2 transition-colors"
      >
        <Plus size={14} /> New research
      </button>
      <div className="flex-1 overflow-y-auto px-2 pb-3 space-y-1" role="list">
        {jobs.length === 0 && <div className="text-xs text-slate-500 px-2">No jobs yet</div>}
        {jobs.map((j) => (
          <button
            key={j.id}
            role="listitem"
            onClick={() => onOpen(j)}
            className={`w-full text-left rounded-lg px-3 py-2 transition-colors group
              ${j.id === activeId ? "bg-base-700 text-slate-100" : "hover:bg-base-800 text-slate-300"}`}
            title={j.query}
          >
            <div className="flex items-center gap-2">
              <span className={`h-2 w-2 rounded-full shrink-0 ${STATUS_DOT[j.status] ?? "bg-slate-600"}`} />
              <span className="truncate text-sm">{j.query || j.id}</span>
            </div>
            <div className="flex gap-2 mt-0.5 ml-4 text-[11px] text-slate-500 font-mono">
              <span>{j.depth && j.depth !== "unknown" ? j.depth : "—"}</span>
              <span>{j.status}</span>
              {j.duration_sec != null && <span>{Math.round(j.duration_sec)}s</span>}
              <span className="ml-auto">{ago(j.created_at)}</span>
            </div>
          </button>
        ))}
      </div>
    </aside>
  );
}
