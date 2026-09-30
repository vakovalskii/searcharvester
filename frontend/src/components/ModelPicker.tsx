import { useState } from "react";
import { Brain, ChevronRight, Cpu } from "lucide-react";
import type { Depth, ModelInfo, ModelOptions, Reasoning, RoleKey, RoleModels } from "../lib/api";
import { REASONING_TITLE, ROLE_TITLE, fitChoice, reasoningBadge, reasoningModes, rolesFor, shortModel } from "../lib/models";

interface Props {
  depth: Depth;
  options: ModelOptions | null;
  loadError: string | null;
  value: RoleModels | null;
  onChange: (v: RoleModels) => void;
  disabled: boolean;
}

function k(n: number | null): string {
  return n == null ? "" : n >= 1000 ? `${Math.round(n / 1024)}k ctx` : `${n} ctx`;
}

function RoleRow({ role, value, models, onChange, disabled }: {
  role: RoleKey; value: RoleModels; models: ModelInfo[]; onChange: (v: RoleModels) => void; disabled: boolean;
}) {
  const c = value[role];
  const info = models.find((m) => m.id === c.model);
  const modes = reasoningModes(info);
  const set = (next: Partial<typeof c>) => onChange({ ...value, [role]: fitChoice({ ...c, ...next }, models) });
  const thinking = models.filter((m) => m.reasoning !== false);
  const plain = models.filter((m) => m.reasoning === false);
  const badge = reasoningBadge(info, c.reasoning);
  return (
    <div className="grid grid-cols-[minmax(0,9rem)_minmax(0,1fr)_minmax(0,7.5rem)] gap-2 items-center max-sm:grid-cols-1">
      <div className="min-w-0">
        <div className="text-sm text-slate-100">{ROLE_TITLE[role].title}</div>
        <div className="text-[11px] text-slate-500 truncate">{ROLE_TITLE[role].hint}</div>
      </div>
      {models.length > 0 ? (
        <select value={c.model ?? ""} disabled={disabled} onChange={(e) => set({ model: e.target.value || null })}
                aria-label={`${ROLE_TITLE[role].title} model`}
                className="min-w-0 rounded-md bg-base-900 border border-base-700 px-2 py-1.5 text-xs font-mono text-slate-200
                           focus:border-accent-500 focus:outline-none">
          {c.model && !info && <option value={c.model}>{c.model} (not in the list)</option>}
          {thinking.length > 0 && (
            <optgroup label="thinking models">
              {thinking.map((m) => <option key={m.id} value={m.id}>{m.id}{m.context ? `  ·  ${k(m.context)}` : ""}</option>)}
            </optgroup>
          )}
          {plain.length > 0 && (
            <optgroup label="no thinking">
              {plain.map((m) => <option key={m.id} value={m.id}>{m.id}{m.context ? `  ·  ${k(m.context)}` : ""}</option>)}
            </optgroup>
          )}
        </select>
      ) : (
        <input value={c.model ?? ""} disabled={disabled} onChange={(e) => set({ model: e.target.value.trim() || null })}
               placeholder="model id" aria-label={`${ROLE_TITLE[role].title} model`}
               className="min-w-0 rounded-md bg-base-900 border border-base-700 px-2 py-1.5 text-xs font-mono text-slate-200
                          focus:border-accent-500 focus:outline-none" />
      )}
      <div className="flex items-center gap-1.5 min-w-0">
        {modes.length > 1 ? (
          <select value={c.reasoning} disabled={disabled} onChange={(e) => set({ reasoning: e.target.value as Reasoning })}
                  aria-label={`${ROLE_TITLE[role].title} reasoning`}
                  title="auto: the model and the gateway decide · low/medium/high: reasoning_effort · off: ask to skip thinking (a gateway may force it)"
                  className="min-w-0 flex-1 rounded-md bg-base-900 border border-base-700 px-2 py-1.5 text-xs text-slate-200
                             focus:border-accent-500 focus:outline-none">
            {modes.map((m) => <option key={m} value={m}>{m === "auto" ? "thinking: auto" : `thinking: ${REASONING_TITLE[m]}`}</option>)}
          </select>
        ) : (
          <span className="text-[11px] text-slate-500 px-1">{badge || "—"}</span>
        )}
      </div>
    </div>
  );
}

/** "Models" under the depth switch: which model and how much thinking per role. */
export default function ModelPicker({ depth, options, loadError, value, onChange, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const roles = rolesFor(depth);
  const models = options?.models ?? [];
  const summary = value
    ? roles.map((r) => {
        const info = models.find((m) => m.id === value[r].model);
        const b = reasoningBadge(info, value[r].reasoning);
        return `${depth === "quick" ? "agent" : ROLE_TITLE[r].title.toLowerCase()}: ${shortModel(value[r].model) || "default"}${b ? ` (${b})` : ""}`;
      }).join(" · ")
    : loadError ? "models: defaults of the server" : "loading models…";
  return (
    <div className="mt-2 rounded-lg border border-base-700">
      <button type="button" onClick={() => setOpen(!open)} aria-expanded={open}
              className="w-full flex items-center gap-2 px-3 py-2 text-left">
        <ChevronRight size={14} className={`text-slate-500 transition-transform ${open ? "rotate-90" : ""}`} />
        <Cpu size={14} className="text-slate-400" />
        <span className="text-sm text-slate-200">Models</span>
        <span className="text-[11px] text-slate-500 truncate min-w-0">{summary}</span>
      </button>
      {open && (
        <div className="px-3 pb-3 space-y-2.5 border-t border-base-800 pt-2.5">
          {loadError && <div className="text-[11px] text-amber-300">Model list unavailable ({loadError}); type a model id or keep the defaults.</div>}
          {options?.error && <div className="text-[11px] text-amber-300">Gateway: {options.error}. The list may be stale.</div>}
          {value && roles.map((r) => (
            <RoleRow key={r} role={r} value={value} models={models} onChange={onChange} disabled={disabled} />
          ))}
          {depth === "quick" && <div className="text-[11px] text-slate-500">Quick research is one agent: the orchestrator's model answers alone.</div>}
          <div className="flex items-start gap-1.5 text-[11px] text-slate-500">
            <Brain size={12} className="mt-0.5 shrink-0" />
            <span>
              Thinking models spend more output tokens and time per step; the job budget counts them too.
              On the NeuralDeep gateway a <span className="font-mono">-noreason</span> alias never thinks and a base Qwen always does,
              whatever the switch says.
            </span>
          </div>
        </div>
      )}
    </div>
  );
}
