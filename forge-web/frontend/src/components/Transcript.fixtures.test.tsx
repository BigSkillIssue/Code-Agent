// The chat view's cards for chats recorded from a real server, and a hostile chat.

import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { StoredItem } from "../api/types";
import plan from "../fixtures/chat-plan.json";
import tools from "../fixtures/chat-tools.json";
import xss from "../fixtures/xss.json";
import { buildTranscript } from "../state/transcript";
import { TodoPanel } from "./TodoPanel";
import { Transcript } from "./Transcript";

afterEach(cleanup);

const transcriptOf = (fixture: { items: unknown[] }, cut?: number) =>
  buildTranscript((fixture.items as StoredItem[]).slice(0, cut));

describe("recorded chats", () => {
  it("shows a planned task with its plan, a folded sub-agent and the review", () => {
    render(<Transcript entries={transcriptOf(plan).entries} />);
    expect(screen.getAllByText("Write greet.py")).toHaveLength(2); // the approved plan and the plan card
    const agent = screen.getByTestId("agent");
    expect(within(agent).queryByText("greet.py is there.")).toBeNull(); // folded
    fireEvent.click(within(agent).getByRole("button"));
    expect(within(agent).getByText("greet.py is there.")).toBeTruthy();
    expect(screen.getByTestId("turn").textContent).toContain("greet.py greets by name");
  });

  it("asks to approve the plan and sends the answer", () => {
    const items = plan.items as StoredItem[];
    const until = items.findIndex((e) => e.item.type === "request_resolved");
    const onAnswer = vi.fn().mockResolvedValue(true);
    render(<Transcript entries={transcriptOf(plan, until).entries} onAnswer={onAnswer} />);
    const card = screen.getByTestId("approval");
    expect(within(card).getByText(/^Check the module/)).toBeTruthy();
    expect(within(card).getByText("Write it, then check it.")).toBeTruthy();
    fireEvent.click(within(card).getByRole("button", { name: /approve plan|plan freigeben/i }));
    expect(onAnswer).toHaveBeenCalledWith(expect.stringMatching(/^r/), { allow: true, remember: false, feedback: "" });
  });

  it("shows the todo list of a small task", () => {
    const todos = transcriptOf(tools).todos;
    render(<TodoPanel todos={todos} />);
    expect(screen.getByTestId("todos").textContent).toContain("3/3");
  });
});

describe("a hostile chat", () => {
  it("renders every field as text, never as markup or a script", () => {
    const transcript = transcriptOf(xss);
    const { container } = render(
      <>
        <Transcript entries={transcript.entries} onAnswer={vi.fn()} />
        <TodoPanel todos={transcript.todos} />
      </>,
    );
    for (const details of container.querySelectorAll("details")) details.open = true;
    for (const button of container.querySelectorAll("button")) {
      if (button.getAttribute("aria-expanded") !== "true" && !button.closest("[data-testid=approval], [data-testid=question]")) {
        fireEvent.click(button);
      }
    }
    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.querySelector("b")).toBeNull();
    for (const link of container.querySelectorAll("a")) expect(link.getAttribute("href") ?? "").not.toMatch(/^javascript:/i);
    expect(container.textContent).toContain('<img src=x onerror="window.hacked=1">');
    expect((window as unknown as { hacked?: number }).hacked).toBeUndefined();
  });
});
