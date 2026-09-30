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
  | "cancelled"
  | "interrupted";

export type Depth = "quick" | "deep";

/** Agent roles of a job; a quick job has only the lead. Mirrors roles.py. */
export type RoleKey = "lead" | "researcher" | "critic" | "fact_checker";
export const ROLE_KEYS: RoleKey[] = ["lead", "researcher", "critic", "fact_checker"];
export type Reasoning = "auto" | "off" | "low" | "medium" | "high";

export interface RoleChoice {
  model: string | null;
  reasoning: Reasoning;
}
export type RoleModels = Record<RoleKey, RoleChoice>;

/** A gateway model: `reasoning` true = it thinks, false = it never does
 *  (e.g. a -noreason alias), null = the gateway does not say. */
export interface ModelInfo {
  id: string;
  reasoning: boolean | null;
  context: number | null;
}

export interface ModelOptions {
  roles: RoleKey[];
  reasoning: Reasoning[];
  defaults: RoleModels;
  models: ModelInfo[];
  error: string | null;
}

export interface ResearchCreated {
  job_id: string;
  status: JobStatus;
}

export interface JobSnapshot {
  job_id: string;
  status: JobStatus;
  query: string;
  depth?: string | null;
  started_at: string | null;
  finished_at: string | null;
  duration_sec: number | null;
  report: string | null;
  error: string | null;
  models?: Partial<RoleModels> | null;
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
  | "usage"
  | "done";

export interface AgentEvent {
  ts: string;
  job_id: string;
  agent_id: string;
  parent_id: string | null;
  type: AgentEventType;
  payload: Record<string, unknown>;
  seq: number;
}

export interface JobListItem {
  id: string;
  query: string;
  depth: string | null;
  status: JobStatus;
  created_at: string | null;
  started_at: string | null;
  duration_sec: number | null;
  parent_job?: string | null;
  models?: Partial<RoleModels> | null;
}

export interface JobTerminalStatus {
  job_id: string;
  status: JobStatus;
  duration_sec: number | null;
  has_report: boolean;
  error: string | null;
}

// --------- Calls ---------

/** Every state-changing call carries it: the adapter refuses POST/DELETE without
 *  it, so a plain HTML form on a foreign site cannot start or cancel a job. */
export const CLIENT_HEADERS = { "X-Searcharvester-Client": "1" } as const;

export async function createResearch(
  query: string, depth: Depth = "quick", models?: Partial<RoleModels>,
): Promise<ResearchCreated> {
  const r = await fetch(`${API_URL}/research`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...CLIENT_HEADERS },
    body: JSON.stringify(models ? { query, depth, models } : { query, depth }),
  });
  if (!r.ok) {
    throw new Error(`POST /research failed: ${r.status} ${await r.text()}`);
  }
  return r.json();
}

/** An image or video search handed to a job (media.py ledger). */
export interface MediaItem {
  kind: "image" | "video";
  src: string;            // the image, or the video page
  thumb: string | null;
  page: string;           // where it lives
  title: string;
  duration?: string | null;
  size?: string | null;
  query?: string | null;
}

/** A picture of a job through the adapter: the browser never fetches foreign
 *  sites, and the adapter serves only what search gave this job. */
export function mediaUrl(jobId: string, src: string): string {
  return `${API_URL}/media?job=${encodeURIComponent(jobId)}&src=${encodeURIComponent(src)}`;
}

export async function getJobMedia(jobId: string): Promise<MediaItem[]> {
  const r = await fetch(`${API_URL}/research/${jobId}/media`);
  if (r.status === 404) return [];
  if (!r.ok) throw new Error(`GET /research/${jobId}/media: ${r.status}`);
  return (await r.json()).items;
}

/** Models the gateway serves and the default of every role, for the pickers. */
export async function getModelOptions(): Promise<ModelOptions> {
  const r = await fetch(`${API_URL}/research/models`);
  if (!r.ok) throw new Error(`GET /research/models: ${r.status}`);
  return r.json();
}

export async function getJob(jobId: string): Promise<JobSnapshot | null> {
  const r = await fetch(`${API_URL}/research/${jobId}`);
  if (r.status === 404) return null;
  if (!r.ok) throw new Error(`GET /research/${jobId}: ${r.status}`);
  return r.json();
}

export async function cancelJob(jobId: string): Promise<void> {
  await fetch(`${API_URL}/research/${jobId}`, { method: "DELETE", headers: CLIENT_HEADERS });
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

export async function listJobs(limit = 50): Promise<JobListItem[]> {
  const r = await fetch(`${API_URL}/research?limit=${limit}`);
  if (!r.ok) throw new Error(`GET /research: ${r.status}`);
  return (await r.json()).jobs;
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
  "plan", "commands", "note", "usage", "done",
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
  onError?: (e: Event) => void,
  after = 0
): SSESubscription {
  // `after` skips what the client already has; on reconnect the browser also
  // sends Last-Event-ID (the server sends seq as the event id).
  const es = new EventSource(`${API_URL}/research/${jobId}/events?after=${after}`);

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
