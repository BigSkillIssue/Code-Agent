// Turns a chat's stored items (and the live state of a running turn) into what the chat view
// shows: messages, tool cards, approvals, questions, the plan, turn reports. Pure functions, so
// the same items always give the same transcript.

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
    }
  | { kind: "plan"; key: string; goal: string; steps: PlanStep[] }
  | {
      kind: "turn";
      key: string;
      ok: boolean;
      summary: string;
      filesChanged: string[];
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
}

export function messageText(message: unknown): string {
  const parts = (message as { parts?: { type?: string; text?: string }[] })?.parts ?? [];
  return parts
    .filter((part) => part.type === "text" && typeof part.text === "string")
    .map((part) => part.text)
    .join("");
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

function addEvent(b: Builder, seq: number, event: Record<string, unknown>): void {
  const agent = String(event.agent_id ?? "main");
  const key = `e${seq}`;
  switch (event.kind) {
    case "model_done": {
      const text = messageText(event.message);
      if (!text.trim()) return;
      const data = structuredReply(text);
      b.entries.push(
        data
          ? { kind: "structured", key, agent, data }
          : { kind: "assistant", key, agent, text, streaming: false },
      );
      return;
    }
    case "tool_started": {
      const call = event.call as ToolCall;
      b.tools.set(call.id, b.entries.length);
      b.entries.push({ kind: "tool", key, agent, call, status: "running", output: [] });
      return;
    }
    case "tool_finished": {
      const result = event.result as ToolResult;
      const index = b.tools.get(result.call_id);
      const status = result.ok ? "ok" : "failed";
      if (index === undefined) {
        const call = { id: result.call_id, name: "tool", arguments: {} };
        b.entries.push({ kind: "tool", key, agent, call, status, result, output: [] });
      } else {
        const entry = b.entries[index] as Extract<Entry, { kind: "tool" }>;
        b.entries[index] = { ...entry, status, result };
      }
      return;
    }
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
    case "agent_finished":
      b.entries.push({
        kind: "notice",
        key,
        tone: "info",
        text: `Sub-agent ${agent} (${String(event.role)}) finished: ${String(event.status)}`,
      });
      return;
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
        b.entries.push({ kind: "question", key, id: item.id, questions: item.payload.questions ?? [] });
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
        filesChanged: item.files_changed ?? [],
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

export function buildTranscript(items: StoredItem[], live?: LiveState): Transcript {
  const b: Builder = { entries: [], tools: new Map(), requests: new Map(), plan: null, todos: [] };
  for (const { seq, item } of items) addItem(b, seq, item);
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
