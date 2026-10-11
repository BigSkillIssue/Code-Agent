import type { SlashCommand } from "../lib/completion";

// The shapes the server sends. They mirror forge_web (ProjectOut, ChatOut) and the chat items
// described in docs/PROTOCOL.md.

export type Role = "owner" | "editor" | "viewer";
export type ChatMode = "ask" | "edits" | "auto";
export type ChatState = "idle" | "running" | "waiting" | "stopped";

export interface User {
  id: string;
  email: string | null;
  name: string;
  role: "admin" | "member";
  apple_apps?: boolean; // may build Apple apps on this server
}

export interface Project {
  id: string;
  name: string;
  role: Role;
  source: string;
  kind?: "code" | "apple" | "app"; // apple: an Apple app (builds on the server's Macs); app: a full-stack product
  created_at: number;
  updated_at: number;
}

export interface Chat {
  id: string;
  project_id: string;
  title: string;
  state: ChatState;
  mode: ChatMode;
  model: string;
  shared: boolean;
  mine: boolean;
  created_at: number;
  updated_at: number;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
}

export interface ToolResult {
  call_id: string;
  ok: boolean;
  text: string;
  code?: string | null;
}

export interface Question {
  text: string;
  kind: "choice" | "multi" | "text" | "confirm";
  options: string[];
  default: string | null;
  why: string;
}

export interface Usage {
  input_tokens: number;
  output_tokens: number;
  cached_tokens: number;
  cost_usd: number;
}

export interface PlanStep {
  id: string;
  title: string;
  status: string;
  detail?: string;
}

export interface Todo {
  content: string;
  status: "pending" | "in_progress" | "completed";
  active_form?: string;
}

export interface ForgeEvent {
  kind: string;
  agent_id: string;
  ts: number;
  [field: string]: unknown;
}

export type ChatItem =
  | { type: "user"; text: string }
  | { type: "ready"; session_id: string; commands?: SlashCommand[] }
  | { type: "status"; state: "running" | "idle" }
  | { type: "event"; event: ForgeEvent }
  | {
      type: "request";
      id: string;
      kind: "approval" | "question";
      payload: { call?: ToolCall; reason?: string; questions?: Question[] };
      purpose?: "apple_approval"; // Forge's "Is the app ready for Apple?"
    }
  | { type: "request_resolved"; id: string; answer?: Record<string, unknown>; cancelled?: boolean }
  | { type: "command_result"; command: string; text: string }
  | {
      type: "turn";
      prompt: string;
      ok: boolean;
      summary: string;
      report: string;
      files_changed: string[];
      manual_checks: string[];
      assumptions?: string[];
      usage: Usage;
      seconds: number;
      cancelled: boolean;
      error: string;
    }
  | { type: "error"; message: string }
  | { type: "worker_exited"; exit_code: number | null }
  | { type: "oversized"; original_type: string; event_kind: string };

export interface StoredItem {
  seq: number;
  item: ChatItem;
}

export interface LiveState {
  state?: ChatState;
  streaming: Record<string, string>;
  outputs: Record<string, string[]>;
  pending: ChatItem[];
}

export type ServerMessage =
  | { type: "hello"; user: { id: string; name: string } }
  | { type: "item"; chat_id: string; seq: number; item: ChatItem }
  | { type: "live"; chat_id: string; item: ChatItem }
  | { type: "subscribed"; chat_id: string; last_seq: number; live: LiveState }
  | { type: "chat_state"; chat_id: string; state: ChatState; title: string }
  | { type: "error"; chat_id?: string; message: string }
  | { type: "pong" };

export interface Model {
  id: string; // "anthropic/claude-sonnet-5-5"
  provider: string;
  model: string;
  key: "own" | "server";
  cost_in: number;
  cost_out: number;
  context_window: number;
}
