import type { GuardView } from "../lib/view";

const BARS: [string, string, string][] = [
  ["llm_calls", "max_llm_calls", "LLM calls"],
  ["input_tokens", "max_input_tokens", "input tokens"],
  ["searches", "max_searches", "searches"],
  ["extracts", "max_extracts", "page reads"],
];

/** Job budgets: the only budgets with real limits (the guard enforces them). */
export default function BudgetBars({ guard }: { guard: GuardView }) {
  const rows = BARS.filter(([, lim]) => guard.limits[lim]);
  if (rows.length === 0) return null;
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
      {rows.map(([cnt, lim, label]) => {
        const v = guard.counters[cnt] ?? 0;
        const max = guard.limits[lim];
        const pct = Math.min(100, (v / max) * 100);
        const color = pct >= 100 ? "bg-red-500" : pct >= 80 ? "bg-amber-400" : "bg-accent-500";
        return (
          <div key={cnt} title={`${v} of ${max}`}>
            <div className="flex justify-between text-[11px] font-mono text-slate-400">
              <span>{label}</span>
              <span>{v >= 1000 ? `${Math.round(v / 1000)}k` : v}/{max >= 1000 ? `${Math.round(max / 1000)}k` : max}</span>
            </div>
            <div className="h-1.5 rounded bg-base-700 overflow-hidden mt-1">
              <div className={`h-full ${color} transition-all`} style={{ width: `${pct}%` }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}
