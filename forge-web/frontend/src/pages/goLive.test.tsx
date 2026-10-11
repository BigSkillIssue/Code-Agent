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
const OPERATOR = {
  name: "Ada Lovelace", representative: "", address: "Hauptstraße 1\n10115 Berlin", email: "ada@example.com",
  phone: "", trade_register: "", vat_id: "", privacy_email: "", privacy_extra: "",
};
const overview = (changes: Partial<HostingOverview>): HostingOverview => ({
  enabled: true, app: "shop", suspended: false, suspended_reason: "", deleting: false, deleting_error: "", backups: [],
  reviews: [{ stage: "product", verdict: "ok", summary: "Impressum, privacy page and reporting are there.", findings: [] }],
  approvals: [{ commit: APPROVED, at: 1, summary: "", chat_id: "c1" }],
  deploys: [], staging: null, production: null, secrets: [], operator: { ...OPERATOR, updated_at: 1 },
  ...changes,
});

const mocks = vi.hoisted(() => ({
  overview: vi.fn(), start: vi.fn(), retry: vi.fn(), setSecret: vi.fn(), setOperator: vi.fn(), exportData: vi.fn(),
  deleteApp: vi.fn(), domains: vi.fn(), addDomain: vi.fn(), checkDomain: vi.fn(), removeDomain: vi.fn(),
}));
vi.mock("../api/hosting", () => ({
  hosting: () => ({
    overview: mocks.overview, start: mocks.start, retry: mocks.retry, setSecret: mocks.setSecret,
    setOperator: mocks.setOperator, exportData: mocks.exportData, deleteApp: mocks.deleteApp,
    domains: mocks.domains, addDomain: mocks.addDomain, checkDomain: mocks.checkDomain, removeDomain: mocks.removeDomain,
    exportUrl: (backupId: string) => `/api/projects/p1/hosting/backups/${backupId}`,
  }),
}));

beforeEach(() => {
  mocks.overview.mockReset().mockResolvedValue(overview({}));
  mocks.start.mockReset().mockResolvedValue(deploy({ status: "running", step: "pack" }));
  mocks.retry.mockReset().mockResolvedValue(deploy({}));
  mocks.setSecret.mockReset().mockResolvedValue({ ok: true });
  mocks.setOperator.mockReset().mockResolvedValue(OPERATOR);
  mocks.exportData.mockReset().mockResolvedValue({ started: true });
  mocks.deleteApp.mockReset().mockResolvedValue(undefined);
  mocks.domains.mockReset().mockResolvedValue([]);
  mocks.addDomain.mockReset().mockResolvedValue({});
  mocks.checkDomain.mockReset().mockResolvedValue({});
  mocks.removeDomain.mockReset().mockResolvedValue(undefined);
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

  it("goes live only with the operator's details, which it takes in a form", async () => {
    const staged = deploy({});
    mocks.overview.mockResolvedValue(overview({ staging: staged, deploys: [staged], operator: null }));
    open();
    expect(await screen.findByText(/(details of who runs the app|Angaben, wer die App betreibt)/)).toBeTruthy();
    expect(button(goLive).disabled).toBe(true);
    const save = button(/^(Save the details|Angaben speichern)$/);
    expect(save.disabled).toBe(true); // name, address and email first
    fireEvent.change(screen.getByLabelText(/^(Name \(a person|Name \(vollständiger)/), { target: { value: "Ada Lovelace" } });
    fireEvent.change(screen.getByLabelText(/^(Postal address|Postanschrift)/), { target: { value: "Hauptstraße 1" } });
    fireEvent.change(screen.getByLabelText(/^(Email|E-Mail)$/), { target: { value: "ada@example.com" } });
    fireEvent.click(save);
    await waitFor(() =>
      expect(mocks.setOperator).toHaveBeenCalledWith(expect.objectContaining({ name: "Ada Lovelace", email: "ada@example.com" })),
    );
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

  it("lets the owner export each live environment and delete the app after typing its name", async () => {
    const staged = deploy({});
    const backups = [{ id: "b1", environment: "staging" as const, created_at: 5, size: 2_500_000 }];
    mocks.overview.mockResolvedValue(overview({ staging: staged, deploys: [staged], backups }));
    open();
    fireEvent.click(await screen.findByRole("button", { name: /^(Export staging|Staging exportieren)$/ }));
    await waitFor(() => expect(mocks.exportData).toHaveBeenCalledWith("staging"));
    expect(screen.queryByRole("button", { name: /^(Export live|Live-Version exportieren)$/ })).toBeNull();
    const link = screen.getByTestId("exports").querySelector("a");
    expect(link?.getAttribute("href")).toBe("/api/projects/p1/hosting/backups/b1");
    expect(link?.textContent).toContain("2.5 MB");
    const remove = button(/^(Delete the app|App löschen)$/);
    expect(remove.disabled).toBe(true); // the app's name first
    fireEvent.change(screen.getByLabelText(/(type its name|Namen der App eintippen): shop/), { target: { value: "shop" } });
    fireEvent.click(remove);
    await waitFor(() => expect(mocks.deleteApp).toHaveBeenCalled());
  });

  it("shows a deletion under way, and offers no data section to members who are not owners", async () => {
    mocks.overview.mockResolvedValue(overview({ deleting: true, deleting_error: "the host did not report the job in time" }));
    open();
    expect((await screen.findByTestId("deleting")).textContent).toContain("did not report");
    cleanup();
    useStore.setState({ projects: [{ id: "p1", name: "Shop", role: "editor", source: "app", kind: "app", created_at: 0, updated_at: 0 }] });
    mocks.overview.mockResolvedValue(overview({}));
    open();
    await screen.findByText(/0123456789/);
    expect(screen.queryByRole("button", { name: /^(Delete the app|App löschen)$/ })).toBeNull();
  });

  it("adds an own domain, shows the TXT record to create and checks it", async () => {
    const staged = deploy({});
    mocks.overview.mockResolvedValue(overview({ staging: staged, deploys: [staged] }));
    const waiting = {
      domain: "shop.example.org", environment: "staging" as const, txt_name: "_forge-verify.shop.example.org",
      txt_value: "forge-verify=abc123", points_to: "shop.staging.apps.example", verified_at: 0, checked_at: 4,
      error: "no TXT record forge-verify=abc123 at _forge-verify.shop.example.org yet",
    };
    mocks.domains.mockResolvedValueOnce([]).mockResolvedValue([waiting]);
    open();
    fireEvent.change(await screen.findByPlaceholderText("shop.example.com"), { target: { value: " shop.example.org " } });
    fireEvent.click(button(/^(Add|Hinzufügen)$/));
    await waitFor(() => expect(mocks.addDomain).toHaveBeenCalledWith("shop.example.org", "staging"));
    const dns = await screen.findByTestId("domain-dns");
    expect(dns.textContent).toContain("_forge-verify.shop.example.org");
    expect(dns.textContent).toContain("forge-verify=abc123");
    expect(dns.textContent).toContain("shop.staging.apps.example");
    fireEvent.click(button(/^(Check|Prüfen)$/));
    await waitFor(() => expect(mocks.checkDomain).toHaveBeenCalledWith("shop.example.org"));
  });
});
