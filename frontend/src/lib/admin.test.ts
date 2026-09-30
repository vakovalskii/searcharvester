import { describe, expect, it } from "vitest";
import type { EngineRow } from "./admin";
import { categoryEngines, errorRate } from "./admin";

const E = (name: string, categories: string[], enabled: boolean, pct: number[] = []): EngineRow =>
  ({ name, categories, enabled, errors: pct.map((p) => ({ percentage: p, exception: "X", message: "" })) });

describe("admin helpers", () => {
  const engines = [E("google", ["general"], true, [10]), E("bing", ["general"], false), E("bing images", ["images"], true), E("brave", ["general"], true, [25, 60])];
  it("lists a category with the draft applied, on first", () => {
    const rows = categoryEngines(engines, "general", { bing: true, google: false });
    expect(rows.map((r) => [r.name, r.on, r.changed])).toEqual([["bing", true, true], ["brave", true, false], ["google", false, true]]);
  });
  it("a draft equal to the live state is not a change", () => {
    expect(categoryEngines(engines, "general", { brave: true }).find((r) => r.name === "brave")!.changed).toBe(false);
  });
  it("error rate is the worst error", () => {
    expect(errorRate(engines[3])).toBe(60);
    expect(errorRate(engines[1])).toBe(0);
  });
});
