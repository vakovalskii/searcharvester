import { describe, expect, it } from "vitest";
import type { ModelInfo, RoleModels } from "./api";
import { fitChoice, mergeChoice, reasoningBadge, reasoningModes, requestModels, roleKey, rolesFor } from "./models";

const MODELS: ModelInfo[] = [
  { id: "qwen3.8-27b", reasoning: true, context: 262144 },
  { id: "qwen3.6-35b-a3b-noreason", reasoning: false, context: 262144 },
  { id: "plain", reasoning: null, context: null },
];
const base = { model: "qwen3.6-35b-a3b-noreason", reasoning: "auto" } as const;
const DEFAULTS: RoleModels = { lead: { ...base }, researcher: { ...base }, critic: { ...base }, fact_checker: { ...base } };

describe("reasoning modes", () => {
  it("a model that never thinks has nothing to tune", () => {
    expect(reasoningModes(MODELS[1])).toEqual(["auto"]);
    expect(reasoningModes(MODELS[0])).toContain("high");
    expect(reasoningModes(undefined)).toContain("off");
  });
  it("switching to a non-thinking model drops the effort", () => {
    expect(fitChoice({ model: "qwen3.6-35b-a3b-noreason", reasoning: "high" }, MODELS).reasoning).toBe("auto");
    expect(fitChoice({ model: "qwen3.8-27b", reasoning: "high" }, MODELS).reasoning).toBe("high");
  });
  it("badges say what the role does", () => {
    expect(reasoningBadge(MODELS[1], "auto")).toBe("no thinking");
    expect(reasoningBadge(MODELS[0], "low")).toBe("thinks · low");
    expect(reasoningBadge(MODELS[0], "off")).toBe("thinking off");
    expect(reasoningBadge(MODELS[2], "auto")).toBe("");
  });
});

describe("choice", () => {
  it("saved choice wins, a model gone from the gateway falls back", () => {
    const m = mergeChoice(DEFAULTS, { lead: { model: "qwen3.8-27b", reasoning: "low" },
                                      critic: { model: "retired-model", reasoning: "auto" } }, MODELS);
    expect(m.lead).toEqual({ model: "qwen3.8-27b", reasoning: "low" });
    expect(m.critic.model).toBe("qwen3.6-35b-a3b-noreason");
    expect(m.researcher).toEqual(base);
  });
  it("sends only the roles of the depth that differ", () => {
    const choice = { ...DEFAULTS, lead: { model: "qwen3.8-27b", reasoning: "low" as const },
                     critic: { model: "qwen3.8-27b", reasoning: "auto" as const } };
    expect(requestModels(choice, DEFAULTS, "quick")).toEqual({ lead: choice.lead });
    expect(Object.keys(requestModels(choice, DEFAULTS, "deep")!)).toEqual(["lead", "critic"]);
    expect(requestModels(DEFAULTS, DEFAULTS, "deep")).toBeUndefined();
    expect(rolesFor("quick")).toEqual(["lead"]);
  });
  it("maps view roles to settings keys", () => {
    expect(roleKey("fact-checker", "sub-a-2")).toBe("fact_checker");
    expect(roleKey("sub-agent", "lead")).toBe("lead");
    expect(roleKey("sub-agent", "sub-a-1")).toBeNull();
  });
});
