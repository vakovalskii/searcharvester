import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCircle2, KeyRound, Loader2, Plus, RefreshCw, RotateCw, Search, Trash2, XCircle } from "lucide-react";
import type { AdminStatus, ApplyResult, EngineRow, Extractor, ProbeResult } from "../lib/admin";
import {
  ADMIN_URL, AdminError, categoryEngines, errorRate, EXTRACTORS, getStatus, loadToken, probeCategory, putSettings,
  restartSearxng, saveToken, testProxy,
} from "../lib/admin";

const GENERAL_DEFAULT = "google,duckduckgo,brave";
const card = "rounded-xl border border-base-700 bg-base-800/40";
const input = "rounded-md bg-base-900 border border-base-700 px-2 py-1.5 text-xs font-mono text-slate-200 focus:border-accent-500 focus:outline-none";
const btn = "inline-flex items-center gap-1.5 rounded-md border border-base-600 px-2.5 py-1.5 text-xs text-slate-200 hover:border-slate-400 disabled:opacity-50";

function Section({ title, hint, children, right }: { title: string; hint?: string; children: React.ReactNode; right?: React.ReactNode }) {
  return (
    <section className={card}>
      <div className="flex items-start gap-3 px-4 py-3 border-b border-base-700">
        <div className="min-w-0">
          <h2 className="text-sm font-semibold text-slate-100">{title}</h2>
          {hint && <div className="text-[11px] text-slate-500 mt-0.5">{hint}</div>}
        </div>
        <div className="ml-auto shrink-0">{right}</div>
      </div>
      <div className="px-4 py-3">{children}</div>
    </section>
  );
}

function KV({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex gap-3 text-xs py-0.5">
      <span className="w-36 shrink-0 text-slate-500">{k}</span>
      <span className="min-w-0 font-mono text-slate-200 break-all">{v ?? "—"}</span>
    </div>
  );
}

function TokenGate({ onToken, error }: { onToken: (t: string) => void; error: string | null }) {
  const [t, setT] = useState("");
  return (
    <div className={`${card} max-w-xl mx-auto mt-10 p-5 space-y-3`}>
      <div className="flex items-center gap-2 text-slate-100"><KeyRound size={16} /> <span className="font-semibold">Admin token</span></div>
      <p className="text-xs text-slate-400 leading-relaxed">
        Search settings go through <span className="font-mono">search-admin</span> ({ADMIN_URL}). Its token is
        <span className="font-mono"> SEARCH_ADMIN_TOKEN</span> in <span className="font-mono">.env</span>, or, when unset, the one
        printed at start: <span className="font-mono">docker logs search-admin | grep token</span>. It stays in this browser only.
      </p>
      {error && <div className="text-xs text-red-300">{error}</div>}
      <form onSubmit={(e) => { e.preventDefault(); if (t.trim()) onToken(t.trim()); }} className="flex gap-2">
        <input type="password" value={t} onChange={(e) => setT(e.target.value)} placeholder="token" className={`${input} flex-1`} autoFocus />
        <button className={btn} type="submit">Open</button>
      </form>
    </div>
  );
}

function ProxyRow({ value, onChange, onRemove, token }: { value: string; onChange: (v: string) => void; onRemove?: () => void; token: string }) {
  const [res, setRes] = useState<{ ok: boolean; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setRes(null);
    try {
      const r = await testProxy(token, value);
      setRes({ ok: r.ok, text: r.ok ? `ok · ${r.seconds}s` : (r.error ?? `status ${r.status}`) });
    } catch (e) {
      setRes({ ok: false, text: (e as Error).message });
    } finally { setBusy(false); }
  };
  return (
    <div className="flex items-center gap-2">
      <input value={value} onChange={(e) => { onChange(e.target.value); setRes(null); }}
             placeholder="socks5h://user:pass@host:1080  or  http://host:3128" className={`${input} flex-1 min-w-0`} />
      <button type="button" className={btn} disabled={!value.trim() || busy} onClick={run}>
        {busy ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />} test
      </button>
      {onRemove && <button type="button" className={btn} onClick={onRemove} aria-label="remove"><Trash2 size={12} /></button>}
      {res && <span className={`text-[11px] ${res.ok ? "text-emerald-400" : "text-red-300"} max-w-[40%] truncate`} title={res.text}>{res.text}</span>}
    </div>
  );
}

