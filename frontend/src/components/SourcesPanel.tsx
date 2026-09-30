import type { JobView } from "../lib/view";

interface Props {
  view: JobView;
  onSelectAgent: (id: string) => void;
}

/** Source cards: domain, who read it, whether the report cites it. No previews yet (stage D). */
export default function SourcesPanel({ view, onSelectAgent }: Props) {
  const sources = [...view.sources.values()].sort((a, b) => Number(b.inReport) - Number(a.inReport));
  if (sources.length === 0) return <div className="text-sm text-slate-500 p-4">No pages read yet</div>;
  return (
    <div className="grid sm:grid-cols-2 gap-2 p-1">
      {sources.map((s) => (
        <div key={s.url} className={`rounded-lg border px-3 py-2 text-sm bg-base-900/60
          ${s.inReport ? "border-emerald-500/40" : "border-base-700"}`}>
          <div className="flex items-center gap-2">
            <span className="font-medium text-slate-200 truncate">{s.domain}</span>
            {s.inReport && <span className="text-[10px] uppercase tracking-wide text-emerald-400">cited</span>}
          </div>
          <a href={s.url} target="_blank" rel="noopener noreferrer"
             className="block text-xs text-slate-500 hover:text-accent-400 truncate">{s.url}</a>
          <div className="flex flex-wrap gap-1 mt-1">
            {s.readers.map((r) => (
              <button key={r} onClick={() => onSelectAgent(r)}
                      className="text-[10px] font-mono px-1.5 py-0.5 rounded bg-base-700 text-slate-400 hover:text-slate-100">
                {view.agents.get(r)?.role ?? r}
              </button>
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}
