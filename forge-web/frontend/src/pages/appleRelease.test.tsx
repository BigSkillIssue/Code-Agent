import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppleRelease, AppleReviewData } from "../api/apple";
import { useStore } from "../state/store";
import { AppleReleasePage } from "./AppleRelease";

const STEPS: AppleRelease["steps"] = ["archive", "identify", "export", "upload", "process", "testflight", "done"];
const approved: AppleReviewData = {
  reviews: [],
  pending: null,
  choices: { approve: "a", send_back: "b", not_yet: "c" },
  builds: [],
  approvals: [{ id: "a1", chat_id: "c1", user: "Ada", at: 1, commit: "0123456789abcdef", clean: true, summary: "" }],
};
const key = { key_id: "ABC123DEFG", issuer_id: "i", team_id: "TEAM123456", created_at: 1, checked_at: 1, check_ok: true, check_message: "" };
const release = (changes: Partial<AppleRelease>): AppleRelease => ({
  id: "r1", platform: "ios", commit: "0123456789abcdef", step: "upload", status: "running", error: "", hint: "",
  build_number: 29312345, version: "1.0", bundle_id: "com.example.tally", steps: STEPS, created_at: 1, updated_at: 1,
  ...changes,
});

const mocks = vi.hoisted(() => ({ review: vi.fn(), key: vi.fn(), list: vi.fn(), start: vi.fn(), retry: vi.fn() }));
vi.mock("../api/submissions", () => ({
  appleSubmissions: () => ({ list: async () => [], prepare: vi.fn(), submit: vi.fn(), release: vi.fn() }),
}));
vi.mock("../api/apple", () => ({
  appleReview: mocks.review,
  appStoreKey: { get: mocks.key },
  appleReleases: () => ({ list: mocks.list, start: mocks.start, retry: mocks.retry }),
}));

beforeEach(() => {
  mocks.review.mockReset().mockResolvedValue(approved);
  mocks.key.mockReset().mockResolvedValue(key);
  mocks.list.mockReset().mockResolvedValue([]);
  mocks.start.mockReset().mockResolvedValue([]);
  mocks.retry.mockReset().mockResolvedValue(release({}));
  useStore.setState({ error: "", projects: [{ id: "p1", name: "Tally", role: "owner", source: "apple", kind: "apple", created_at: 0, updated_at: 0 }] });
});

afterEach(cleanup);

function open() {
  render(
    <MemoryRouter initialEntries={["/p/p1/apple/release"]}>
      <Routes>
        <Route path="/p/:projectId/apple/release" element={<AppleReleasePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const iosButton = /^(To TestFlight: iPhone, iPad and Watch|Zu TestFlight: iPhone, iPad und Watch)$/;

describe("the release page", () => {
  it("starts a release of the approved commit only on the user's click", async () => {
    open();
    expect(await screen.findByText(/0123456789/)).toBeTruthy();
    expect(mocks.start).not.toHaveBeenCalled();
    await waitFor(() => expect((screen.getByRole("button", { name: iosButton }) as HTMLButtonElement).disabled).toBe(false));
    fireEvent.click(screen.getByRole("button", { name: iosButton }));
    await waitFor(() => expect(mocks.start).toHaveBeenCalledWith(["ios"]));
  });

  it("shows each release's steps, and where one stopped with Apple's reason and a retry", async () => {
    mocks.list.mockResolvedValue([
      release({ id: "r2", status: "failed", step: "identify", error: "App Store Connect has no app with the bundle ID com.example.tally", hint: "create the app once" }),
      release({ id: "r1", platform: "macos", status: "done", step: "done" }),
    ]);
    open();
    const cards = await screen.findAllByTestId("release");
    expect(cards).toHaveLength(2);
    expect(screen.getByTestId("release-problem").textContent).toContain("no app with the bundle ID");
    fireEvent.click(screen.getByRole("button", { name: /^(Start again from here|Ab hier neu starten)$/ }));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith("r2"));
  });

  it("asks for an approval and a key before anything can start", async () => {
    mocks.review.mockResolvedValue({ ...approved, approvals: [] });
    mocks.key.mockResolvedValue(null);
    open();
    expect(await screen.findByText(/(Approve the app first|Gib die App zuerst frei)/)).toBeTruthy();
    expect(await screen.findByText(/(App Store Connect team key first|Teamschlüssel)/)).toBeTruthy();
    expect((screen.getByRole("button", { name: iosButton }) as HTMLButtonElement).disabled).toBe(true);
  });
});
