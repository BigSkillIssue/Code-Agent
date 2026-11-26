import { describe, expect, it } from "vitest";
import type { AgentsView } from "../api/account";
import { chainOf, mergedAgents } from "./AdminAgents";

const view: AgentsView = {
  user_model_choice: true,
  roles: {},
  parallel: {},
  tiers: ["default", "pro"],
  role_groups: ["planner", "coder"],
  builtin: [{ name: "ui", description: "Designs", prompt: "You are the UI agent.", policy: "required", applies_to: "ui" }],
  agents: [
    { name: "ui", description: "", prompt: "", tools: [], policy: "off", applies_to: "ui" },
    { name: "seo", description: "Search", prompt: "You are the SEO agent.", tools: [], policy: "recommended", applies_to: "ui" },
  ],
};

describe("admin agents", () => {
  it("reads model chains from comma-separated text", () => {
    expect(chainOf(" anthropic/claude-sonnet, openai/gpt-5 ,")).toEqual(["anthropic/claude-sonnet", "openai/gpt-5"]);
  });

  it("shows built-ins with the admin's changes, then the admin's own agents", () => {
    const merged = mergedAgents(view);
    expect(merged.map((a) => a.name)).toEqual(["ui", "seo"]);
    expect(merged[0]).toMatchObject({ prompt: "You are the UI agent.", description: "Designs", policy: "off" });
  });
});
