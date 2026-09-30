import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Activity, Github, Settings, Square, Timer } from "lucide-react";
import ResearchForm from "./components/ResearchForm";
import ReportView from "./components/ReportView";
import JobList from "./components/JobList";
import AgentGraph from "./components/AgentGraph";
import AgentChat from "./components/AgentChat";
import BudgetBars from "./components/BudgetBars";
import SourcesPanel from "./components/SourcesPanel";
import BranchView from "./components/BranchView";
import MediaPanel from "./components/MediaPanel";
import SettingsPage from "./components/SettingsPage";
import {
  API_URL,
  Depth,
  JobListItem,
  RoleModels,
  cancelJob,
  checkHealth,
  createResearch,
  getJob,
  getSnapshot,
  listJobs,
  subscribeToJob,
} from "./lib/api";
import { Store, addEvents, emptyRecord, lastSeq, orderedEvents, withJob } from "./lib/store";
import { agentFlows, agentReport, markReportSources, reduce } from "./lib/view";

const TERMINAL = new Set(["completed", "failed", "timeout", "cancelled", "interrupted"]);

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

function hashJob(): string | null {
  const id = new URLSearchParams(window.location.hash.replace(/^#/, "")).get("job");
  return id && /^[0-9a-f]{16}$/.test(id) ? id : null;
}

const isSettingsHash = () => window.location.hash === "#settings";

function useClock(running: boolean, startedAt: string | null, durationSec: number | null): string {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!running) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running]);
  const sec = durationSec ?? (startedAt ? Math.max(0, (now - new Date(startedAt).getTime()) / 1000) : 0);
  const m = Math.floor(sec / 60), s = Math.floor(sec % 60);
  return m ? `${m}m ${s}s` : `${s}s`;
}

