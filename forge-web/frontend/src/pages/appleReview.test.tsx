import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppleReviewData } from "../api/apple";
import { useStore } from "../state/store";
import { AppleReviewPage } from "./AppleReview";

const choices = { approve: "Ready for Apple", send_back: "Send it back to the agent", not_yet: "Not yet" };
const waiting: AppleReviewData = {
  reviews: [
    { stage: "prompt", verdict: "ok", summary: "A counter app is fine.", findings: [], sources: [], error: "" },
    { stage: "product", verdict: "concern", summary: "Check the privacy label.", findings: [], sources: [], error: "" },
  ],
  pending: { chat_id: "c1", request_id: "r9", text: "Is the app ready for Apple? The Apple review: fine." },
  choices,
  builds: [{ id: "j1", kind: "build", params: { platform: "ios", action: "test" }, status: "done", outcome: "test on ios: succeeded", seconds: 42, created_at: 1 }],
  approvals: [],
};

const mocks = vi.hoisted(() => ({ review: vi.fn(), decide: vi.fn(), screens: vi.fn() }));
vi.mock("../api/apple", () => ({ appleReview: mocks.review, decideApproval: mocks.decide }));
vi.mock("../api/project", () => ({ projectApi: () => ({ screens: mocks.screens }) }));

beforeEach(() => {
  mocks.review.mockReset().mockResolvedValue(waiting);
  mocks.decide.mockReset().mockResolvedValue({ accepted: true });
  mocks.screens.mockReset().mockResolvedValue([{ platform: "ios", dark: false, url: "data:image/png;base64,iVBORw0KGgo=" }]);
  useStore.setState({ error: "", projects: [{ id: "p1", name: "Tally", role: "owner", source: "apple", kind: "apple", created_at: 0, updated_at: 0 }] });
});

afterEach(cleanup);

function open() {
  render(
    <MemoryRouter initialEntries={["/p/p1/apple"]}>
      <Routes>
        <Route path="/p/:projectId/apple" element={<AppleReviewPage />} />
        <Route path="/c/:chatId" element={<p>the chat</p>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("the approval page", () => {
  it("shows the devices, the reviews and the builds, and approves on the user's click", async () => {
    open();
    expect(await screen.findByAltText(/^iPhone \((Light|Hell)\)$/)).toBeTruthy();
    expect(screen.getAllByTestId("guideline-review").map((c) => c.textContent)).toEqual([
      expect.stringContaining("A counter app is fine."),
      expect.stringContaining("Check the privacy label."),
    ]);
    expect(screen.getByText("test on ios: succeeded")).toBeTruthy();
    expect(mocks.decide).not.toHaveBeenCalled(); // nothing is approved by opening the page
    fireEvent.click(screen.getByRole("button", { name: /^(Approve for Apple|Für Apple freigeben)$/ }));
    await waitFor(() => expect(mocks.decide).toHaveBeenCalledWith("c1", "r9", "Ready for Apple"));
    await waitFor(() => expect(mocks.review).toHaveBeenCalledTimes(2)); // shown again, approved
  });

  it("sends the app back to the agent and opens the chat for the feedback", async () => {
    open();
    fireEvent.click(await screen.findByRole("button", { name: /^(Back to the agent|Zurück an den Agenten)$/ }));
    await waitFor(() => expect(mocks.decide).toHaveBeenCalledWith("c1", "r9", "Send it back to the agent"));
    expect(await screen.findByText("the chat")).toBeTruthy();
  });

  it("says when the question is gone, and who approved what", async () => {
    mocks.decide.mockResolvedValue({ accepted: false });
    open();
    fireEvent.click(await screen.findByRole("button", { name: /^(Approve for Apple|Für Apple freigeben)$/ }));
    await waitFor(() => expect(useStore.getState().error).toMatch(/no longer open|nicht mehr offen/));
    cleanup();
    const approval = { id: "a1", chat_id: "c1", user: "Ada", at: 1, commit: "0123456789abcdef0123456789abcdef01234567", clean: false, summary: "" };
    mocks.review.mockResolvedValue({ ...waiting, pending: null, approvals: [approval] });
    open();
    const decided = await screen.findAllByText(/(Approved by|Freigegeben von) Ada/);
    expect(decided[0].textContent).toMatch(/0123456789/);
    expect(decided[0].textContent).toMatch(/uncommitted|nicht committeten/);
    expect(screen.queryByRole("button", { name: /^(Approve for Apple|Für Apple freigeben)$/ })).toBeNull();
    expect(screen.queryByTestId("open-release")).toBeNull(); // uncommitted changes: nothing to release
    cleanup();
    mocks.review.mockResolvedValue({ ...waiting, pending: null, approvals: [{ ...approval, clean: true }] });
    open();
    const link = await screen.findByTestId("open-release");
    expect(link.getAttribute("href")).toBe("/p/p1/apple/release");
  });
});