function EngineTable({ rows, probe, onToggle }: {
  rows: (EngineRow & { on: boolean; changed: boolean })[]; probe: ProbeResult | null; onToggle: (name: string, on: boolean) => void;
}) {
  const unresp = new Map((probe?.unresponsive ?? []).map((u) => [u.engine, u.reason]));
  return (
    <table className="w-full text-xs">
      <thead>
        <tr className="text-left text-[11px] text-slate-500">
          <th className="py-1 w-10">on</th><th>engine</th><th className="w-20">errors</th><th>last check</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((e) => {
          const rate = errorRate(e);
          const answered = probe?.answered?.[e.name];
          const reason = unresp.get(e.name);
          return (
            <tr key={e.name} className={`border-t border-base-800 ${e.changed ? "bg-accent-500/5" : ""}`}>
              <td className="py-1"><input type="checkbox" checked={e.on} onChange={(ev) => onToggle(e.name, ev.target.checked)} aria-label={`enable ${e.name}`} /></td>
              <td className={`font-mono ${e.on ? "text-slate-200" : "text-slate-500"}`}>
                {e.name}{e.shortcut ? <span className="text-slate-600"> !{e.shortcut}</span> : null}
                {e.changed && <span className="ml-2 text-[10px] text-accent-400">{e.on ? "will enable" : "will disable"}</span>}
              </td>
              <td title={e.errors.map((x) => `${x.percentage}% ${x.exception ?? ""} ${x.message}`).join("\n")}
                  className={rate >= 50 ? "text-red-300" : rate > 0 ? "text-amber-300" : "text-slate-600"}>
                {rate ? `${rate}%` : "—"}
              </td>
              <td className="text-[11px]">
                {!probe ? <span className="text-slate-600">—</span>
                  : answered ? <span className="text-emerald-400">{answered} results</span>
                  : reason ? <span className="text-red-300">{reason}</span>
                  : e.enabled ? <span className="text-slate-500">no results</span> : <span className="text-slate-600">off</span>}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

/** Search engine settings: the SearXNG container, its engines and proxies, and
 *  the adapter's defaults. Apply writes them through search-admin, which
 *  restarts SearXNG when its part changed. */
export default function SettingsPage() {
  const [token, setToken] = useState(loadToken());
  const [st, setSt] = useState<AdminStatus | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [cat, setCat] = useState("general");
  const [filter, setFilter] = useState("");
  const [draftEngines, setDraftEngines] = useState<Record<string, boolean>>({});
  const [proxies, setProxies] = useState<string[]>([]);
  const [rt, setRt] = useState(""), [mrt, setMrt] = useState("");
  const [defaults, setDefaults] = useState<Record<string, string>>({});
  const [readerProxy, setReaderProxy] = useState("");
  const [extractor, setExtractor] = useState<Extractor>("auto");
  const [probes, setProbes] = useState<Record<string, ProbeResult>>({});
  const [probing, setProbing] = useState(false);
  const [applying, setApplying] = useState(false);
  const [result, setResult] = useState<ApplyResult | null>(null);

  const load = useCallback(async (t: string) => {
    setLoading(true); setErr(null);
    try {
      const s = await getStatus(t);
      setSt(s);
      setDraftEngines({});
      setProxies(s.overrides.searxng.proxies.length ? s.overrides.searxng.proxies : []);
      setRt(s.effective.request_timeout?.toString() ?? ""); setMrt(s.effective.max_request_timeout?.toString() ?? "");
      setDefaults(Object.fromEntries(Object.entries(s.overrides.adapter.default_engines).map(([k, v]) => [k, v.join(",")])));
      setReaderProxy(s.overrides.adapter.reader_proxy ?? "");
      setExtractor(s.overrides.adapter.extractor ?? "auto");
    } catch (e) {
      const ae = e as AdminError;
      if (ae.status === 401) { saveToken(""); setToken(""); }
      setErr(ae.message);
    } finally { setLoading(false); }
  }, []);

  useEffect(() => { if (token) load(token); }, [token, load]);

  const rows = useMemo(() => st ? categoryEngines(st.searxng.engines, cat, draftEngines)
    .filter((e) => !filter || e.name.includes(filter.toLowerCase())) : [], [st, cat, draftEngines, filter]);

  if (!token) return <TokenGate error={err} onToken={(t) => { saveToken(t); setToken(t); }} />;
  if (!st) {
    return <div className="text-sm text-slate-500 flex items-center gap-2 p-4">
      {loading ? <><Loader2 size={14} className="animate-spin" /> loading settings…</> : err ?? ""}
    </div>;
  }

  const ov = st.overrides;
  const engineChanges = Object.entries(draftEngines).filter(([n, on]) => st.searxng.engines.find((e) => e.name === n)?.enabled !== on);
  const num = (v: string) => (v.trim() ? Number(v) : null);
  const dirtySearxng = engineChanges.length > 0
    || JSON.stringify(proxies.filter((p) => p.trim())) !== JSON.stringify(ov.searxng.proxies)
    || num(rt) !== (st.effective.request_timeout ?? null) || num(mrt) !== (st.effective.max_request_timeout ?? null);
  const defaultsNow = Object.fromEntries(Object.entries(defaults).filter(([, v]) => v.trim()));
  const defaultsWas = Object.fromEntries(Object.entries(ov.adapter.default_engines).map(([k, v]) => [k, v.join(",")]));
  const dirtyAdapter = JSON.stringify(defaultsNow) !== JSON.stringify(defaultsWas) || (readerProxy || null) !== (ov.adapter.reader_proxy || null)
    || extractor !== (ov.adapter.extractor ?? "auto");

  const apply = async () => {
    setApplying(true); setResult(null);
    try {
      const body = {
        searxng: {
          engines: { ...ov.searxng.engines, ...Object.fromEntries(engineChanges) },
          proxies: proxies.filter((p) => p.trim()),
          request_timeout: num(rt), max_request_timeout: num(mrt),
        },
        adapter: {
          default_engines: Object.fromEntries(st.categories.map((c) => [c, (defaults[c] ?? "").split(",").map((x) => x.trim()).filter(Boolean)])),
          reader_proxy: readerProxy.trim() || null,
          extractor,
        },
      };
      const r = await putSettings(token, body);
      setResult(r);
      if (!r.errors) { setProbes({}); await load(token); }
    } catch (e) {
      setResult({ errors: [(e as Error).message] });
    } finally { setApplying(false); }
  };

  const runProbe = async () => {
    setProbing(true);
    try { const r = await probeCategory(token, cat); setProbes((p) => ({ ...p, [cat]: r })); }
    finally { setProbing(false); }
  };

  const c = st.container;
  const catEnabled = st.searxng.engines.filter((e) => e.categories.includes(cat) && (draftEngines[e.name] ?? e.enabled)).map((e) => e.name);

  return (
    <div className="max-w-5xl mx-auto space-y-4 pb-24">
      <div className="flex items-center gap-3">
        <h1 className="text-lg font-semibold text-slate-100">Search settings</h1>
        <button className={btn} onClick={() => load(token)} disabled={loading}>
          <RefreshCw size={12} className={loading ? "animate-spin" : ""} /> refresh
        </button>
        <button className={`${btn} ml-auto`} onClick={() => { saveToken(""); setToken(""); setSt(null); }}>forget token</button>
      </div>

      <Section title="SearXNG container" hint="What runs now, as Docker and SearXNG report it."
               right={<button className={btn} disabled={applying} onClick={async () => {
                 setApplying(true); setResult(null);
                 try { const r = await restartSearxng(token); setResult({ restarted: true, restart: r }); await load(token); }
                 finally { setApplying(false); }
               }}><RotateCw size={12} /> restart</button>}>
        {c.error ? <div className="text-xs text-red-300">{c.error}</div> : (
          <div className="grid md:grid-cols-2 gap-x-6">
            <div>
              <KV k="container" v={c.name} />
              <KV k="image" v={`${c.image} (${c.image_id})`} />
              <KV k="SearXNG version" v={st.searxng.version ?? (st.searxng.reachable ? "?" : `not reachable: ${st.searxng.error}`)} />
              <KV k="state" v={<span className={c.state === "running" ? "text-emerald-400" : "text-red-300"}>{c.state}{c.health ? ` · ${c.health}` : ""}</span>} />
              <KV k="started" v={c.started_at ? new Date(c.started_at).toLocaleString() : "—"} />
              <KV k="restarts / policy" v={`${c.restart_count} / ${c.restart_policy}`} />
              <KV k="ports" v={c.ports?.join(", ")} />
              <KV k="networks" v={c.networks?.join(", ")} />
            </div>
            <div>
              <KV k="settings file" v={c.env?.SEARXNG_SETTINGS_PATH ?? "/etc/searxng/settings.yml"} />
              <KV k="base config" v="config.yaml (repo root) + page overrides" />
              <KV k="limiter" v={String(st.effective.limiter)} />
              <KV k="image proxy" v={String(st.effective.image_proxy)} />
              <KV k="safe search" v={String(st.effective.safesearch ?? st.searxng.safe_search)} />
              <KV k="formats" v={st.effective.formats?.join(", ")} />
              <KV k="engines on / all" v={`${st.searxng.engines.filter((e) => e.enabled).length} / ${st.searxng.engines.length}`} />
              {c.env && Object.entries(c.env).map(([k, v]) => <KV key={k} k={k} v={v} />)}
            </div>
            <div className="md:col-span-2 mt-2">
              <div className="text-[11px] text-slate-500 mb-1">mounts</div>
              {c.mounts?.map((m) => (
                <div key={m.destination} className="text-[11px] font-mono text-slate-400 break-all">
                  {m.source} → {m.destination} <span className="text-slate-600">{m.rw ? "rw" : "ro"}</span>
                </div>
              ))}
            </div>
          </div>
        )}
      </Section>

      <Section title="SearXNG proxies" hint="Every engine request goes through these (SearXNG rotates the list). Empty: direct. Against 'too many requests' and 'access denied' from engines.">
        <div className="space-y-2">
          {proxies.map((p, i) => (
            <ProxyRow key={i} value={p} token={token} onChange={(v) => setProxies(proxies.map((x, j) => (j === i ? v : x)))}
                      onRemove={() => setProxies(proxies.filter((_, j) => j !== i))} />
          ))}
          <button type="button" className={btn} onClick={() => setProxies([...proxies, ""])}><Plus size={12} /> add proxy</button>
          <div className="flex flex-wrap items-center gap-3 pt-2 text-xs text-slate-400">
            <label className="flex items-center gap-2">request timeout, s
              <input value={rt} onChange={(e) => setRt(e.target.value)} className={`${input} w-16`} inputMode="decimal" /></label>
            <label className="flex items-center gap-2">max request timeout, s
              <input value={mrt} onChange={(e) => setMrt(e.target.value)} className={`${input} w-16`} inputMode="decimal" /></label>
          </div>
        </div>
      </Section>

      <Section title="Engines" hint="On and off per engine in SearXNG. Errors: SearXNG's own error stats since its start. 'Check' runs one search in the category."
               right={<button className={btn} disabled={probing} onClick={runProbe}>
                 {probing ? <Loader2 size={12} className="animate-spin" /> : <Search size={12} />} check {cat}</button>}>
        <div className="flex flex-wrap gap-1 mb-2">
          {st.categories.map((k) => {
            const n = st.searxng.engines.filter((e) => e.categories.includes(k) && (draftEngines[e.name] ?? e.enabled)).length;
            return (
              <button key={k} onClick={() => setCat(k)}
                      className={`px-2 py-1 rounded-md text-xs border ${k === cat ? "border-accent-500 text-slate-100 bg-accent-500/10" : "border-base-700 text-slate-400"}`}>
                {k} <span className="text-slate-500">{n}</span>
              </button>
            );
          })}
          <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="filter" className={`${input} ml-auto w-36`} />
        </div>
        {probes[cat] && (
          <div className="text-[11px] text-slate-400 mb-2">
            {probes[cat].error ? <span className="text-red-300">check failed: {probes[cat].error}</span>
              : `last check: ${probes[cat].results} results in ${probes[cat].seconds}s, ${probes[cat].unresponsive?.length ?? 0} engines did not answer`}
          </div>
        )}
        <div className="max-h-[28rem] overflow-y-auto">
          <EngineTable rows={rows} probe={probes[cat] ?? null} onToggle={(n, on) => setDraftEngines({ ...draftEngines, [n]: on })} />
        </div>
      </Section>

      <Section title="Adapter defaults" hint="Engines the adapter asks for when a caller (an agent's search.py) names none. Applied at once, no restart.">
        <div className="space-y-2">
          <div className="text-[11px] text-slate-500">
            Category <span className="text-slate-300">{cat}</span> (switch above). Empty: {cat === "general" || !["images", "videos"].includes(cat)
              ? <span className="font-mono">{cat === "general" ? GENERAL_DEFAULT : "google,duckduckgo,brave"}</span> : "SearXNG's own engines of the category"}.
          </div>
          <input value={defaults[cat] ?? ""} onChange={(e) => setDefaults({ ...defaults, [cat]: e.target.value })}
                 placeholder="engine,engine" className={`${input} w-full`} />
          <div className="flex flex-wrap gap-1">
            {catEnabled.map((n) => {
              const list = (defaults[cat] ?? "").split(",").map((x) => x.trim()).filter(Boolean);
              const on = list.includes(n);
              return (
                <button key={n} type="button" onClick={() => setDefaults({ ...defaults, [cat]: (on ? list.filter((x) => x !== n) : [...list, n]).join(",") })}
                        className={`px-1.5 py-0.5 rounded border text-[11px] font-mono ${on ? "border-accent-500 text-slate-100" : "border-base-700 text-slate-500"}`}>
                  {n}
                </button>
              );
            })}
          </div>
          {Object.keys(defaultsNow).length > 0 && (
            <div className="text-[11px] text-slate-500">set: {Object.entries(defaultsNow).map(([k, v]) => `${k}: ${v}`).join(" · ")}</div>
          )}
        </div>
      </Section>

      <Section title="Page extractor" hint="Which extractor turns a fetched page into text for /extract and search raw_content. Applied at once, no restart.">
        <div className="space-y-2">
          <div className="flex flex-wrap gap-1.5">
            {EXTRACTORS.map((x) => (
              <button key={x.id} type="button" onClick={() => setExtractor(x.id)}
                      className={`px-2 py-1 rounded border text-xs ${extractor === x.id ? "border-accent-500 bg-accent-500/15 text-slate-100" : "border-base-700 text-slate-400"}`}>
                <span className="font-mono">{x.id}</span>
              </button>
            ))}
          </div>
          <div className="text-[11px] text-slate-500">{EXTRACTORS.find((x) => x.id === extractor)?.hint}</div>
        </div>
      </Section>

      <Section title="Reader proxy" hint="Egress of page reads (/extract) and pictures (/media) when a site refuses our IP; over PROXY_URL. Applied at once.">
        <ProxyRow value={readerProxy} onChange={setReaderProxy} token={token} />
      </Section>

      <div className="fixed bottom-0 inset-x-0 border-t border-base-800 bg-base-950/95 backdrop-blur px-4 py-2.5">
        <div className="max-w-5xl mx-auto flex items-center gap-3">
          <button className={`${btn} border-accent-500 bg-accent-500/15`} disabled={applying || (!dirtySearxng && !dirtyAdapter)} onClick={apply}>
            {applying ? <Loader2 size={12} className="animate-spin" /> : <CheckCircle2 size={12} />}
            {dirtySearxng ? "apply and restart SearXNG" : "apply"}
          </button>
          <span className="text-[11px] text-slate-500">
            {dirtySearxng ? `${engineChanges.length} engine changes; SearXNG restarts (~10 s, running searches fail meanwhile)`
              : dirtyAdapter ? "adapter settings only, no restart" : "no changes"}
          </span>
          {result && (result.errors
            ? <span className="text-xs text-red-300 flex items-center gap-1 truncate" title={result.errors.join("\n")}><XCircle size={12} /> {result.errors[0]}</span>
            : result.restart && !result.restart.ok
              ? <span className="text-xs text-red-300 truncate">{result.restart.error}</span>
              : <span className="text-xs text-emerald-400">saved{result.restarted ? `, SearXNG back in ${result.restart?.seconds}s` : ""}</span>)}
        </div>
      </div>
    </div>
  );
}
