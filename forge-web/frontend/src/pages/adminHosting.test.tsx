import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AbuseNotice, HostedApp, HostingUser, QueueItem } from "../api/hosting";
import { useStore } from "../state/store";
import { HostingTab } from "./AdminHosting";

const COMMIT = "fedcba9876543210fedcba9876543210fedcba98";
const item: QueueItem = {
  id: "d9", environment: "production", commit: COMMIT, release: 1, app: "shop", step: "admin", status: "waiting",
  error: "", hint: "", steps: [], checks: [{ name: "api: tests", ok: true, detail: "" }], checks_from: "d1",
  url: "", log_tail: "", creator_approved_at: 1, admin: "", admin_approved_at: 0, migrated: false, created_at: 1,
  updated_at: 1, project_id: "p1", project: "Shop", creator: "Maria", review_summary: "Ready: legal pages there.",
};
const app: HostedApp = {
  project_id: "p1", app: "shop", project: "Shop", host: "app-server-1", suspended: false, suspended_reason: "",
  suspended_at: 0, live: { production: { release: 3, commit: COMMIT, url: "https://shop.apps.example" } },
};
const maria: HostingUser = { id: "u2", email: "maria@example.com", name: "Maria", role: "member", trusted: false };
const ada: HostingUser = { id: "u1", email: "ada@example.com", name: "Ada", role: "admin", trusted: false };

const notice: AbuseNotice = {
  id: "n1", project_id: "p1", app: "shop", project: "Shop", location: "https://shop.apps.example/offers/7",
  category: "fraud", reason: "Payment is taken for goods that are never sent.", name: "Kim", email: "kim@example.org",
  status: "open", decision: "", created_at: 5, decided_at: 0,
};

const mocks = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>> }));
vi.mock("../api/hosting", () => ({ hostingAdmin: mocks.api }));

beforeEach(() => {
  Object.assign(mocks.api, {
    hosts: vi.fn(async () => [{ id: "h1", name: "app-server-1", enabled: true, created_at: 1, last_seen: 2, online: true, version: "0.1.0", apps: 1 }]),
    addHost: vi.fn(async (name: string) => ({ id: "h2", name, enabled: true, created_at: 3, last_seen: 0, online: false, version: "", apps: 0, token: "fhw_h2_secret" })),
    setHost: vi.fn(async () => ({})),
    queue: vi.fn(async () => [item]),
    approve: vi.fn(async () => ({})),
    refuse: vi.fn(async () => ({})),
    apps: vi.fn(async () => [app]),
    suspend: vi.fn(async () => ({ suspended: true, environments: ["production"] })),
    logs: vi.fn(async () => ({ ok: true, log_tail: "--- api\nGET /api/healthz 200", error: "" })),
    users: vi.fn(async () => [ada, maria]),
    trust: vi.fn(async () => ({ trusted: true })),
    notices: vi.fn(async () => [notice, { ...notice, id: "n2", status: "dismissed", decision: "a real shop" }]),
    decide: vi.fn(async () => ({ ...notice, status: "actioned" })),
  });
  useStore.setState({ error: "" });
});

afterEach(cleanup);

describe("the hosting panel", () => {
  it("shows exactly the checked commit with its checks, and approves or refuses with a reason", async () => {
    render(<HostingTab />);
    const card = await screen.findByTestId("queue-item");
    expect(within(card).getByTestId("queue-commit").textContent).toBe(COMMIT);
    expect(card.textContent).toContain("api: tests");
    expect(card.textContent).toContain("Shop");
    expect(card.textContent).toContain("Maria");
    const refuse = within(card).getByRole("button", { name: /^(Refuse|Ablehnen)$/ }) as HTMLButtonElement;
    expect(refuse.disabled).toBe(true); // a refusal needs its reason
    fireEvent.change(within(card).getByLabelText(/^(Why \(the creator reads it\)|Warum \(der Ersteller liest es\))$/), {
      target: { value: "no contact on the privacy page" },
    });
    fireEvent.click(refuse);
    await waitFor(() => expect(mocks.api.refuse).toHaveBeenCalledWith("d9", "no contact on the privacy page"));
    fireEvent.click(within(card).getByRole("button", { name: /^(Approve|Freigeben)$/ }));
    await waitFor(() => expect(mocks.api.approve).toHaveBeenCalledWith("d9"));
  });

  it("adds an app server and shows its token once", async () => {
    render(<HostingTab />);
    expect((await screen.findByTestId("hosts")).textContent).toContain("app-server-1");
    fireEvent.change(screen.getByPlaceholderText("app-server-1"), { target: { value: "app-server-2" } });
    fireEvent.change(screen.getByLabelText(/(Public key|Öffentlicher Schlüssel)/), { target: { value: "A".repeat(43) + "=" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Add server|Server hinzufügen)$/ }));
    expect((await screen.findByTestId("host-token")).textContent).toContain("fhw_h2_secret");
    expect(mocks.api.addHost).toHaveBeenCalledWith("app-server-2", "A".repeat(43) + "=");
  });

  it("suspends an app with a statement of reasons and reads its logs", async () => {
    render(<HostingTab />);
    const card = await screen.findByTestId("hosted-app");
    expect(card.textContent).toContain("https://shop.apps.example");
    const suspend = within(card).getByRole("button", { name: /^(Suspend|Sperren)$/ }) as HTMLButtonElement;
    expect(suspend.disabled).toBe(true);
    fireEvent.change(within(card).getByLabelText(/(statement of reasons|Begründung)/), { target: { value: "a DSA notice" } });
    fireEvent.click(suspend);
    await waitFor(() => expect(mocks.api.suspend).toHaveBeenCalledWith("p1", true, "a DSA notice"));
    fireEvent.click(within(card).getByRole("button", { name: /^(Logs|Protokolle)$/ }));
    expect((await screen.findByTestId("app-logs")).textContent).toContain("GET /api/healthz 200");
    expect(mocks.api.logs).toHaveBeenCalledWith("p1", "production");
  });

  it("marks a person trusted (admins need no mark)", async () => {
    render(<HostingTab />);
    const box = (await screen.findByRole("checkbox")) as HTMLInputElement;
    expect(screen.queryByText("Ada")).toBeNull();
    fireEvent.click(box);
    await waitFor(() => expect(mocks.api.trust).toHaveBeenCalledWith("u2", true));
  });

  it("decides a report with reasons: suspending the app or no action", async () => {
    render(<HostingTab />);
    const [open, done] = await screen.findAllByTestId("notice");
    expect(open.textContent).toContain("https://shop.apps.example/offers/7");
    expect(open.textContent).toContain("Kim <kim@example.org>");
    expect(within(done).getByTestId("notice-decision").textContent).toContain("a real shop");
    const suspend = within(open).getByRole("button", { name: /^(Suspend the app|App sperren)$/ }) as HTMLButtonElement;
    expect(suspend.disabled).toBe(true); // reasons first
    fireEvent.change(within(open).getByLabelText(/(Your reasons|Deine Begründung)/), { target: { value: "fraudulent offers" } });
    fireEvent.click(suspend);
    await waitFor(() => expect(mocks.api.decide).toHaveBeenCalledWith("n1", "suspend", "fraudulent offers"));
  });
});
