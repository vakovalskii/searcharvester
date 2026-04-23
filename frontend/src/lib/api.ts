/** HTTP + SSE client for the Searcharvester API. */

export const API_URL =
  (import.meta as unknown as { env: Record<string, string> }).env.VITE_API_URL ||
  "http://localhost:8000";

// --------- Types mirroring the FastAPI models ---------

export type JobStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "timeout"
  | "cancelled";

export interface ResearchCreated {
  job_id: string;
  status: JobStatus;
}

export interface JobSnapshot {
  job_id: string;
  status: JobStatus;
  query: string;
  started_at: string | null;
  finished_at: string | null;
  duration_sec: number | null;
  report: string | null;
  error: string | null;
}

// --------- Agent events (mirror of events.py / Event dataclass) ---------

export type AgentEventType =
  | "spawn"
  | "thought"
  | "message"
  | "tool_call"
  | "tool_result"
  | "plan"
  | "commands"
  | "note"
  | "done";

export interface AgentEvent {
  ts: string;
  job_id: string;
  agent_id: string;
  parent_id: string | null;
  type: AgentEventType;
  payload: Record<string, unknown>;
}

export interface JobTerminalStatus {
  job_id: string;
  status: JobStatus;
  duration_sec: number | null;
  has_report: boolean;
  error: string | null;
}

// --------- Calls ---------

export async function createResearch(query: string): Promise<ResearchCreated> {
  const r = await fetch(`${API_URL}/research`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ query }),
  });
  if (!r.ok) {
    throw new Error(`POST /research failed: ${r.status} ${await r.text()}`);
  }
  return r.json();
}

export async function getJob(jobId: string): Promise<JobSnapshot | null> {
  const r = await fetch(`${API_URL}/research/${jobId}`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`GET /research/${jobId}: ${r.status}`);
  return r.json();
}

export async function cancelJob(jobId: string): Promise<void> {
  await fetch(`${API_URL}/research/${jobId}`, { method: "DELETE" });
}

export interface JobSnapshotWithEvents {
  job_id: string;
  status: JobStatus;
  phase: string;
  artifacts: Record<string, number>;
  events: AgentEvent[];
}

/** One-shot fetch of every event the orchestrator has recorded for a job.
 *  Used on initial load of a terminal job (SSE isn't opened) and as a
 *  safety net after SSE closes (in case backfilled events weren't drained). */
export async function getSnapshot(
  jobId: string
): Promise<JobSnapshotWithEvents | null> {
  const r = await fetch(`${API_URL}/research/${jobId}/snapshot`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`GET /research/${jobId}/snapshot: ${r.status}`);
  return r.json();
}

export async function checkHealth(): Promise<{ status: string; orchestrator: string }> {
  const r = await fetch(`${API_URL}/health`);
  if (!r.ok) throw new Error(`GET /health: ${r.status}`);
  return r.json();
}

// --------- SSE subscription ---------

export interface SSESubscription {
  close: () => void;
}

const EVENT_TYPES: AgentEventType[] = [
  "spawn", "thought", "message",
  "tool_call", "tool_result",
  "plan", "commands", "note", "done",
];

/**
 * Subscribe to the SSE stream of a research job. Each typed event fires
 * `onEvent`; the final `status` frame (emitted once after `done`) fires
 * `onFinal`, after which we close the EventSource.
 */
export function subscribeToJob(
  jobId: string,
  onEvent: (e: AgentEvent) => void,
  onFinal: (s: JobTerminalStatus) => void,
  onError?: (e: Event) => void
): SSESubscription {
  const es = new EventSource(`${API_URL}/research/${jobId}/events`);

  for (const t of EVENT_TYPES) {
    es.addEventListener(t, (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data) as AgentEvent;
        onEvent(data);
      } catch (e) {
        console.error("SSE parse error", t, e);
      }
    });
  }

  es.addEventListener("status", (ev: MessageEvent) => {
    try {
      const data = JSON.parse(ev.data) as JobTerminalStatus;
      onFinal(data);
    } catch (e) {
      console.error("SSE parse error (status)", e);
    } finally {
      es.close();
    }
  });

  es.onerror = (e) => {
    if (onError) onError(e);
  };

  return { close: () => es.close() };
}
