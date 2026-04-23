import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, Github } from "lucide-react";
import ResearchForm from "./components/ResearchForm";
import JobStatusCard from "./components/JobStatusCard";
import ReportView from "./components/ReportView";
import DebugDrawer from "./components/DebugDrawer";
import {
  API_URL,
  AgentEvent,
  JobStatus,
  JobTerminalStatus,
  SSESubscription,
  cancelJob,
  checkHealth,
  createResearch,
  getJob,
  getSnapshot,
  subscribeToJob,
} from "./lib/api";

interface ActiveJob {
  jobId: string;
  query: string;
}

function useHealth() {
  const [healthy, setHealthy] = useState<"ok" | "degraded" | "down">("down");
  useEffect(() => {
    const tick = async () => {
      try {
        const h = await checkHealth();
        setHealthy(h.orchestrator === "available" ? "ok" : "degraded");
      } catch {
        setHealthy("down");
      }
    };
    tick();
    const id = window.setInterval(tick, 15000);
    return () => clearInterval(id);
  }, []);
  return healthy;
}

/** Restore / persist current job_id via URL hash so reloading keeps it alive. */
function useHashJob(): [
  ActiveJob | null,
  (j: ActiveJob | null) => void
] {
  const [job, setJob] = useState<ActiveJob | null>(null);

  useEffect(() => {
    const parseHash = () => {
      const raw = window.location.hash.replace(/^#/, "");
      const params = new URLSearchParams(raw);
      const id = params.get("job");
      const q = params.get("q");
      if (id && q) setJob({ jobId: id, query: q });
      else setJob(null);
    };
    parseHash();
    window.addEventListener("hashchange", parseHash);
    return () => window.removeEventListener("hashchange", parseHash);
  }, []);

  const writeJob = useCallback((j: ActiveJob | null) => {
    if (j) {
      const p = new URLSearchParams({ job: j.jobId, q: j.query });
      window.location.hash = p.toString();
    } else {
      history.replaceState(null, "", window.location.pathname);
    }
    setJob(j);
  }, []);

  return [job, writeJob];
}

export default function App() {
  const healthy = useHealth();
  const [job, setJob] = useHashJob();
  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [finalStatus, setFinalStatus] = useState<JobTerminalStatus | null>(null);
  const [report, setReport] = useState<string | null>(null);
  const [jobSnapshot, setJobSnapshot] = useState<{
    status: JobStatus;
    error: string | null;
    duration_sec: number | null;
  } | null>(null);
  const subRef = useRef<SSESubscription | null>(null);

  // (Re-)subscribe when the active job changes (includes hash restore).
  useEffect(() => {
    subRef.current?.close();
    subRef.current = null;
    setEvents([]);
    setFinalStatus(null);
    setReport(null);
    setJobSnapshot(null);

    if (!job) return;

    let aborted = false;
    (async () => {
      const snapshot = await getJob(job.jobId).catch(() => null);
      if (aborted) return;
      if (snapshot === null) {
        // Job unknown to the server — drop from URL (adapter likely restarted).
        setJob(null);
        return;
      }

      setJobSnapshot({
        status: snapshot.status,
        error: snapshot.error,
        duration_sec: snapshot.duration_sec,
      });
      setReport(snapshot.report);

      const terminal = ["completed", "failed", "timeout", "cancelled"];
      if (terminal.includes(snapshot.status)) {
        // Terminal job loaded via URL — still show the activity timeline
        // by fetching the recorded event log one-shot.
        const snap = await getSnapshot(job.jobId).catch(() => null);
        if (!aborted && snap) setEvents(snap.events);
        setFinalStatus({
          job_id: snapshot.job_id,
          status: snapshot.status,
          duration_sec: snapshot.duration_sec,
          has_report: snapshot.report !== null,
          error: snapshot.error,
        });
        return;
      }

      const sub = subscribeToJob(
        job.jobId,
        (ev) => {
          setEvents((prev) => [...prev, ev]);
          if (ev.type === "done") {
            const status = (ev.payload.status as JobStatus | undefined) ?? null;
            if (status) {
              setJobSnapshot((s) =>
                s ? { ...s, status } : { status, error: null, duration_sec: null }
              );
            }
          }
        },
        async (final) => {
          setFinalStatus(final);
          // Drain any events the SSE may have missed (backfilled sub-agent
          // messages that got appended between our last yield and the status
          // frame). Snapshot API sees everything the orchestrator recorded.
          const snap = await getSnapshot(job.jobId).catch(() => null);
          if (snap) setEvents(snap.events);
          if (final.has_report) {
            const jobSnap = await getJob(job.jobId).catch(() => null);
            if (jobSnap?.report) setReport(jobSnap.report);
          }
        },
        async () => {
          const check = await getJob(job.jobId).catch(() => null);
          if (check === null) setJob(null);
        }
      );
      subRef.current = sub;
    })();

    return () => {
      aborted = true;
      subRef.current?.close();
      subRef.current = null;
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.jobId]);

  const onSubmit = async (query: string) => {
    try {
      const res = await createResearch(query);
      setJob({ jobId: res.job_id, query });
    } catch (e) {
      alert(`Failed to start research: ${(e as Error).message}`);
    }
  };

  const onCancel = async () => {
    if (!job) return;
    await cancelJob(job.jobId);
  };

  const onRunAgain = () => setJob(null);

  const isRunning = job !== null && finalStatus === null;

  return (
    <div className="min-h-full pb-12">
      {/* Header */}
      <header className="border-b border-base-800">
        <div className="max-w-4xl mx-auto px-4 py-4 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="text-2xl">🌾</div>
            <div>
              <h1 className="text-lg font-semibold text-slate-100">Searcharvester</h1>
              <div className="text-xs text-slate-500">Self-hosted deep research</div>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div
              className={`flex items-center gap-1.5 text-xs ${
                healthy === "ok"
                  ? "text-emerald-400"
                  : healthy === "degraded"
                  ? "text-amber-400"
                  : "text-red-400"
              }`}
              title={`API: ${API_URL}`}
            >
              <Activity size={12} className={healthy === "ok" ? "animate-pulse" : ""} />
              {healthy === "ok" ? "API connected" : healthy === "degraded" ? "orchestrator offline" : "API down"}
            </div>
            <a
              href="https://github.com/vakovalskii/searcharvester"
              target="_blank"
              rel="noreferrer"
              className="text-slate-500 hover:text-slate-300 transition-colors"
              aria-label="GitHub"
            >
              <Github size={16} />
            </a>
          </div>
        </div>
      </header>

      {/* Main */}
      <main className="max-w-4xl mx-auto px-4 pt-8 space-y-5">
        {!job && <ResearchForm onSubmit={onSubmit} disabled={healthy === "down"} />}

        {job && (
          <JobStatusCard
            query={job.query}
            jobId={job.jobId}
            events={events}
            finalStatus={finalStatus}
            jobSnapshot={jobSnapshot}
            onCancel={onCancel}
          />
        )}

        {job && (
          <DebugDrawer
            jobId={job.jobId}
            events={events}
            isRunning={isRunning}
          />
        )}

        {report && <ReportView report={report} onRunAgain={onRunAgain} />}

        {job && finalStatus && !report && (
          <div className="text-center">
            <button
              onClick={onRunAgain}
              className="text-slate-400 hover:text-slate-100 underline text-sm"
            >
              Start a new research
            </button>
          </div>
        )}
      </main>
    </div>
  );
}
