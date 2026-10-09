import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ServerSettings } from "../api/account";
import type { AppleJob, AppleUser, Mac } from "../api/apple";
import { useStore } from "../state/store";
import { AppleTab } from "./AdminApple";

const settings = {
  signup: "invite", allowed_domains: [], passwords: true, admin_two_factor: false, projects_per_user: 20,
  project_disk_mb: 10000, sandbox_cpus: 2, sandbox_memory: "4g", sandbox_pids: 1024, sandbox_idle_minutes: 30,
  server_keys_for: "granted", monthly_limit_usd: 20, apple_enabled: false, apple_allowed: "granted",
  apple_minutes_per_month: 600, apple_reviewer_model: "",
} satisfies ServerSettings;

const mac: Mac = { id: "m1", name: "mini-1", enabled: true, created_at: 1, last_seen: 2, online: true, version: "0.1.0" };
const bob: AppleUser = { id: "u2", email: "bob@example.com", name: "Bob", role: "member", allowed: false, minutes_this_month: 12.4 };
const job: AppleJob = {
  id: "j1", kind: "build", params: "{}", status: "failed", user: "bob@example.com", project: "Tally",
  worker_id: "m1", created_at: 3, seconds: 61, outcome: "build failed on ios",
};

const mocks = vi.hoisted(() => ({ admin: {} as Record<string, ReturnType<typeof vi.fn>>, apple: {} as Record<string, ReturnType<typeof vi.fn>> }));
vi.mock("../api/account", () => ({ admin: mocks.admin }));
vi.mock("../api/apple", () => ({ appleAdmin: mocks.apple }));

beforeEach(() => {
  Object.assign(mocks.admin, {
    settings: vi.fn(async () => settings),
    saveSettings: vi.fn(async (changes: Partial<ServerSettings>) => ({ ...settings, ...changes })),
  });
  Object.assign(mocks.apple, {
    macs: vi.fn(async () => [mac]),
    addMac: vi.fn(async (name: string) => ({ ...mac, id: "m2", name, online: false, token: "fmw_m2_secret" })),
    setMac: vi.fn(async () => ({ ...mac, enabled: false })),
    removeMac: vi.fn(async () => ({ ok: true })),
    users: vi.fn(async () => [bob]),
    grant: vi.fn(async () => ({})),
    jobs: vi.fn(async () => [job]),
  });
  useStore.setState({ error: "" });
});

afterEach(cleanup);

describe("Apple administration", () => {
  it("turns Apple builds on and picks the reviewer's model", async () => {
    render(<AppleTab />);
    const form = (await screen.findByLabelText(/Build Apple apps|Apple-Apps auf den Macs/)).closest("form")!;
    fireEvent.click(within(form).getByRole("checkbox"));
    fireEvent.change(within(form).getByRole("combobox"), { target: { value: "everyone" } });
    fireEvent.change(within(form).getByPlaceholderText("openai/gpt-5"), { target: { value: " openai/gpt-5 " } });
    fireEvent.click(within(form).getByRole("button", { name: /^(Save|Speichern)$/ }));
    await waitFor(() =>
      expect(mocks.admin.saveSettings).toHaveBeenCalledWith({ apple_enabled: true, apple_allowed: "everyone", apple_reviewer_model: "openai/gpt-5" }),
    );
  });

  it("adds a Mac and shows its token with the command to run, once", async () => {
    render(<AppleTab />);
    const row = (await screen.findByText("mini-1")).closest("li")!;
    expect(row.textContent).toMatch(/online|verbunden/);
    fireEvent.change(screen.getByPlaceholderText("mac-mini-1"), { target: { value: "studio" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Add Mac|Mac hinzufügen)$/ }));
    const shown = await screen.findByTestId("mac-token");
    expect(shown.textContent).toContain("fmw_m2_secret");
    expect(shown.textContent).toContain(`forge-mac-worker run --server ${window.location.origin} --token-file`);
    expect(mocks.apple.addMac).toHaveBeenCalledWith("studio");
    fireEvent.click(within(row).getByRole("button", { name: /^(Disable|Sperren|Deaktivieren)$/ }));
    await waitFor(() => expect(mocks.apple.setMac).toHaveBeenCalledWith("m1", false));
  });

  it("grants Apple builds per user and lists the jobs", async () => {
    render(<AppleTab />);
    fireEvent.click(await screen.findByRole("checkbox", { name: /Bob/ }));
    await waitFor(() => expect(mocks.apple.grant).toHaveBeenCalledWith("u2", true));
    const failed = (await screen.findByText(/^(failed|fehlgeschlagen)$/)).closest("tr")!;
    expect(failed.textContent).toContain("Tally");
    expect(within(failed).getByTitle("build failed on ios")).toBeTruthy();
    expect(failed.textContent).toContain("61 s");
  });
});