export default function App() {
  const healthy = useHealth();
  const [jobs, setJobs] = useState<JobListItem[]>([]);
  const [store, setStore] = useState<Store>(new Map());
  const [activeId, setActiveId] = useState<string | null>(hashJob());
  const [settings, setSettings] = useState(isSettingsHash());
  const [selectedAgent, setSelectedAgent] = useState("lead");
  type Tab = "report" | "branches" | "graph" | "sources" | "media";
  const [tabChoice, setTab] = useState<Tab | null>(null); // null: pick by what the job has
  const [focus, setFocus] = useState<string | null>(null);
  const subs = useRef(new Map<string, { close: () => void }>());

  const refreshJobs = useCallback(async () => {
    try {
      setJobs(await listJobs(50));
    } catch {
      /* the list is a convenience; the open job keeps working */
    }
  }, []);

  useEffect(() => {
    refreshJobs();
    const id = window.setInterval(refreshJobs, 5000);
    return () => clearInterval(id);
  }, [refreshJobs]);

  useEffect(() => {
    const onHash = () => { setActiveId(hashJob()); setSettings(isSettingsHash()); };
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const open = useCallback((id: string | null) => {
    if (id) window.location.hash = `job=${id}`;
    else history.replaceState(null, "", window.location.pathname);
    setActiveId(id);
    setSettings(false);
    setSelectedAgent("lead");
    setFocus(null);
    setTab(null);
  }, []);

  // Load / follow the active job. Every async result is written into ITS job's
  // record (withJob), so switching jobs mid-request never mixes two jobs.
  useEffect(() => {
    if (!activeId) return;
    const jobId = activeId;
    let cancelled = false;
    (async () => {
      const st = await getJob(jobId).catch(() => null);
      if (cancelled) return;
      if (!st) {
        setStore((s) => withJob(s, jobId, (r) => ({ ...r, status: "interrupted", error: "job not found" })));
        return;
      }
      setStore((s) => withJob(s, jobId, (r) => ({ ...r, query: st.query, status: st.status, report: st.report,
                                                    error: st.error, durationSec: st.duration_sec })));
      const snap = await getSnapshot(jobId).catch(() => null);
      if (snap) setStore((s) => addEvents(s, jobId, snap.events));
      if (cancelled || TERMINAL.has(st.status) || subs.current.has(jobId)) return;
      const sub = subscribeToJob(
        jobId,
        (ev) => setStore((s) => addEvents(s, jobId, [ev])),
        async (final) => {
          subs.current.get(jobId)?.close();
          subs.current.delete(jobId);
          const done = await getJob(jobId).catch(() => null);
          const snap2 = await getSnapshot(jobId).catch(() => null);
          setStore((s) => {
            let n = withJob(s, jobId, (r) => ({ ...r, status: final.status, error: final.error,
                                                durationSec: final.duration_sec, report: done?.report ?? r.report }));
            if (snap2) n = addEvents(n, jobId, snap2.events);
            return n;
          });
          refreshJobs();
        },
        undefined,
        snap ? Math.max(0, ...snap.events.map((e) => e.seq || 0)) : 0,
      );
      subs.current.set(jobId, sub);
    })();
    return () => {
      cancelled = true;
    };
  }, [activeId, refreshJobs]);

  // Close streams of jobs that are no longer open.
  useEffect(() => {
    for (const [id, sub] of subs.current) {
      if (id !== activeId) {
        sub.close();
        subs.current.delete(id);
      }
    }
  }, [activeId]);
  useEffect(() => () => { for (const s of subs.current.values()) s.close(); }, []);

  const record = activeId ? store.get(activeId) : undefined;
  const events = useMemo(() => orderedEvents(record), [record]);
  const view = useMemo(() => markReportSources(reduce(events), record?.report ?? null), [events, record?.report]);
  const running = Boolean(activeId && record && record.status && !TERMINAL.has(record.status));
  const listed = jobs.find((j) => j.id === activeId);
  const focusAgent = focus ? view.agents.get(focus) : undefined;
  const focusReport = focus ? agentReport(view, focus) : null;
  const focusFlows = focus ? agentFlows(view, focus) : [];
  const onFocus = (id: string | null) => {
    setFocus(id);
    setSelectedAgent(id ?? "lead");
    setTab(null);
  };
  const tabs: Tab[] = focusAgent ? ["report", "graph", "sources"] : ["report", "branches", "graph", "sources", "media"];
  const tab: Tab = tabChoice && tabs.includes(tabChoice) ? tabChoice
    : focusAgent ? (focusReport ? "report" : "graph")
    : record?.report ? "report" : "branches";
  const focusSources = focusAgent ? [...view.sources.values()].filter((x) => x.readers.includes(focusAgent.id)).length : 0;
  const tabName = (t: Tab) => t === "report" ? (focusAgent ? "Findings" : "Report")
    : t === "branches" ? `Branches${view.rounds.length ? ` (${view.rounds.length})` : ""}`
    : t === "graph" ? "Graph" : t === "media" ? "Media" : `Sources (${focusAgent ? focusSources : view.sources.size})`;
  const clock = useClock(running, listed?.started_at ?? null, running ? null : record?.durationSec ?? null);

  const onSubmit = async (query: string, depth: Depth, models?: Partial<RoleModels>) => {
    try {
      const res = await createResearch(query, depth, models);
      setStore((s) => withJob(s, res.job_id, () => ({ ...emptyRecord(res.job_id, query), status: "queued" })));
      open(res.job_id);
      refreshJobs();
    } catch (e) {
      alert(`Failed to start research: ${(e as Error).message}`);
    }
  };

  const onCancel = async () => {
    if (activeId) await cancelJob(activeId);
    refreshJobs();
  };

  return (
    <div className="h-full flex flex-col">
      <header className="border-b border-base-800 shrink-0">
        <div className="px-4 py-2.5 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <div className="text-xl">🌾</div>
            <div>
              <h1 className="text-base font-semibold text-slate-100 leading-tight">Searcharvester</h1>
              <div className="text-[11px] text-slate-500">Self-hosted research agents</div>
            </div>
          </div>
          <div className="flex items-center gap-4">
            <div className={`flex items-center gap-1.5 text-xs ${healthy === "ok" ? "text-emerald-400"
              : healthy === "degraded" ? "text-amber-400" : "text-red-400"}`} title={`API: ${API_URL}`}>
              <Activity size={12} className={healthy === "ok" ? "animate-pulse" : ""} />
              {healthy === "ok" ? "API connected" : healthy === "degraded" ? "orchestrator offline" : "API down"}
            </div>
            <a href="#settings" className={`${settings ? "text-slate-100" : "text-slate-500"} hover:text-slate-300`}
               aria-label="Search settings" title="Search settings"><Settings size={16} /></a>
            <a href="https://github.com/vakovalskii/searcharvester" target="_blank" rel="noreferrer"
               className="text-slate-500 hover:text-slate-300" aria-label="GitHub"><Github size={16} /></a>
          </div>
        </div>
      </header>

      <div className="flex-1 min-h-0 grid grid-cols-1 lg:grid-cols-[260px_minmax(0,1fr)_420px]">
        <div className="hidden lg:block border-r border-base-800 min-h-0">
          <JobList jobs={jobs} activeId={activeId} onOpen={(j) => open(j.id)} onNew={() => open(null)} />
        </div>

        {settings && (
          <main className="min-h-0 overflow-y-auto p-4 lg:col-span-2">
            <SettingsPage />
          </main>
        )}
        <main className={`min-h-0 overflow-y-auto p-4 space-y-4 ${settings ? "hidden" : ""}`}>
          {!activeId && (
            <div className="max-w-2xl mx-auto pt-10">
              <ResearchForm onSubmit={onSubmit} disabled={healthy === "down"} />
            </div>
          )}

          {activeId && (
            <>
              <div className="flex items-start gap-3">
                <div className="flex-1 min-w-0">
                  <div className="text-[11px] uppercase tracking-wide text-slate-500">
                    {view.depth || listed?.depth || ""} research · <span className="font-mono">{activeId}</span>
                  </div>
                  <div className="text-lg text-slate-100 break-words">{view.query || record?.query || listed?.query}</div>
                </div>
                <div className="flex items-center gap-1.5 text-sm text-slate-400 font-mono shrink-0">
                  <Timer size={14} /> {clock}
                </div>
                <span className={`text-xs px-2 py-1 rounded font-mono shrink-0 ${
                  record?.status === "completed" ? "bg-emerald-500/15 text-emerald-300"
                  : running ? "bg-sky-500/15 text-sky-300" : "bg-red-500/15 text-red-300"}`}>
                  {record?.status ?? "…"}
                </span>
                {running && (
                  <button onClick={onCancel} className="flex items-center gap-1 text-xs px-2 py-1 rounded border border-red-500/40
                                                        text-red-300 hover:bg-red-500/10 shrink-0">
                    <Square size={12} /> Stop
                  </button>
                )}
              </div>

              {view.guard.stoppedBy && (
                <div className="text-sm rounded-lg border border-red-500/40 bg-red-500/10 text-red-200 px-3 py-2">
                  Stopped by the loop guard: {view.guard.stoppedBy}
                </div>
              )}
              {record?.error && !running && (
                <div className="text-sm rounded-lg border border-base-700 text-slate-400 px-3 py-2">{record.error}</div>
              )}

              <nav className="flex items-center gap-1.5 text-xs text-slate-500" aria-label="breadcrumb">
                <button onClick={() => onFocus(null)} className={focusAgent ? "hover:text-slate-200" : "text-slate-200"}>
                  whole job
                </button>
                {focusAgent && (
                  <>
                    <span>›</span>
                    <span className="text-slate-200">{focusAgent.role} {view.order.indexOf(focusAgent.id)}</span>
                    <span className="text-slate-600 truncate">· {focusAgent.goal.slice(0, 120)}</span>
                  </>
                )}
                {!focusAgent && view.order.length > 1 && <span className="text-slate-600">· click a sub-agent to open its research</span>}
              </nav>

              {!focusAgent && <BudgetBars guard={view.guard} />}
              {focusAgent && (
                <div className="flex flex-wrap gap-3 text-xs font-mono text-slate-400">
                  <span>{focusAgent.toolCalls} tool calls</span>
                  <span>{focusFlows.filter((f) => f.kind === "query").length} searches</span>
                  <span>{focusFlows.filter((f) => f.kind === "fetch").length} pages opened</span>
                  <span>{Math.round(focusAgent.tokensIn / 1000)}k in / {Math.round(focusAgent.tokensOut / 1000)}k out</span>
                  <span className="text-slate-500">{focusAgent.state}</span>
                </div>
              )}

              <div className="flex gap-4 border-b border-base-800 text-sm sticky top-0 bg-base-950/95 backdrop-blur z-10 pt-1">
                {tabs.map((t) => (
                  <button key={t} onClick={() => setTab(t)}
                          className={`pb-2 -mb-px border-b-2 ${tab === t ? "border-accent-500 text-slate-100" : "border-transparent text-slate-500 hover:text-slate-300"}`}>
                    {tabName(t)}
                  </button>
                ))}
              </div>

              {tab === "branches" && (
                <BranchView view={view} hasReport={Boolean(record?.report)} onOpenAgent={(id) => onFocus(id)}
                            onOpenLead={() => setSelectedAgent("lead")} onOpenReport={() => setTab("report")} />
              )}
              {tab === "graph" && (
                <div className="rounded-xl border border-base-800 bg-base-900/50 p-3">
                  <AgentGraph view={view} selected={selectedAgent} focus={focus} live={running}
                              onSelect={setSelectedAgent} onFocus={onFocus} />
                  <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[11px] text-slate-500">
                    <span><span className="inline-block w-2 h-2 rounded-full bg-emerald-400 mr-1" />page cited in the report</span>
                    <span><span className="inline-block w-2 h-2 rounded-full bg-slate-500 mr-1" />page read, not cited</span>
                    <span>dots running along edges: data moving right now (task, query, results, page, findings)</span>
                  </div>
                </div>
              )}
              {tab === "report" && focusAgent && (focusReport
                ? <ReportView report={focusReport} jobId={activeId} onRunAgain={() => open(null)} />
                : <div className="text-sm text-slate-500">
                    {["done", "failed", "stopped"].includes(focusAgent.state)
                      ? "This sub-agent ended without findings."
                      : "Still working. Its findings appear here when it finishes; follow its steps in the chat on the right."}
                  </div>)}
              {tab === "report" && !focusAgent && (record?.report
                ? <ReportView report={record.report} jobId={activeId} onRunAgain={() => open(null)} />
                : <div className="text-sm text-slate-500">{running ? "The report appears when the agents finish. Watch the branches meanwhile." : "No report."}</div>)}
              {tab === "media" && activeId && <MediaPanel jobId={activeId} live={running} report={record?.report ?? null} />}
              {tab === "sources" && <SourcesPanel view={focusAgent ? { ...view, sources: new Map(
                [...view.sources].filter(([, src]) => src.readers.includes(focusAgent.id))) } : view}
                                                   onSelectAgent={setSelectedAgent} />}
            </>
          )}
        </main>

        <div className={`border-t lg:border-t-0 lg:border-l border-base-800 min-h-[420px] lg:min-h-0 ${settings ? "hidden" : ""}`}>
          {activeId && lastSeq(record) > 0
            ? <AgentChat view={view} selected={selectedAgent} onSelect={setSelectedAgent} />
            : <div className="p-4 text-sm text-slate-500">Agent chats appear here once a job runs.</div>}
        </div>
      </div>
    </div>
  );
}
