import { useEffect, useState } from "react";
import { ChevronDown, ChevronUp, Terminal } from "lucide-react";
import { getLogs } from "../lib/api";

interface Props {
  jobId: string | null;
  isRunning: boolean;
}

/**
 * Collapsible drawer showing the hermes.log for the current job.
 * Polls every 3s while the job is running.
 */
export default function DebugDrawer({ jobId, isRunning }: Props) {
  const [open, setOpen] = useState(false);
  const [logs, setLogs] = useState<string>("");

  useEffect(() => {
    if (!jobId) {
      setLogs("");
      return;
    }

    let cancelled = false;
    let handle: number | null = null;

    const tick = async () => {
      try {
        const l = await getLogs(jobId);
        if (cancelled) return;
        if (l !== null) {
          setLogs(l);
        } else {
          // 404 — job is gone (e.g. adapter restart). Stop polling and clear.
          setLogs("");
          if (handle !== null) {
            clearInterval(handle);
            handle = null;
          }
        }
      } catch (e) {
        console.debug("log fetch error", e);
      }
    };

    tick(); // immediate first poll

    if (isRunning) {
      handle = window.setInterval(tick, 3000);
    }

    return () => {
      cancelled = true;
      if (handle !== null) clearInterval(handle);
    };
  }, [jobId, isRunning]);

  return (
    <div className="fixed bottom-0 left-0 right-0 z-20">
      <div className="max-w-4xl mx-auto px-4 pb-4">
        <div className="rounded-t-xl border border-base-700 bg-base-900 border-b-0 shadow-2xl">
          <button
            onClick={() => setOpen(!open)}
            className="w-full px-4 py-2.5 flex items-center justify-between
                       text-slate-400 hover:text-slate-200 transition-colors"
          >
            <div className="flex items-center gap-2 text-sm">
              <Terminal size={14} />
              <span className="font-semibold uppercase tracking-wider text-xs">
                Debug
              </span>
              {jobId && (
                <span className="text-slate-600 font-mono text-xs">
                  · {logs.length > 0 ? `${logs.length} chars` : "waiting"}
                </span>
              )}
            </div>
            {open ? <ChevronDown size={14} /> : <ChevronUp size={14} />}
          </button>
          {open && (
            <div className="border-t border-base-700 max-h-64 overflow-y-auto p-4">
              {!jobId ? (
                <div className="text-slate-500 text-sm">No job running.</div>
              ) : !logs ? (
                <div className="text-slate-500 text-sm">
                  No logs yet. The container hasn't produced any output.
                </div>
              ) : (
                <pre className="log-pane text-xs text-slate-300 font-mono">
                  {logs}
                </pre>
              )}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
