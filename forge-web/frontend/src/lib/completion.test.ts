import { describe, expect, it } from "vitest";
import { applyCompletion, BUILTIN_COMMANDS, completionAt, matchCommands } from "./completion";

describe("completionAt", () => {
  it("completes a command only at the start, before the first space", () => {
    expect(completionAt("/co", 3)).toEqual({ kind: "command", query: "co", start: 0, end: 3 });
    expect(completionAt("/compact hard", 13)).toBeNull();
    expect(completionAt("say /co", 7)).toBeNull();
  });

  it("completes a file after @ anywhere", () => {
    expect(completionAt("look at @src/ma", 15)).toEqual({ kind: "file", query: "src/ma", start: 8, end: 15 });
    expect(completionAt("@", 1)).toEqual({ kind: "file", query: "", start: 0, end: 1 });
    expect(completionAt("mail@example", 12)).toBeNull(); // not after a space
  });
});

describe("applyCompletion", () => {
  it("fills in a command and a file and moves the caret behind them", () => {
    expect(applyCompletion("/co", { kind: "command", query: "co", start: 0, end: 3 }, "/compact")).toEqual({
      text: "/compact ",
      caret: 9,
    });
    const file = completionAt("fix @ma please", 7)!;
    expect(applyCompletion("fix @ma please", file, "src/main.py")).toEqual({ text: "fix @src/main.py  please", caret: 17 });
    const spaced = completionAt("@ha", 3)!;
    expect(applyCompletion("@ha", spaced, "has space.txt").text).toBe('@"has space.txt" ');
  });
});

describe("matchCommands", () => {
  it("finds commands by their start", () => {
    expect(matchCommands(BUILTIN_COMMANDS, "co").map((c) => c.name)).toEqual(["/compact", "/context"]);
    expect(matchCommands(BUILTIN_COMMANDS, "").length).toBe(8);
  });
});
