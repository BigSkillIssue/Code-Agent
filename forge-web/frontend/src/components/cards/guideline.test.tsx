import { cleanup, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { GuidelineReview } from "../../api/apple";
import type { StoredItem } from "../../api/types";
import { buildTranscript } from "../../state/transcript";
import { Transcript } from "../Transcript";
import { GuidelineCard } from "./GuidelineCard";

afterEach(cleanup);

const review: GuidelineReview = {
  stage: "product",
  verdict: "violation",
  summary: "The app collects data without saying so.",
  findings: [
    { area: "legal", status: "violation", guideline: "5.1.1", reason: "No privacy policy.", fix: "Add one." },
    { area: "design", status: "ok", guideline: "4.2", reason: "Enough features.", fix: "" },
  ],
  sources: ["https://developer.apple.com/app-store/review/guidelines/", "javascript:alert(1)"],
  error: "",
};

const approval = {
  text: "Is the app ready for Apple?",
  kind: "choice" as const,
  options: ["Not yet", "Ready for Apple", "Send it back to the agent"],
  default: "Not yet",
  why: "Only an app you approve counts.",
};

describe("guideline reviews", () => {
  it("show the problems first and the fine areas folded away", () => {
    render(<GuidelineCard review={review} />);
    const card = screen.getByTestId("guideline-review");
    expect(card.textContent).toMatch(/Apple review of the app|Apple-Prüfung der App/);
    expect(card.textContent).toMatch(/breaks the guidelines|verstößt gegen die Richtlinien/);
    expect(card.textContent).toContain("5.1.1");
    expect(card.textContent).toContain("Add one.");
    const folded = card.querySelector("details")!;
    expect(folded.textContent).toContain("Enough features.");
    expect(within(folded).getByText(/1 (areas without problems|Bereiche ohne Probleme)/)).toBeTruthy();
  });

  it("link only to Apple's own pages", () => {
    render(<GuidelineCard review={review} />);
    const links = screen.getAllByRole("link").map((a) => a.getAttribute("href"));
    expect(links).toEqual(["https://developer.apple.com/app-store/review/guidelines/"]);
    expect(screen.getByText(/javascript:alert/)).toBeTruthy(); // shown as text only
  });

  it("land in the main transcript although the reviewer is an agent of its own", () => {
    const items: StoredItem[] = [
      { seq: 1, item: { type: "event", event: { kind: "model_done", agent_id: "apple-reviewer-product", message: { parts: [] } } } },
      { seq: 2, item: { type: "event", event: { kind: "guideline_review", agent_id: "apple-reviewer-product", ...review } } },
      { seq: 3, item: { type: "request", id: "r1", kind: "question", payload: { questions: [approval] }, purpose: "apple_approval" } },
    ] as StoredItem[];
    const { entries } = buildTranscript(items);
    expect(entries.map((e) => e.kind)).toEqual(["agent", "guideline", "question"]); // its work, folded
    expect(entries[0]).toMatchObject({ role: "apple_reviewer", status: "ok" }); // its verdict ends it
    expect(entries[2]).toMatchObject({ appleApproval: true });
  });

  it("make Forge's approval question link to the approval page", () => {
    const entries = buildTranscript([
      { seq: 1, item: { type: "request", id: "r1", kind: "question", payload: { questions: [approval] }, purpose: "apple_approval" } },
      { seq: 2, item: { type: "request", id: "r2", kind: "question", payload: { questions: [approval] } } },
    ] as StoredItem[]).entries;
    render(
      <MemoryRouter>
        <Transcript entries={entries} onAnswer={vi.fn()} approvalPage="/p/p1/apple" />
      </MemoryRouter>,
    );
    const links = screen.getAllByRole("link", { name: /Open the approval page|Zur Freigabe öffnen/ });
    expect(links.map((a) => a.getAttribute("href"))).toEqual(["/p/p1/apple"]); // the look-alike gets none
  });
});
