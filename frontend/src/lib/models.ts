/**
 * Model and reasoning per agent role: what the pickers may offer, what the
 * form sends, and how a role shows up on the job screen. Pure functions, the
 * components only render them.
 */
import type { Depth, ModelInfo, Reasoning, RoleChoice, RoleKey, RoleModels } from "./api";
import { ROLE_KEYS } from "./api";

export const ROLE_TITLE: Record<RoleKey, { title: string; hint: string }> = {
  lead: { title: "Orchestrator", hint: "plans, sends the rounds out, writes the report" },
  researcher: { title: "Researchers", hint: "one per sub-question, search and read" },
  critic: { title: "Critic", hint: "tries to disprove the findings" },
  fact_checker: { title: "Fact-checker", hint: "re-checks numbers, dates, names" },
};

export const REASONING_TITLE: Record<Reasoning, string> = {
  auto: "auto", off: "off", low: "low", medium: "medium", high: "high",
};

/** Roles that run for a depth: a quick job is the lead alone. */
export function rolesFor(depth: Depth): RoleKey[] {
  return depth === "quick" ? ["lead"] : ROLE_KEYS;
}

/**
 * Reasoning modes that make sense for a model. A model that never thinks
 * (reasoning: false, e.g. a -noreason alias) has nothing to tune: only auto.
 * A thinking model gets the effort levels and off; off may be ignored where
 * the gateway forces thinking for that model name.
 */
export function reasoningModes(model: ModelInfo | undefined): Reasoning[] {
  if (model?.reasoning === false) return ["auto"];
  return ["auto", "low", "medium", "high", "off"];
}

/** A choice that fits its model: a mode the model cannot use falls back to auto. */
export function fitChoice(choice: RoleChoice, models: ModelInfo[]): RoleChoice {
  const info = models.find((m) => m.id === choice.model);
  return reasoningModes(info).includes(choice.reasoning) ? choice : { ...choice, reasoning: "auto" };
}

/** What a role will do, in a few words: for the picker row and the job cards. */
export function reasoningBadge(model: ModelInfo | undefined, reasoning: Reasoning | undefined): string {
  if (model?.reasoning === false) return "no thinking";
  if (reasoning === "off") return "thinking off";
  if (reasoning && reasoning !== "auto") return `thinks · ${reasoning}`;
  return model?.reasoning ? "thinks" : "";
}

const STORE_KEY = "searcharvester.roleModels.v1";

/** The viewer's last choice over the server defaults, role by role; a model the
 *  gateway no longer serves drops back to the default. */
export function mergeChoice(defaults: RoleModels, saved: Partial<RoleModels> | null, models: ModelInfo[]): RoleModels {
  const known = new Set(models.map((m) => m.id));
  const out = {} as RoleModels;
  for (const r of ROLE_KEYS) {
    const d = defaults[r] ?? { model: null, reasoning: "auto" };
    const s = saved?.[r];
    const model = s?.model && (known.size === 0 || known.has(s.model)) ? s.model : d.model;
    out[r] = fitChoice({ model, reasoning: (s?.reasoning ?? d.reasoning ?? "auto") as Reasoning }, models);
  }
  return out;
}

export function loadSaved(): Partial<RoleModels> | null {
  try {
    const raw = window.localStorage.getItem(STORE_KEY);
    return raw ? (JSON.parse(raw) as Partial<RoleModels>) : null;
  } catch {
    return null;
  }
}

export function saveChoice(choice: RoleModels): void {
  try {
    window.localStorage.setItem(STORE_KEY, JSON.stringify(choice));
  } catch {
    /* private window or blocked storage: the choice lives until reload */
  }
}

/** The request body part: only the roles of this depth, only what differs
 *  from the defaults (the server fills the rest), undefined when nothing does. */
export function requestModels(choice: RoleModels, defaults: RoleModels, depth: Depth): Partial<RoleModels> | undefined {
  const out: Partial<RoleModels> = {};
  for (const r of rolesFor(depth)) {
    const c = choice[r], d = defaults[r];
    if (!c) continue;
    if (c.model !== d?.model || c.reasoning !== d?.reasoning) out[r] = c;
  }
  return Object.keys(out).length ? out : undefined;
}

/** View role ("fact-checker", "researcher", "lead") to its settings key. */
export function roleKey(role: string, id: string): RoleKey | null {
  if (id === "lead") return "lead";
  if (role === "fact-checker") return "fact_checker";
  return role === "researcher" || role === "critic" ? role : null;
}

/** "qwen3.6-35b-a3b-noreason" is long for a chip: drop what the badge already says. */
export function shortModel(id: string | null | undefined): string {
  return (id ?? "").replace(/-noreason$/, "");
}
