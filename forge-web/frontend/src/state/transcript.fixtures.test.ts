// The transcript of chats recorded from a real server (tests/record_ui_fixtures.py).

import { describe, expect, it } from "vitest";
import type { StoredItem } from "../api/types";
import plan from "../fixtures/chat-plan.json";
import tools from "../fixtures/chat-tools.json";
import { buildTranscript, type Entry } from "./transcript";

const items = (fixture: { items: unknown[] }) => fixture.items as StoredItem[];
const shape = (entries: Entry[]) =>
  entries.map((e) => (e.kind === "tool" ? `tool:${e.call.name}:${e.status}` : e.kind));

describe("a recorded small task", () => {
  const transcript = buildTranscript(items(tools));

  it("shows each tool with its approval right below it", () => {
    expect(shape(transcript.entries)).toEqual([
      "user",
      "structured",
      "tool:write_file:ok",
      "approval",
      "tool:bash:ok",
      "approval",
      "tool:edit_file:ok",
      "approval",
      "question",
      "assistant",
      "turn",
    ]);
    expect(transcript.pending).toEqual([]);
  });

  it("keeps the todo list out of the transcript and up to date", () => {
    expect(transcript.todos.map((t) => t.status)).toEqual(["completed", "completed", "completed"]);
  });

  it("records how the user answered", () => {
    const answered = transcript.entries.filter((e) => e.kind === "approval");
    expect(answered.every((e) => e.kind === "approval" && e.resolution === "allowed")).toBe(true);
    const question = transcript.entries.find((e) => e.kind === "question");
    expect(question).toMatchObject({ resolution: "answered", answers: [["no"]] });
  });
});

describe("a recorded planned task", () => {
  const transcript = buildTranscript(items(plan));

  it("shows the spec, the plan approval, the plan, the steps, the sub-agent and the report", () => {
    expect(shape(transcript.entries)).toEqual([
      "user",
      "structured",
      "approval",
      "plan",
      "assistant",
      "tool:write_file:ok",
      "tool:finish_step:ok",
      "assistant",
      "agent",
      "tool:finish_step:ok",
      "assistant",
      "turn",
    ]);
  });

  it("keeps one plan card with every step done", () => {
    const card = transcript.entries.find((e) => e.kind === "plan");
    expect(card).toMatchObject({ goal: "Add a greeting module", steps: [{ status: "done" }, { status: "done" }] });
  });

  it("puts the sub-agent's work inside its card", () => {
    const agent = transcript.entries.find((e) => e.kind === "agent");
    expect(agent).toMatchObject({ agent: "a1", role: "explore", status: "ok" });
    if (agent?.kind !== "agent") throw new Error("no agent card");
    expect(shape(agent.entries)).toEqual(["tool:list_dir:ok", "assistant"]);
    expect(agent.task).toContain("greet.py");
  });

  it("carries the report's manual checks", () => {
    const turn = transcript.entries.find((e) => e.kind === "turn");
    expect(turn).toMatchObject({ ok: true, manualChecks: [expect.stringContaining("greet")] });
  });
});
