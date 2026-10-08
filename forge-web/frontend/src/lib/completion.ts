// Completing what is typed in the composer: `/command` at the start, `@file` anywhere.

export interface SlashCommand {
  name: string; // "/compact"
  usage: string; // "/compact [hard]"
  help: string;
  custom: boolean;
}

export interface Completion {
  kind: "command" | "file";
  query: string;
  start: number; // where the replaced text starts (the "/" or "@")
  end: number; // the caret
}

// Forge's built-in commands, until the chat's worker has told us its own list.
export const BUILTIN_COMMANDS: SlashCommand[] = [
  ["/plan", "show the plan with step statuses"],
  ["/go", "continue the plan from its first unfinished step"],
  ["/compact [hard]", "summarize the context now (hard: reset it)"],
  ["/context", "tokens used by the last request, by category"],
  ["/undo", "roll back the last step or file change"],
  ["/mode [MODE]", "show or set the sandbox mode or approval policy"],
  ["/tasks [stop] [ID]", "background jobs, agents and monitors; show or stop one"],
  ["/jobs", "background jobs and their status"],
  ["/agents", "the session's agents with status and usage"],
  ["/init", "write a FORGE.md with this project's commands and layout"],
  ["/help", "this list"],
].map(([usage, help]) => ({ name: usage.split(" ")[0], usage, help, custom: false }));

/** What the caret is completing, if anything. */
export function completionAt(text: string, caret: number): Completion | null {
  const before = text.slice(0, caret);
  const command = /^\/([a-z0-9-]*)$/.exec(before);
  if (command) return { kind: "command", query: command[1], start: 0, end: caret };
  const mention = /(?:^|\s)@([^\s@"]*)$/.exec(before);
  if (mention) return { kind: "file", query: mention[1], start: caret - mention[1].length - 1, end: caret };
  return null;
}

/** The text with the completion filled in, and where the caret goes. */
export function applyCompletion(text: string, completion: Completion, value: string): { text: string; caret: number } {
  const insert =
    completion.kind === "command" ? `${value} ` : /\s/.test(value) ? `@"${value}" ` : `@${value} `;
  return {
    text: text.slice(0, completion.start) + insert + text.slice(completion.end),
    caret: completion.start + insert.length,
  };
}

/** Commands whose name starts with the typed text, at most `limit`. */
export function matchCommands(commands: SlashCommand[], query: string, limit = 8): SlashCommand[] {
  return commands.filter((c) => c.name.startsWith(`/${query}`)).slice(0, limit);
}
