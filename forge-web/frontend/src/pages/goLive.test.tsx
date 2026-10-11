import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Deploy, HostingOverview } from "../api/hosting";
import { useStore } from "../state/store";
import { GoLivePage } from "./GoLive";

const APPROVED = "0123456789abcdef0123456789abcdef01234567";
const STEPS: Deploy["steps"] = ["pack", "check", "backup", "migrate", "release", "done"];
const deploy = (changes: Partial<Deploy>): Deploy => ({
  id: "d1", environment: "staging", commit: APPROVED, release: 1, app: "shop", step: "done", status: "live",
  error: "", hint: "", steps: STEPS, checks: [{ name: "api: tests", ok: true, detail: "" }], checks_from: "",
  url: "https://shop.staging.apps.example", log_tail: "", creator_approved_at: 0, admin: "", admin_approved_at: 0,
  migrated: true, created_at: 1, updated_at: 1,
  ...changes,
});
const overview = (changes: Partial<HostingOverview>): HostingOverview => ({
  enabled: true, app: "shop", suspended: false, suspended_reason: "",
  reviews: [{ stage: "product", verdict: "ok", summary: "Impressum, privacy page and reporting are there.", findings: [] }],
  approvals: [{ commit: APPROVED, at: 1, summary: "", chat_id: "c1" }],
  deploys: [], staging: null, production: null, secrets: [],
  ...changes,
});

const mocks = vi.hoisted(() => ({ overview: vi.fn(), start: vi.fn(), retry: vi.fn(), setSecret: vi.fn() }));
vi.mock("../api/hosting", () => ({
  hosting: () => ({ overview: mocks.overview, start: mocks.start, retry: mocks.retry, setSecret: mocks.setSecret }),
}));

beforeEach(() => {
  mocks.overview.mockReset().mockResolvedValue(overview({}));
  mocks.start.mockReset().mockResolvedValue(deploy({ status: "running", step: "pack" }));
  mocks.retry.mockReset().mockResolvedValue(deploy({}));
  mocks.setSecret.mockReset().mockResolvedValue({ ok: true });
  useStore.setState({ error: "", projects: [{ id: "p1", name: "Shop", role: "owner", source: "app", kind: "app", created_at: 0, updated_at: 0 }] });
});

afterEach(cleanup);

function open() {
  render(
    <MemoryRouter initialEntries={["/p/p1/live"]}>
      <Routes>
        <Route path="/p/:projectId/live" element={<GoLivePage />} />
      </Routes>
    </MemoryRouter>,
  );
}

const toStaging = /^(Put it on staging|Auf Staging bringen)$/;
const goLive = /^(Go live|Live schalten)$/;
const button = (name: RegExp) => screen.getByRole("button", { name }) as HTMLButtonElement;

describe("the go-live page", () => {
  it("shows the review and the approved commit, and puts that commit on staging first", async () => {
    open();
    expect(await screen.findByText(/0123456789/)).toBeTruthy();
    expect(screen.getByTestId("release-reviews").textContent).toContain("Impressum, privacy page");
    expect(button(goLive).disabled).toBe(true); // nothing in staging yet
    await waitFor(() => expect(button(toStaging).disabled).toBe(false));
    fireEvent.click(button(toStaging));
    await waitFor(() => expect(mocks.start).toHaveBeenCalledWith("staging", APPROVED));
  });

  it("goes live with the commit that runs in staging, showing its link and the server's checks", async () => {
    const staged = deploy({});
    mocks.overview.mockResolvedValue(overview({ staging: staged, deploys: [staged] }));
    open();
    expect((await screen.findByTestId("live-links")).textContent).toContain("https://shop.staging.apps.example");
    expect(screen.getByTestId("host-checks").textContent).toContain("api: tests");
    await waitFor(() => expect(button(goLive).disabled).toBe(false));
    fireEvent.click(button(goLive));
    await waitFor(() => expect(mocks.start).toHaveBeenCalledWith("production", APPROVED));
  });

  it("asks for the approval first, and stays closed while the app is suspended", async () => {
    mocks.overview.mockResolvedValue(overview({ approvals: [] }));
    open();
    expect(await screen.findByText(/(Ready to go live”|„Ready to go live“)/)).toBeTruthy();
    expect(button(toStaging).disabled).toBe(true);
    cleanup();
    mocks.overview.mockResolvedValue(overview({ suspended: true, suspended_reason: "a notice is being checked" }));
    open();
    expect((await screen.findByTestId("suspended")).textContent).toContain("a notice is being checked");
    expect(button(toStaging).disabled).toBe(true);
  });

  it("says why a deploy stopped, with its log and a retry", async () => {
    const failed = deploy({
      id: "d2", status: "failed", step: "release", error: "the release did not get healthy and was rolled back: api",
      hint: "the release before keeps running", log_tail: "Traceback: KeyError 'MAPS'",
    });
    mocks.overview.mockResolvedValue(overview({ deploys: [failed] }));
    open();
    const problem = await screen.findByTestId("deploy-problem");
    expect(problem.textContent).toContain("rolled back");
    expect(problem.textContent).toContain("KeyError");
    fireEvent.click(screen.getByRole("button", { name: /^(Start again from here|Ab hier neu starten)$/ }));
    await waitFor(() => expect(mocks.retry).toHaveBeenCalledWith("d2"));
  });

  it("takes a secret's value without ever showing one", async () => {
    mocks.overview.mockResolvedValue(overview({ secrets: [{ environment: "staging", name: "MAPS_KEY" }] }));
    open();
    expect((await screen.findByTestId("secret-names")).textContent).toContain("MAPS_KEY");
    fireEvent.change(screen.getByPlaceholderText("MAPS_KEY"), { target: { value: "STRIPE_KEY" } });
    const value = screen.getByLabelText(/^(Value|Wert)$/) as HTMLInputElement;
    expect(value.type).toBe("password");
    fireEvent.change(value, { target: { value: "sk_live_x" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Save|Speichern)$/ }));
    await waitFor(() => expect(mocks.setSecret).toHaveBeenCalledWith("staging", "STRIPE_KEY", "sk_live_x"));
  });
});
