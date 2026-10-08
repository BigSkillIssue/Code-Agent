import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Entry } from "../state/transcript";
import { Transcript } from "./Transcript";

afterEach(cleanup);

const call = { id: "c1", name: "bash", arguments: { command: "rm -rf build" } };

describe("Transcript", () => {
  it("renders model text without running any HTML in it", () => {
    const entries: Entry[] = [
      {
        kind: "assistant",
        key: "a",
        agent: "main",
        streaming: false,
        text: 'Hi <img src=x onerror="window.hacked=1"> <script>window.hacked=2</script> [link](javascript:alert(1))',
      },
    ];
    const { container } = render(<Transcript entries={entries} />);
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    const link = container.querySelector("a");
    expect(link?.getAttribute("href") ?? "").not.toContain("javascript:");
    expect((window as unknown as { hacked?: number }).hacked).toBeUndefined();
  });

  it("sends the right answer from an approval card", () => {
    const onAnswer = vi.fn().mockResolvedValue(true);
    const entries: Entry[] = [{ kind: "approval", key: "r", id: "r1", call, reason: "no OS sandbox" }];
    render(<Transcript entries={entries} onAnswer={onAnswer} />);
    fireEvent.change(screen.getByPlaceholderText(/instead|stattdessen/), { target: { value: "keep build" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Deny|Ablehnen)$/ }));
    expect(onAnswer).toHaveBeenCalledWith("r1", { allow: false, remember: false, feedback: "keep build" });
  });

  it("shows an answered approval without buttons", () => {
    const entries: Entry[] = [{ kind: "approval", key: "r", id: "r1", call, reason: "", resolution: "allowed" }];
    render(<Transcript entries={entries} onAnswer={vi.fn()} />);
    expect(screen.queryByRole("button", { name: /^(Allow|Erlauben)$/ })).toBeNull();
    expect(screen.getByText(/Allowed|Erlaubt/)).toBeTruthy();
  });

  it("answers a question with the chosen option", () => {
    const onAnswer = vi.fn().mockResolvedValue(true);
    const entries: Entry[] = [
      {
        kind: "question",
        key: "q",
        id: "r2",
        questions: [{ text: "Which color?", kind: "choice", options: ["red", "blue"], default: null, why: "" }],
      },
    ];
    render(<Transcript entries={entries} onAnswer={onAnswer} />);
    fireEvent.click(screen.getByRole("button", { name: "blue" }));
    fireEvent.click(screen.getByRole("button", { name: /^(Answer|Antworten)$/ }));
    expect(onAnswer).toHaveBeenCalledWith("r2", { answers: [{ question_index: 0, values: ["blue"] }] });
  });
});
