/** Client of search-admin (search_admin/server.py): SearXNG container, engines,
 *  proxies, the adapter's search defaults. Every call carries the admin token,
 *  which lives only in this browser (localStorage) and in search-admin. */

export const ADMIN_URL =
  (import.meta as unknown as { env: Record<string, string> }).env.VITE_ADMIN_URL || "http://localhost:8011";

const TOKEN_KEY = "searcharvester.adminToken";

export function loadToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function saveToken(t: string): void {
  try {
    if (t) window.localStorage.setItem(TOKEN_KEY, t);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* blocked storage: the token lives until reload */
  }
}

export interface EngineRow {
  name: string;
  categories: string[];
  enabled: boolean;          // what the running SearXNG has
  shortcut?: string | null;
  timeout?: number | null;
  errors: { percentage: number | null; exception: string | null; message: string }[];
}

export interface Overrides {
  searxng: {
    engines: Record<string, boolean>;
    proxies: string[];
    request_timeout: number | null;
    max_request_timeout: number | null;
  };
  adapter: { default_engines: Record<string, string[]>; reader_proxy: string | null };
}

export interface AdminStatus {
  container: {
    error?: string; name?: string; image?: string; image_id?: string; created?: string; started_at?: string;
    state?: string; health?: string | null; restart_count?: number; restart_policy?: string;
    mounts?: { source: string; destination: string; rw: boolean }[]; ports?: string[];
    env?: Record<string, string>; networks?: string[];
  };
  searxng: { reachable: boolean; error?: string; version?: string; safe_search?: number; limiter?: boolean; engines: EngineRow[] };
  effective: {
    limiter?: boolean; image_proxy?: boolean; safesearch?: number; formats?: string[];
    request_timeout?: number | null; max_request_timeout?: number | null; proxies: string[];
  };
  overrides: Overrides;
  files: Record<string, string>;
  categories: string[];
}

export interface ApplyResult {
  saved?: boolean;
  restarted?: boolean;
  restart?: { ok: boolean; seconds?: number; error?: string };
  errors?: string[];
}

export interface ProbeResult {
  category?: string; seconds?: number; results?: number; error?: string;
  answered?: Record<string, number>;
  unresponsive?: { engine: string; reason: string }[];
}

export class AdminError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

async function call<T>(token: string, method: string, path: string, body?: unknown): Promise<T> {
  let r: Response;
  try {
    r = await fetch(`${ADMIN_URL}${path}`, {
      method,
      headers: { "X-Admin-Token": token, ...(body !== undefined ? { "Content-Type": "application/json" } : {}) },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new AdminError(0, `search-admin is not reachable at ${ADMIN_URL}`);
  }
  const data = await r.json().catch(() => ({}));
  if (r.status === 422) return data as T;              // validation errors come back as data
  if (!r.ok) throw new AdminError(r.status, (data as { error?: string }).error ?? `HTTP ${r.status}`);
  return data as T;
}

export const getStatus = (t: string) => call<AdminStatus>(t, "GET", "/api/status");
export const putSettings = (t: string, body: unknown) => call<ApplyResult>(t, "PUT", "/api/settings", body);
export const restartSearxng = (t: string) => call<{ ok: boolean; seconds?: number; error?: string }>(t, "POST", "/api/restart", {});
export const probeCategory = (t: string, category: string) => call<ProbeResult>(t, "POST", "/api/probe", { category });
export const testProxy = (t: string, proxy: string) =>
  call<{ ok: boolean; status?: number; seconds?: number; error?: string }>(t, "POST", "/api/proxy-test", { proxy });

/** Engines of a category with the enabled state the page is about to apply. */
export function categoryEngines(engines: EngineRow[], category: string, draft: Record<string, boolean>) {
  return engines
    .filter((e) => e.categories.includes(category))
    .map((e) => ({ ...e, on: draft[e.name] ?? e.enabled, changed: draft[e.name] !== undefined && draft[e.name] !== e.enabled }))
    .sort((a, b) => Number(b.on) - Number(a.on) || a.name.localeCompare(b.name));
}

export function errorRate(e: EngineRow): number {
  return Math.max(0, ...e.errors.map((x) => x.percentage ?? 0));
}
