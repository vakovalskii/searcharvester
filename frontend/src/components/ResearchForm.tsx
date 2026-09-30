import { KeyboardEvent, useEffect, useRef, useState } from "react";
import { SendHorizontal } from "lucide-react";
import type { Depth, ModelOptions, RoleModels } from "../lib/api";
import { getModelOptions } from "../lib/api";
import { loadSaved, mergeChoice, requestModels, saveChoice } from "../lib/models";
import ModelPicker from "./ModelPicker";

interface Props {
  onSubmit: (query: string, depth: Depth, models?: Partial<RoleModels>) => void;
  disabled: boolean;
}

const DEPTHS: { id: Depth; title: string; hint: string }[] = [
  { id: "quick", title: "Quick", hint: "one agent, a few searches, ~2 min" },
  { id: "deep", title: "Deep", hint: "a team of agents and a critic, ~10 min, many tokens" },
];

/**
 * Large textarea for the research query.
 * Cmd/Ctrl+Enter submits; Enter inserts a newline.
 */
export default function ResearchForm({ onSubmit, disabled }: Props) {
  const [query, setQuery] = useState("");
  const [depth, setDepth] = useState<Depth>("quick");
  const [options, setOptions] = useState<ModelOptions | null>(null);
  const [optionsError, setOptionsError] = useState<string | null>(null);
  const [models, setModels] = useState<RoleModels | null>(null);
  const ref = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    ref.current?.focus();
  }, []);

  useEffect(() => {
    let live = true;
    getModelOptions()
      .then((o) => {
        if (!live) return;
        setOptions(o);
        setModels(mergeChoice(o.defaults, loadSaved(), o.models));
      })
      .catch((e: Error) => live && setOptionsError(e.message));
    return () => { live = false; };
  }, []);

  const changeModels = (v: RoleModels) => {
    setModels(v);
    saveChoice(v);
  };

  const submit = () => {
    const q = query.trim();
    if (!q || disabled) return;
    onSubmit(q, depth, models && options ? requestModels(models, options.defaults, depth) : undefined);
  };

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
      e.preventDefault();
      submit();
    }
  };

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        submit();
      }}
      className="w-full"
    >
      <div className="relative">
        <textarea
          ref={ref}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKey}
          disabled={disabled}
          placeholder="Ask a research question…  (Cmd/Ctrl+Enter to run)"
          rows={4}
          className="w-full resize-none rounded-xl bg-base-800 border border-base-700
                     focus:border-accent-500 focus:outline-none focus:ring-1 focus:ring-accent-500
                     px-4 py-3 pr-14 text-slate-100 placeholder:text-slate-500
                     font-medium text-base leading-relaxed
                     disabled:opacity-50 disabled:cursor-not-allowed"
        />
        <button
          type="submit"
          disabled={disabled || !query.trim()}
          className="absolute right-3 bottom-3 rounded-lg bg-accent-500 hover:bg-accent-400
                     disabled:bg-base-700 disabled:text-slate-500 disabled:cursor-not-allowed
                     text-white p-2 transition-colors"
          aria-label="Submit"
        >
          <SendHorizontal size={18} />
        </button>
      </div>
      <div className="flex gap-2 mt-3" role="radiogroup" aria-label="Research depth">
        {DEPTHS.map((d) => (
          <button key={d.id} type="button" role="radio" aria-checked={depth === d.id} onClick={() => setDepth(d.id)}
                  className={`flex-1 rounded-lg border px-3 py-2 text-left transition-colors
                    ${depth === d.id ? "border-accent-500 bg-accent-500/10" : "border-base-700 hover:border-base-600"}`}>
            <div className="text-sm text-slate-100">{d.title}</div>
            <div className="text-[11px] text-slate-500">{d.hint}</div>
          </button>
        ))}
      </div>
      <ModelPicker depth={depth} options={options} loadError={optionsError} value={models}
                   onChange={changeModels} disabled={disabled} />
      <div className="flex justify-between items-center text-xs text-slate-500 mt-2 px-1">
        <span>
          {query.length > 0 && `${query.length} chars`}
          {query.length > 2000 && (
            <span className="text-red-400 ml-2">max 2000</span>
          )}
        </span>
        <kbd className="font-mono">⌘ + ↵</kbd>
      </div>
    </form>
  );
}
