// Turns a chat's stored items (and the live state of a running turn) into what the chat view
// shows: messages, tool cards, approvals, questions, the plan, turn reports. Pure functions, so
// the same items always give the same transcript.

import type { GuidelineReview } from "../api/apple";
import type {
  ChatItem,
  ChatState,
  LiveState,
  PlanStep,
  Question,
  StoredItem,
  Todo,
  ToolCall,
  ToolResult,
  Usage,
} from "../api/types";

export type Entry =
  | { kind: "user"; key: string; text: string }
  | { kind: "assistant"; key: string; agent: string; text: string; streaming: boolean }
  | { kind: "structured"; key: string; agent: string; data: Record<string, unknown> }
  | {
      kind: "tool";
      key: string;
      agent: string;
      call: ToolCall;
      status: "running" | "ok" | "failed";
      result?: ToolResult;
      output: string[];
    }
  | {
      kind: "approval";
      key: string;
      id: string;
      call: ToolCall;
      reason: string;
      resolution?: "allowed" | "denied" | "cancelled";
    }
  | {
      kind: "question";
      key: string;
      id: string;
      questions: Question[];
      resolution?: "answered" | "dismissed" | "cancelled";
      answers?: string[][];
      appleApproval?: boolean; // Forge's final question in an Apple app's chat
    }
  | { kind: "guideline"; key: string; review: GuidelineReview }
  | { kind: "plan"; key: string; goal: string; steps: PlanStep[] }
  | {
      kind: "agent";
      key: string;
      agent: string; // the sub-agent's id ("" until its first event)
      role: string;
      task: string;
      status: "running" | "ok" | "failed";
      summary: string;
      entries: Entry[];
    }
  | {
      kind: "turn";
      key: string;
      ok: boolean;
      summary: string;
      report: string;
      filesChanged: string[];
      manualChecks: string[];
      assumptions: string[];
      usage: Usage;
      seconds: number;
      cancelled: boolean;
      error: string;
    }
  | { kind: "notice"; key: string; tone: "info" | "error"; text: string };

export interface Transcript {
  entries: Entry[];
  todos: Todo[];
  pending: string[]; // ids of requests still waiting for an answer
}

interface Builder {
  entries: Entry[];
  tools: Map<string, number>; // tool call id -> entry index
  requests: Map<string, number>; // request id -> entry index
  plan: number | null;
  todos: Todo[];
  hidden: Set<string>; // calls shown elsewhere (todo list, plan, question cards)
  agents: Map<string, { index: number; builder: Builder }>; // sub-agent id -> its card
  spawns: number[]; // sub-agent cards still waiting for their agent's first event
  nested: boolean;
}

