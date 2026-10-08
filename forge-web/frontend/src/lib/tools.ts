// How a tool call is summed up in one line.

import type { ToolCall } from "../api/types";

const MAIN_ARGUMENT: Record<string, string> = {
  read_file: "path",
  write_file: "path",
  edit_file: "path",
  list_dir: "path",
  apply_patch: "path",
  bash: "command",
  powershell: "command",
  monitor: "command",
  grep: "pattern",
  glob: "pattern",
  web_fetch: "url",
  web_search: "query",
  research: "question",
  spawn_agent: "task",
  browser_open: "url",
  remember: "note",
};

const LABEL: Record<string, string> = {
  read_file: "Read",
  write_file: "Write",
  edit_file: "Edit",
  apply_patch: "Patch",
  list_dir: "List",
  bash: "Run",
  powershell: "Run",
  grep: "Search",
  glob: "Find",
  web_fetch: "Fetch",
  web_search: "Web search",
  research: "Research",
  spawn_agent: "Sub-agent",
  todo_write: "Todos",
  submit_plan: "Plan",
  update_plan: "Plan",
  finish_step: "Finish step",
  ask_user: "Question",
};

export function toolLabel(call: ToolCall): string {
  return LABEL[call.name] ?? call.name;
}

export function toolSummary(call: ToolCall): string {
  const key = MAIN_ARGUMENT[call.name];
  const value = key ? call.arguments[key] : Object.values(call.arguments).find((v) => typeof v === "string");
  const text = typeof value === "string" ? value : "";
  const firstLine = text.split("\n")[0];
  return firstLine.length > 160 ? `${firstLine.slice(0, 157)}…` : firstLine;
}

export function isEdit(call: ToolCall): boolean {
  return call.name === "edit_file" || call.name === "write_file";
}
