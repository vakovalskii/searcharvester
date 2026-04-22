import { Clock, FileText, Loader2, Square, XCircle, CheckCircle2, AlertTriangle } from "lucide-react";
import { EventPayload, JobStatus, Phase } from "../lib/api";
import PhaseTimeline from "./PhaseTimeline";

interface Props {
  query: string;
  jobId: string;
  latest: EventPayload | null;
  finalStatus: JobStatus | null;
  onCancel: () => void;
}

function formatElapsed(sec: number | null | undefined): string {
  if (sec == null) return "–";
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  return `${m}m ${s}s`;
}

function formatBytes(n: number): string {
  if (n < 1024) return `${n}b`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)}kb`;
  return `${(n / 1024 / 1024).toFixed(2)}mb`;
}

export default function JobStatusCard({
  query,
  jobId,
  latest,
  finalStatus,
  onCancel,
}: Props) {
  const status = latest?.status ?? "queued";
  const phase: Phase = latest?.phase ?? "queued";
  const elapsed = latest?.elapsed_sec ?? null;
  const artifacts = latest?.artifacts ?? {};
  const hasArtifacts = Object.keys(artifacts).length > 0;

  const isRunning = status === "running" || status === "queued";
  const isDone = finalStatus === "completed";
  const isErrored =
    finalStatus === "failed" || finalStatus === "timeout" || finalStatus === "cancelled";

  return (
    <div className="rounded-xl border border-base-700 bg-base-800/60 overflow-hidden">
      <div className="px-5 py-4 border-b border-base-700 flex items-start justify-between gap-4">
        <div className="flex-1 min-w-0">
          <div className="text-xs text-slate-500 uppercase tracking-wider mb-1">Research query</div>
          <div className="text-slate-100 font-medium line-clamp-2">{query}</div>
          <div className="text-xs text-slate-500 mt-1 font-mono">job: {jobId}</div>
        </div>
        <div className="flex items-center gap-3 shrink-0">
          <div className="flex items-center gap-1.5 text-slate-400 text-sm">
            <Clock size={14} />
            <span className="font-mono">{formatElapsed(elapsed)}</span>
          </div>
          {isRunning && (
            <button
              onClick={onCancel}
              className="flex items-center gap-1.5 rounded-md bg-red-500/10 hover:bg-red-500/20
                         border border-red-500/30 text-red-400 hover:text-red-300
                         px-3 py-1.5 text-sm font-medium transition-colors"
              aria-label="Stop"
              title="Stop and kill the running container"
            >
              <Square size={12} className="fill-current" />
              <span>Stop</span>
            </button>
          )}
        </div>
      </div>

      <div className="px-5 py-4">
        <PhaseTimeline
          currentPhase={isRunning ? phase : null}
          finalStatus={
            isDone
              ? "completed"
              : isErrored
              ? (finalStatus as "failed" | "timeout" | "cancelled")
              : null
          }
        />
      </div>

      {(hasArtifacts || isRunning) && (
        <div className="px-5 py-3 border-t border-base-700 flex items-center gap-4 text-xs text-slate-400">
          {isRunning && (
            <div className="flex items-center gap-1.5">
              <Loader2 size={12} className="animate-spin" />
              <span>agent working</span>
            </div>
          )}
          {hasArtifacts && (
            <div className="flex items-center gap-3 flex-wrap">
              {Object.entries(artifacts).map(([name, size]) => (
                <div key={name} className="flex items-center gap-1 font-mono">
                  <FileText size={11} />
                  <span>{name}</span>
                  <span className="text-slate-600">{formatBytes(size)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {isErrored && latest?.error && (
        <div className="px-5 py-3 border-t border-red-900/30 bg-red-950/20 text-red-400 text-sm flex items-start gap-2">
          {finalStatus === "cancelled" ? (
            <XCircle size={16} className="shrink-0 mt-0.5" />
          ) : finalStatus === "timeout" ? (
            <AlertTriangle size={16} className="shrink-0 mt-0.5" />
          ) : (
            <AlertTriangle size={16} className="shrink-0 mt-0.5" />
          )}
          <div>
            <div className="font-semibold capitalize">{finalStatus}</div>
            <div className="text-red-300/80 font-mono text-xs mt-0.5 break-all">
              {latest.error}
            </div>
          </div>
        </div>
      )}

      {isDone && latest?.duration_sec != null && (
        <div className="px-5 py-3 border-t border-emerald-900/30 bg-emerald-950/20 text-emerald-400 text-sm flex items-center gap-2">
          <CheckCircle2 size={16} />
          <span>
            Completed in <span className="font-mono">{latest.duration_sec.toFixed(1)}s</span>
          </span>
        </div>
      )}
    </div>
  );
}
