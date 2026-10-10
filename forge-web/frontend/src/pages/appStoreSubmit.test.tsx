import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppleRelease } from "../api/apple";
import type { AppleSubmission } from "../api/submissions";
import { useStore } from "../state/store";
import { AppStoreSubmit } from "./AppStoreSubmit";

const release: AppleRelease = {
  id: "r1", platform: "ios", commit: "0123456789", step: "done", status: "done", error: "", hint: "",
  build_number: 29312345, version: "1.0", bundle_id: "com.example.tally",
  steps: ["archive", "identify", "export", "upload", "process", "testflight", "done"], created_at: 1, updated_at: 1,
};
const submission = (changes: Partial<AppleSubmission>): AppleSubmission => ({
  id: "s1", release_id: "r1", platform: "ios", version: "1.0", status: "ready", step: "placements",
  steps: ["screenshots", "version", "texts", "terms", "contact", "placements"], error: "", hint: "",
  review_state: "", version_state: "", screenshots: 6, created_at: 1, updated_at: 1, ...changes,
});

const mocks = vi.hoisted(() => ({ list: vi.fn(), prepare: vi.fn(), submit: vi.fn(), release: vi.fn() }));
vi.mock("../api/submissions", () => ({
  appleSubmissions: () => ({ list: mocks.list, prepare: mocks.prepare, submit: mocks.submit, release: mocks.release }),
}));

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
  mocks.list.mockResolvedValue([]);
  mocks.prepare.mockResolvedValue(submission({ status: "preparing", step: "screenshots" }));
  mocks.submit.mockResolvedValue(submission({ status: "submitted" }));
  mocks.release.mockResolvedValue(submission({ status: "released" }));
  useStore.setState({ error: "" });
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const field = (label: RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } });

describe("the App Store step", () => {
  it("prepares a release from TestFlight once the contact for App Review is given", async () => {
    render(<AppStoreSubmit projectId="p1" appName="Tally" releases={[release]} />);
    const prepare = await screen.findByRole("button", { name: /(Prepare for the App Store|Für den App Store vorbereiten)/ });
    expect((prepare as HTMLButtonElement).disabled).toBe(true);
    field(/^(First name|Vorname)$/, "Ada");
    field(/^(Last name|Nachname)$/, "Lovelace");
    field(/^(Phone|Telefon)/, "+44 20 7946 0000");
    field(/^(Email|E-Mail)$/, "review@example.com");
    fireEvent.click(prepare);
    await waitFor(() => expect(mocks.prepare).toHaveBeenCalledWith("r1", expect.objectContaining({ last_name: "Lovelace" })));
  });

  it("submits only after the user confirms, naming the version", async () => {
    mocks.list.mockResolvedValue([submission({})]);
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<AppStoreSubmit projectId="p1" appName="Tally" releases={[release]} />);
    const send = await screen.findByRole("button", { name: /^(Submit to Apple|Bei Apple einreichen)$/ });
    fireEvent.click(send);
    expect(confirm).toHaveBeenCalledWith(expect.stringContaining("1.0"));
    expect(mocks.submit).not.toHaveBeenCalled(); // the user said no
    fireEvent.click(send);
    await waitFor(() => expect(mocks.submit).toHaveBeenCalledWith("s1", "1.0"));
  });

  it("shows Apple's state and releases an approved version on the user's click", async () => {
    mocks.list.mockResolvedValue([submission({ status: "submitted", version_state: "PENDING_DEVELOPER_RELEASE" })]);
    vi.spyOn(window, "confirm").mockReturnValue(true);
    render(<AppStoreSubmit projectId="p1" appName="Tally" releases={[release]} />);
    expect(await screen.findByText(/pending developer release/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^(Release on the App Store|Im App Store veröffentlichen)$/ }));
    await waitFor(() => expect(mocks.release).toHaveBeenCalledWith("s1"));
  });
});
