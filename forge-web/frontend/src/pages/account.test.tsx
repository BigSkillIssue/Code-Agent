import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Me, ServerSettings } from "../api/account";
import { useStore } from "../state/store";
import { AdminPage } from "./Admin";
import { changedSettings } from "./AdminServer";
import { grouped, SettingsPage } from "./Settings";

const me: Me = {
  id: "u1", email: "ada@example.com", name: "Ada", role: "admin", default_model: "",
  has_password: true, totp_enabled: false, two_factor_required: true,
};

const serverSettings: ServerSettings = {
  signup: "invite", allowed_domains: [], passwords: true, admin_two_factor: false,
  projects_per_user: 20, project_disk_mb: 10000, sandbox_cpus: 2, sandbox_memory: "4g",
  sandbox_pids: 1024, sandbox_idle_minutes: 30, server_keys_for: "granted", monthly_limit_usd: 20,
};

const mocks = vi.hoisted(() => ({ account: {} as Record<string, ReturnType<typeof vi.fn>>, admin: {} as Record<string, ReturnType<typeof vi.fn>> }));

vi.mock("../api/account", () => ({ account: mocks.account, admin: mocks.admin }));
vi.mock("../api/client", async (original) => ({
  ...(await original<typeof import("../api/client")>()),
  api: { get: vi.fn(async () => ({ passwords: true, providers: [{ name: "github", label: "GitHub" }] })) },
}));

beforeEach(() => {
  Object.assign(mocks.account, {
    me: vi.fn(async () => me),
    models: vi.fn(async () => [{ id: "anthropic/claude-sonnet-4-5", provider: "anthropic", model: "claude-sonnet-4-5", key: "server" }]),
    update: vi.fn(async (changes: Partial<Me>) => ({ ...me, ...changes })),
    changePassword: vi.fn(async () => ({ ok: true })),
    totpSetup: vi.fn(async () => ({ secret: "ABCDEFGHIJKLMNOP", uri: "otpauth://totp/Forge:ada?secret=ABCDEFGHIJKLMNOP" })),
    totpEnable: vi.fn(async () => ({ recovery_codes: ["aaaa-bbbb", "cccc-dddd"] })),
    totpDisable: vi.fn(async () => ({})),
    identities: vi.fn(async () => []),
    keys: vi.fn(async () => []),
    providers: vi.fn(async () => [{ name: "anthropic", own_key: false, server_key: true }]),
    gitCredentials: vi.fn(async () => []),
    sessions: vi.fn(async () => [{ id: "s1", current: true, ip: "127.0.0.1", user_agent: "test", last_seen_at: 1 }]),
    usage: vi.fn(async () => ({ server_keys_usd: 1.5, limit_usd: 20, by_model: [] })),
  });
  Object.assign(mocks.admin, {
    users: vi.fn(async () => [
      { id: "u1", email: "ada@example.com", name: "Ada", role: "admin", status: "active", created_at: 1, has_password: true, totp_enabled: true },
      { id: "u2", email: "bob@example.com", name: "Bob", role: "member", status: "pending", created_at: 1, has_password: true, totp_enabled: false },
    ]),
    grants: vi.fn(async () => [{ user_id: "u2", allowed: false, monthly_limit_usd: null }]),
    updateUser: vi.fn(async () => ({})),
    approve: vi.fn(async () => ({})),
    resetLink: vi.fn(async () => ({ link: "http://forge/reset#token=xyz" })),
    resetTwoFactor: vi.fn(async () => ({})),
    setGrant: vi.fn(async () => ({})),
    settings: vi.fn(async () => serverSettings),
    saveSettings: vi.fn(async (changes: Partial<ServerSettings>) => ({ ...serverSettings, ...changes })),
  });
  useStore.setState({ user: { id: "u1", email: "ada@example.com", name: "Ada", role: "admin" }, error: "" });
});

afterEach(cleanup);

const inRouter = (page: React.ReactNode) => render(<MemoryRouter>{page}</MemoryRouter>);

