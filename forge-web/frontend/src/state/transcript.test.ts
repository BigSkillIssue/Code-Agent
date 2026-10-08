import { describe, expect, it } from "vitest";
import type { ChatItem, StoredItem } from "../api/types";
import { applyMessage } from "./store";
import { buildTranscript, structuredReply } from "./transcript";

const event = (kind: string, fields: Record<string, unknown> = {}): ChatItem => ({
  type: "event",
  event: { kind, agent_id: "main", ts: 0, session_id: "s", ...fields },
});
const numbered = (items: ChatItem[]): StoredItem[] => items.map((item, i) => ({ seq: i + 1, item }));
const message = (text: string) => ({ role: "assistant", parts: [{ type: "text", text }], tool_calls: [] });
const call = { id: "c1", name: "write_file", arguments: { path: "a.txt", content: "A\n" } };

describe("buildTranscript", () => {
  it("shows the user, the assistant and the turn", () => {
    const transcript = buildTranscript(
      numbered([
        { type: "user", text: "Say hello" },
        event("model_done", { message: message("Hello **there**") }),
        {
          type: "turn", prompt: "Say hello", ok: true, summary: "Greeted.", report: "", files_changed: [],
          manual_checks: [], usage: { input_tokens: 1, output_tokens: 2, cached_tokens: 0, cost_usd: 0 },
          seconds: 1, cancelled: false, error: "",
        },
      ]),
    );
    expect(transcript.entries.map((e) => e.kind)).toEqual(["user", "assistant", "turn"]);
    expect(transcript.entries[2]).toMatchObject({ summary: "Greeted." });
  });

  it("does not repeat a reply as the turn's summary", () => {
    const usage = { input_tokens: 0, output_tokens: 0, cached_tokens: 0, cost_usd: 0 };
    const transcript = buildTranscript(
      numbered([
        event("model_done", { message: message("Hi there.") }),
        {
          type: "turn", prompt: "hi", ok: true, summary: "Hi there.", report: "", files_changed: [],
          manual_checks: [], usage, seconds: 0, cancelled: false, error: "",
        },
      ]),
    );
    expect(transcript.entries[1]).toMatchObject({ kind: "turn", summary: "" });
  });

  it("folds a JSON reply (the task spec) into a structured card", () => {
    const spec = JSON.stringify({ goal: "greet", acceptance_criteria: ["greeted"] });
    const transcript = buildTranscript(numbered([event("model_done", { message: message(spec) })]));
    expect(transcript.entries[0]).toMatchObject({ kind: "structured", data: { goal: "greet" } });
    expect(structuredReply("not json")).toBeNull();
  });

  it("joins a tool's start, live output and result into one card", () => {
    const items = numbered([event("tool_started", { call: { id: "b1", name: "bash", arguments: { command: "ls" } } })]);
    const running = buildTranscript(items, { streaming: {}, outputs: { b1: ["one", "two"] }, pending: [] });
    expect(running.entries[0]).toMatchObject({ kind: "tool", status: "running", output: ["one", "two"] });
    const done = buildTranscript([
      ...items,
      { seq: 2, item: event("tool_finished", { result: { call_id: "b1", ok: true, text: "one\ntwo" } }) },
    ]);
    expect(done.entries).toHaveLength(1);
    expect(done.entries[0]).toMatchObject({ kind: "tool", status: "ok" });
  });

  it("tracks approvals until they are answered", () => {
    const request: ChatItem = { type: "request", id: "r1", kind: "approval", payload: { call, reason: "needs approval" } };
    const open = buildTranscript(numbered([request]));
    expect(open.pending).toEqual(["r1"]);
    const answered = buildTranscript(
      numbered([request, { type: "request_resolved", id: "r1", answer: { allow: false } }]),
    );
    expect(answered.pending).toEqual([]);
    expect(answered.entries[0]).toMatchObject({ kind: "approval", resolution: "denied" });
  });

  it("keeps one plan card and updates it in place", () => {
    const plan = (status: string) => ({ spec: { goal: "g" }, steps: [{ id: "s1", title: "Step", status }] });
    const transcript = buildTranscript(
      numbered([event("plan_updated", { plan: plan("todo") }), { type: "user", text: "x" }, event("plan_updated", { plan: plan("done") })]),
    );
    expect(transcript.entries.map((e) => e.kind)).toEqual(["plan", "user"]);
    expect(transcript.entries[0]).toMatchObject({ steps: [{ status: "done" }] });
  });

  it("shows streaming text at the end, but not a JSON reply in progress", () => {
    const live = { streaming: { main: "Hel", lead: '{"goal": ' }, outputs: {}, pending: [] };
    const transcript = buildTranscript([], live);
    expect(transcript.entries).toEqual([
      { kind: "assistant", key: "live-main", agent: "main", text: "Hel", streaming: true },
    ]);
  });
});

describe("applyMessage", () => {
  const empty = { items: [], lastSeq: 0, live: { streaming: {}, outputs: {}, pending: [] }, subscribed: false };

  it("takes each numbered item once, in order", () => {
    let data = applyMessage(empty, { type: "item", chat_id: "c", seq: 1, item: { type: "user", text: "a" } });
    data = applyMessage(data, { type: "item", chat_id: "c", seq: 1, item: { type: "user", text: "a" } });
    data = applyMessage(data, { type: "item", chat_id: "c", seq: 2, item: { type: "user", text: "b" } });
    expect(data.items.map((i) => i.seq)).toEqual([1, 2]);
  });

  it("streams text and clears it when the message is stored", () => {
    let data = applyMessage(empty, { type: "live", chat_id: "c", item: event("model_delta", { text: "Hi" }) });
    data = applyMessage(data, { type: "live", chat_id: "c", item: event("model_delta", { text: "!" }) });
    expect(data.live.streaming.main).toBe("Hi!");
    data = applyMessage(data, { type: "item", chat_id: "c", seq: 1, item: event("model_done", { message: message("Hi!") }) });
    expect(data.live.streaming.main).toBeUndefined();
  });
});
