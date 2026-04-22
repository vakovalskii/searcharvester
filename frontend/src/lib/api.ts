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

export type Phase =
  | "queued"
  | "planning"
  | "gather"
  | "synthesise"
  | "verify"
  | JobStatus;

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

export interface EventPayload {
  status: JobStatus;
  phase: Phase;
  elapsed_sec: number | null;
  artifacts: Record<string, number>;
  duration_sec?: number | null;
  report?: string | null;
  error?: string | null;
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

export async function getLogs(jobId: string): Promise<string | null> {
  const r = await fetch(`${API_URL}/research/${jobId}/logs`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`GET /research/${jobId}/logs: ${r.status}`);
  const body = (await r.json()) as { logs?: string };
  return body.logs ?? null;
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

export type SSEEventKind =
  | "status"
  | "completed"
  | "failed"
  | "timeout"
  | "cancelled";

/**
 * Subscribe to the SSE stream for a job. Each typed event (status/completed/
 * failed/...) triggers `onEvent` with the payload. When a terminal event
 * arrives, the backend closes; we also auto-close the EventSource here.
 */
export function subscribeToJob(
  jobId: string,
  onEvent: (kind: SSEEventKind, payload: EventPayload) => void,
  onError?: (e: Event) => void
): SSESubscription {
  const es = new EventSource(`${API_URL}/research/${jobId}/events`);

  const kinds: SSEEventKind[] = ["status", "completed", "failed", "timeout", "cancelled"];
  const terminal: SSEEventKind[] = ["completed", "failed", "timeout", "cancelled"];

  for (const k of kinds) {
    es.addEventListener(k, (ev: MessageEvent) => {
      try {
        const data = JSON.parse(ev.data) as EventPayload;
        onEvent(k, data);
        if (terminal.includes(k)) {
          es.close();
        }
      } catch (e) {
        console.error("SSE parse error", e);
      }
    });
  }

  es.onerror = (e) => {
    if (onError) onError(e);
  };

  return { close: () => es.close() };
}