describe("settings page", () => {
  it("saves the name and the default model", async () => {
    inRouter(<SettingsPage />);
    const profile = (await screen.findByText(/^(Profile|Profil)$/)).closest("section")!;
    fireEvent.change(within(profile).getByRole("textbox"), { target: { value: "Ada L." } });
    await within(profile).findByRole("option", { name: "anthropic/claude-sonnet-4-5" });
    fireEvent.change(within(profile).getByRole("combobox"), { target: { value: "anthropic/claude-sonnet-4-5" } });
    fireEvent.click(within(profile).getByRole("button", { name: /^(Save|Speichern)$/ }));
    await waitFor(() =>
      expect(mocks.account.update).toHaveBeenCalledWith({ name: "Ada L.", default_model: "anthropic/claude-sonnet-4-5" }),
    );
    expect(useStore.getState().user?.name).toBe("Ada L.");
  });

  it("sets up two-factor sign-in and shows the recovery codes once", async () => {
    inRouter(<SettingsPage />);
    expect(await screen.findByRole("alert")).toBeTruthy(); // required for admins: a hint at the top
    fireEvent.click(await screen.findByRole("button", { name: /^(Set up two-factor sign-in|Zwei-Faktor-Anmeldung einrichten)$/ }));
    expect((await screen.findByTestId("totp-secret")).textContent).toBe(grouped("ABCDEFGHIJKLMNOP"));
    expect(screen.getByRole("link", { name: /^(Open in the authenticator app|In der Authenticator-App öffnen)$/ }).getAttribute("href")).toMatch(/^otpauth:/);
    const section = screen.getByTestId("totp-secret").closest("section")!;
    fireEvent.change(within(section).getByRole("textbox"), { target: { value: "123456" } });
    fireEvent.click(within(section).getByRole("button", { name: /^(Turn on|Einschalten)$/ }));
    expect((await screen.findByTestId("recovery-codes")).textContent).toContain("aaaa-bbbb");
    expect(mocks.account.totpEnable).toHaveBeenCalledWith("123456");
  });
});

describe("admin page", () => {
  it("is for admins only", () => {
    useStore.setState({ user: { id: "u2", email: "bob@example.com", name: "Bob", role: "member" } });
    inRouter(<AdminPage />);
    expect(screen.getByText(/admins|Admins/)).toBeTruthy();
    expect(mocks.admin.users).not.toHaveBeenCalled();
  });

  it("approves accounts, hands out reset links and grants server keys", async () => {
    inRouter(<AdminPage />);
    const bob = (await screen.findByText("bob@example.com")).closest("tr")!;
    fireEvent.click(within(bob).getByRole("button", { name: /^(Approve|Freischalten)$/ }));
    await waitFor(() => expect(mocks.admin.approve).toHaveBeenCalledWith("u2"));
    fireEvent.click(within(bob).getByRole("button", { name: /^(Reset link|Link zum Zurücksetzen)$/ }));
    expect((await screen.findByTestId("reset-link")).textContent).toContain("token=xyz");
    fireEvent.click(within(bob).getByRole("checkbox"));
    await waitFor(() => expect(mocks.admin.setGrant).toHaveBeenCalledWith("u2", true, null));
    fireEvent.change(within(bob).getByRole("combobox", { name: /^(Role|Rolle)$/ }), { target: { value: "admin" } });
    await waitFor(() => expect(mocks.admin.updateUser).toHaveBeenCalledWith("u2", { role: "admin" }));
  });

  it("saves only the server settings that changed", async () => {
    inRouter(<AdminPage />);
    fireEvent.click(await screen.findByRole("tab", { name: /^(Settings|Einstellungen)$/ }));
    const form = await screen.findByTestId("server-settings");
    fireEvent.change(within(form).getAllByRole("combobox")[0], { target: { value: "open" } });
    fireEvent.change(within(form).getByLabelText(/email domains|E-Mail-Domains/), { target: { value: "example.com, firma.de" } });
    fireEvent.click(within(form).getByRole("button", { name: /^(Save|Speichern)$/ }));
    await waitFor(() =>
      expect(mocks.admin.saveSettings).toHaveBeenCalledWith({ signup: "open", allowed_domains: ["example.com", "firma.de"] }),
    );
  });

  it("finds changed fields", () => {
    expect(changedSettings(serverSettings, { ...serverSettings })).toEqual({});
    expect(changedSettings(serverSettings, { ...serverSettings, sandbox_memory: "2g" })).toEqual({ sandbox_memory: "2g" });
  });
});
