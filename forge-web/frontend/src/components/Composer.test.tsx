import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { BUILTIN_COMMANDS } from "../lib/completion";
import { Composer } from "./Composer";

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function setup(searchFiles = vi.fn().mockResolvedValue(["src/greet.py", "greet_test.py"])) {
  const onSend = vi.fn().mockResolvedValue(undefined);
  render(
    <Composer running={false} onSend={onSend} onStop={vi.fn()} commands={BUILTIN_COMMANDS} searchFiles={searchFiles} />,
  );
  const box = screen.getByRole("textbox") as HTMLTextAreaElement;
  const type = (value: string) => fireEvent.change(box, { target: { value } });
  return { box, type, onSend, searchFiles };
}

describe("Composer", () => {
  it("completes a slash command with the keyboard", () => {
    const { box, type, onSend } = setup();
    type("/co");
    expect(screen.getAllByRole("option").map((o) => o.textContent)).toEqual([
      expect.stringContaining("/compact"),
      expect.stringContaining("/context"),
    ]);
    fireEvent.keyDown(box, { key: "ArrowDown" });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(box.value).toBe("/context ");
    expect(onSend).not.toHaveBeenCalled(); // Enter took the suggestion, it did not send
    expect(screen.queryByRole("listbox")).toBeNull();
  });

  it("offers project files after @ and fills one in with Tab", async () => {
    vi.useFakeTimers();
    const { box, type, searchFiles } = setup();
    type("look at @gre");
    await act(async () => {
      await vi.advanceTimersByTimeAsync(200);
    });
    expect(searchFiles).toHaveBeenCalledWith("gre");
    expect(screen.getAllByRole("option")).toHaveLength(2);
    fireEvent.keyDown(box, { key: "Tab" });
    expect(box.value).toBe("look at @src/greet.py ");
  });

  it("closes the list with Escape and then sends with Enter", async () => {
    const { box, type, onSend } = setup();
    type("/he");
    expect(screen.getByRole("listbox")).toBeTruthy();
    fireEvent.keyDown(box, { key: "Escape" });
    expect(screen.queryByRole("listbox")).toBeNull();
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(onSend).toHaveBeenCalledWith("/he");
  });
});
