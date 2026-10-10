import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ListingView, StoreListing } from "../api/listing";
import { useStore } from "../state/store";
import { AppleListingPage } from "./AppleListing";

const listing: StoreListing = {
  locale: "en-US", name: "Tally", subtitle: "Count anything", description: "Tap to count.", keywords: "counter,tally",
  promotional_text: "", whats_new: "", copyright: "2026 Ada", primary_category: "UTILITIES", secondary_category: null,
  age_rating: { violence_realistic: "NONE", gambling: false }, collects_data: false, privacy: [],
  notes: ["Check the screenshots."], support_url: "", marketing_url: "", privacy_policy_url: "",
};
const draft: ListingView = { listing, source: "draft", updated_at: 0, missing: ["support_url", "privacy_policy_url"] };

const mocks = vi.hoisted(() => ({ get: vi.fn(), draft: vi.fn(), save: vi.fn() }));
vi.mock("../api/listing", async (original) => ({
  ...(await original<typeof import("../api/listing")>()),
  appleListing: () => ({ get: mocks.get, draft: mocks.draft, save: mocks.save }),
}));

beforeEach(() => {
  mocks.get.mockReset().mockResolvedValue(draft);
  mocks.draft.mockReset().mockResolvedValue({ ...draft, listing: { ...listing, name: "Tally Pro" } });
  mocks.save.mockReset().mockResolvedValue({ ...draft, source: "saved", missing: [] });
  useStore.setState({ error: "", projects: [{ id: "p1", name: "Tally", role: "owner", source: "apple", kind: "apple", created_at: 0, updated_at: 0 }] });
});

afterEach(cleanup);

function open() {
  render(
    <MemoryRouter initialEntries={["/p/p1/apple/listing"]}>
      <Routes>
        <Route path="/p/:projectId/apple/listing" element={<AppleListingPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("the store texts page", () => {
  it("shows Forge's draft, what Apple still needs, and saves what the user finished", async () => {
    open();
    expect(await screen.findByDisplayValue("Tally")).toBeTruthy();
    expect(screen.getByTestId("listing-missing").textContent).toMatch(/Support/);
    expect(screen.getByText("Check the screenshots.")).toBeTruthy();
    fireEvent.change(screen.getByLabelText(/^(Support URL|Support-URL)$/), { target: { value: "https://example.com/help" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Save|Speichern)$/ }));
    await waitFor(() => expect(mocks.save).toHaveBeenCalledWith(expect.objectContaining({ support_url: "https://example.com/help", name: "Tally" })));
  });

  it("counts the keywords in bytes, as Apple does, and takes a new draft on request", async () => {
    open();
    const keywords = (await screen.findByDisplayValue("counter,tally")) as HTMLInputElement;
    fireEvent.change(keywords, { target: { value: "ä".repeat(51) } });
    expect(screen.getByText(/^102 (of|von) 100$/).className).toContain("text-bad");
    fireEvent.click(screen.getByRole("button", { name: /(Take Forge's newest draft|Neuesten Entwurf)/ }));
    expect(await screen.findByDisplayValue("Tally Pro")).toBeTruthy();
  });

  it("says when there is no draft yet", async () => {
    mocks.get.mockResolvedValue({ listing: null, source: "none", updated_at: 0, missing: [] });
    open();
    expect(await screen.findByText(/(has not drafted|noch nicht entworfen)/)).toBeTruthy();
  });
});