// Tools whose effect has its own card: the todo list, the plan, the question.
const SHOWN_ELSEWHERE = new Set(["todo_write", "submit_plan", "update_plan", "ask_user"]);
const AGENT_IN_RESULT = /\bagent (\w[\w-]*) \(/;

const newBuilder = (nested = false): Builder => ({
  entries: [],
  tools: new Map(),
  requests: new Map(),
  plan: null,
  todos: [],
  hidden: new Set(),
  agents: new Map(),
  spawns: [],
  nested,
});

export function messageText(message: unknown): string {
  const parts = (message as { parts?: { type?: string; text?: string }[] })?.parts ?? [];
  return parts
    .filter((part) => part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("");
}

/** The final reviewer's verdict: its summary and checks are the turn's report. */
export function isReviewVerdict(data: Record<string, unknown>): boolean {
  return typeof data.ok === "boolean" && typeof data.summary === "string" && !("goal" in data);
}

/** A reply that is one JSON object (the task spec, a review) is shown as data, not as text. */
export function structuredReply(text: string): Record<string, unknown> | null {
  const trimmed = text.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return null;
  try {
    const value: unknown = JSON.parse(trimmed);
    return value && typeof value === "object" && !Array.isArray(value)
      ? (value as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

/** The builder of a sub-agent's card: the oldest card still waiting for its agent, or a new one. */
function agentBuilder(b: Builder, agent: string, key: string): Builder {
  const known = b.agents.get(agent);
  if (known) return known.builder;
  let index = b.spawns.shift();
  if (index === undefined) {
    index = b.entries.length;
    b.entries.push({ kind: "agent", key, agent, role: "", task: "", status: "running", summary: "", entries: [] });
  } else {
    b.entries[index] = { ...(b.entries[index] as Extract<Entry, { kind: "agent" }>), agent };
  }
  const builder = newBuilder(true);
  b.agents.set(agent, { index, builder });
  return builder;
}

function startTool(b: Builder, key: string, agent: string, call: ToolCall): void {
  if (SHOWN_ELSEWHERE.has(call.name)) {
    b.hidden.add(call.id);
    return;
  }
  b.tools.set(call.id, b.entries.length);
  if (call.name === "spawn_agent" && !b.nested) {
    b.spawns.push(b.entries.length);
    const args = call.arguments as { role?: unknown; task?: unknown };
    b.entries.push({
      kind: "agent", key, agent: "", role: String(args.role ?? ""), task: String(args.task ?? ""),
      status: "running", summary: "", entries: [],
    });
    return;
  }
  b.entries.push({ kind: "tool", key, agent, call, status: "running", output: [] });
}

function finishTool(b: Builder, key: string, agent: string, result: ToolResult): void {
  const status = result.ok ? "ok" : "failed";
  const index = b.tools.get(result.call_id);
  if (index === undefined && b.hidden.has(result.call_id)) {
    if (!result.ok) b.entries.push({ kind: "notice", key, tone: "error", text: result.text });
    return;
  }
  if (index === undefined) {
    const call = { id: result.call_id, name: "tool", arguments: {} };
    b.entries.push({ kind: "tool", key, agent, call, status, result, output: [] });
    return;
  }
  const entry = b.entries[index];
  if (entry.kind === "agent") {
    const named = AGENT_IN_RESULT.exec(result.text)?.[1];
    if (!entry.agent && named && !b.agents.has(named)) {
      b.spawns = b.spawns.filter((i) => i !== index);
      b.agents.set(named, { index, builder: newBuilder(true) });
    }
    b.entries[index] = { ...entry, agent: entry.agent || named || "", status, summary: result.text };
  } else if (entry.kind === "tool") {
    b.entries[index] = { ...entry, status, result };
  }
}

function addEvent(b: Builder, seq: number, event: Record<string, unknown>): void {
  const agent = String(event.agent_id ?? "main");
  const key = `e${seq}`;
  if (event.kind === "guideline_review") {
    // The reviewer runs as an agent of its own, but its verdict belongs in the main transcript;
    // it also ends the reviewer's card (nobody spawned it, so no spawn result will).
    const card = b.agents.get(agent);
    const entry = card ? b.entries[card.index] : undefined;
    if (card && entry?.kind === "agent") b.entries[card.index] = { ...entry, role: entry.role || "apple_reviewer", status: "ok" };
    b.entries.push({ kind: "guideline", key, review: event as unknown as GuidelineReview });
    return;
  }
  if (agent !== "main" && !b.nested && event.kind !== "agent_finished") {
    addEvent(agentBuilder(b, agent, key), seq, event);
    return;
  }
  switch (event.kind) {
    case "model_done": {
      const text = messageText(event.message);
      if (!text.trim()) return;
      const data = structuredReply(text);
      if (data && isReviewVerdict(data)) return; // the turn's report card shows it
      b.entries.push(
        data
          ? { kind: "structured", key, agent, data }
          : { kind: "assistant", key, agent, text, streaming: false },
      );
      return;
    }
    case "tool_started":
      startTool(b, key, agent, event.call as ToolCall);
      return;
    case "tool_finished":
      finishTool(b, key, agent, event.result as ToolResult);
      return;
    case "plan_updated": {
      const plan = event.plan as { spec?: { goal?: string }; steps?: PlanStep[] };
      const entry: Entry = {
        kind: "plan",
        key: b.plan === null ? key : b.entries[b.plan].key,
        goal: plan.spec?.goal ?? "",
        steps: plan.steps ?? [],
      };
      if (b.plan === null) {
        b.plan = b.entries.length;
        b.entries.push(entry);
      } else {
        b.entries[b.plan] = entry;
      }
      return;
    }
    case "todos_updated":
      if (agent === "main") b.todos = (event.todos as Todo[]) ?? [];
      return;
    case "error":
      b.entries.push({ kind: "notice", key, tone: "error", text: String(event.message ?? "") });
      return;
    case "compacted":
      b.entries.push({ kind: "notice", key, tone: "info", text: "The conversation was compacted." });
      return;
    case "agent_finished": {
      const card = b.agents.get(agent);
      const entry = card ? b.entries[card.index] : undefined;
      if (entry?.kind === "agent") {
        const status = event.status === "done" ? "ok" : "failed";
        b.entries[card!.index] = { ...entry, role: entry.role || String(event.role ?? ""), status };
      }
      return;
    }
    default:
      return;
  }
}

function addItem(b: Builder, seq: number, item: ChatItem): void {
  const key = `i${seq}`;
  switch (item.type) {
    case "user":
      b.entries.push({ kind: "user", key, text: item.text });
      return;
    case "event":
      addEvent(b, seq, item.event as Record<string, unknown>);
      return;
    case "request":
      b.requests.set(item.id, b.entries.length);
      if (item.kind === "approval" && item.payload.call) {
        b.entries.push({
          kind: "approval",
          key,
          id: item.id,
          call: item.payload.call,
          reason: item.payload.reason ?? "",
        });
      } else {
        const appleApproval = item.purpose === "apple_approval";
        b.entries.push({ kind: "question", key, id: item.id, questions: item.payload.questions ?? [], appleApproval });
      }
      return;
    case "request_resolved":
      resolve(b, item);
      return;
    case "command_result":
      b.entries.push({ kind: "notice", key, tone: "info", text: item.text });
      return;
    case "turn":
      b.entries.push({
        kind: "turn",
        key,
        ok: item.ok,
        summary: repeatsLastReply(b, item.summary) ? "" : item.summary,
        report: item.report ?? "",
        filesChanged: item.files_changed ?? [],
        manualChecks: item.manual_checks ?? [],
        assumptions: item.assumptions ?? [],
        usage: item.usage,
        seconds: item.seconds,
        cancelled: item.cancelled,
        error: item.error,
      });
      return;
    case "error":
      b.entries.push({ kind: "notice", key, tone: "error", text: item.message });
      return;
    case "worker_exited":
      if (item.exit_code) {
        const text = `The chat's worker stopped (exit code ${item.exit_code}); sending again restarts it.`;
        b.entries.push({ kind: "notice", key, tone: "error", text });
      }
      return;
    case "oversized":
      b.entries.push({ kind: "notice", key, tone: "info", text: "An item was too large to show." });
      return;
    default:
      return;
  }
}

/** A simple task's summary is its only reply: no need to show it twice. */
function repeatsLastReply(b: Builder, summary: string): boolean {
  const last = [...b.entries].reverse().find((e) => e.kind === "assistant");
  return last?.kind === "assistant" && last.text.trim() === summary.trim();
}

function resolve(b: Builder, item: Extract<ChatItem, { type: "request_resolved" }>): void {
  const index = b.requests.get(item.id);
  if (index === undefined) return;
  const entry = b.entries[index];
  const answer = item.answer ?? {};
  if (entry.kind === "approval") {
    const resolution = item.cancelled ? "cancelled" : answer.allow === true ? "allowed" : "denied";
    b.entries[index] = { ...entry, resolution };
  } else if (entry.kind === "question") {
    const answers = Array.isArray(answer.answers)
      ? (answer.answers as { values?: string[] }[]).map((a) => a.values ?? [])
      : undefined;
    const resolution = item.cancelled ? "cancelled" : answer.dismissed ? "dismissed" : "answered";
    b.entries[index] = { ...entry, resolution, answers };
  }
}

/** Put each sub-agent's own entries into its card. */
function attachAgents(b: Builder): void {
  for (const { index, builder } of b.agents.values()) {
    const entry = b.entries[index];
    // Inside its card a sub-agent's entries need no agent label of their own.
    const entries = builder.entries.map((e) => ("agent" in e ? { ...e, agent: "main" } : e));
    if (entry.kind === "agent") b.entries[index] = { ...entry, entries };
  }
}

export function buildTranscript(items: StoredItem[], live?: LiveState): Transcript {
  const b = newBuilder();
  for (const { seq, item } of items) addItem(b, seq, item);
  attachAgents(b);
  if (live) {
    for (const [callId, lines] of Object.entries(live.outputs)) {
      const index = b.tools.get(callId);
      const entry = index === undefined ? undefined : b.entries[index];
      if (entry?.kind === "tool" && entry.status === "running") {
        b.entries[index!] = { ...entry, output: lines };
      }
    }
    for (const [agent, text] of Object.entries(live.streaming)) {
      if (text.trim() && !structuredReply(text) && !text.trimStart().startsWith("{")) {
        b.entries.push({ kind: "assistant", key: `live-${agent}`, agent, text, streaming: true });
      }
    }
  }
  const pending = [...b.requests.entries()]
    .filter(([, index]) => {
      const entry = b.entries[index];
      return (entry.kind === "approval" || entry.kind === "question") && !entry.resolution;
    })
    .map(([id]) => id);
  return { entries: b.entries, todos: b.todos, pending };
}

export function chatStateLabel(state: ChatState): string {
  return { idle: "idle", running: "working", waiting: "waiting for you", stopped: "stopped" }[state];
}
