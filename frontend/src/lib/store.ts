/**
 * Per-job state (stage B): every job has its own record, so a response that
 * arrives for a job that is no longer on screen lands in that job's record and
 * never on the screen of another one. Events are keyed by seq: a reconnecting SSE
 * that replays history adds nothing twice.
 */
import type { AgentEvent, JobStatus } from "./api";

export interface JobRecord {
  jobId: string;
  query: string;
  events: Map<number, AgentEvent>;
  status: JobStatus | "interrupted" | null;
  report: string | null;
  error: string | null;
  durationSec: number | null;
}

export type Store = Map<string, JobRecord>;

export function emptyRecord(jobId: string, query = ""): JobRecord {
  return { jobId, query, events: new Map(), status: null, report: null, error: null, durationSec: null };
}

/** Immutable update of one job; the other records keep their identity. */
export function withJob(store: Store, jobId: string, update: (r: JobRecord) => JobRecord): Store {
  const next = new Map(store);
  next.set(jobId, update(store.get(jobId) ?? emptyRecord(jobId)));
  return next;
}

export function addEvents(store: Store, jobId: string, events: AgentEvent[]): Store {
  return withJob(store, jobId, (r) => {
    let changed = false;
    const ev = new Map(r.events);
    events.forEach((e, i) => {
      const seq = e.seq || ev.size + i + 1; // older servers: position
      if (!ev.has(seq)) { ev.set(seq, { ...e, seq }); changed = true; }
    });
    return changed ? { ...r, events: ev } : r;
  });
}

export function orderedEvents(r: JobRecord | undefined): AgentEvent[] {
  if (!r) return [];
  return [...r.events.values()].sort((a, b) => (a.seq ?? 0) - (b.seq ?? 0));
}

export function lastSeq(r: JobRecord | undefined): number {
  if (!r || r.events.size === 0) return 0;
  return Math.max(...r.events.keys());
}
