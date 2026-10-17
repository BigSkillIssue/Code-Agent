import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CustomerDetail, CustomerRow } from "../api/customers";
import { useStore } from "../state/store";
import { CustomersTab } from "./AdminCustomers";

const maria: CustomerRow = { id: "u1", email: "maria@example.com", name: "Maria", role: "member", status: "active",
  paying: ["pro"], gift: "", dunning: "suspended", held: false };
const detail: CustomerDetail = {
  id: "u1", email: "maria@example.com", name: "Maria", role: "member", stripe_customer: "cus_1",
  entitlements: { source: "plan:pro", projects: 5, ai_month_usd: null, ai_product_usd: null, server_keys: true,
    mac_minutes: null, hosted_apps: 2, storage_mb: null, custom_domains: 1, store_publishing: false },
  subscriptions: [{ item_id: "pro", status: "active", interval: "month", quantity: 1, current_period_end: 1_900_000_000,
    stripe_subscription: "sub_1" }],
  orders: [{ id: "cs_1", item_id: "pro", interval: "month", status: "complete", created_at: 1_760_000_000,
    completed_at: 1_760_000_100, project_id: "", trial_days: 0 }],
  apps: [{ project_id: "p1p1p1p1p1p1p1p1", name: "Shop", unlocked: false, by: "" }],
  dunning: { id: "d1", stage: "suspended", opened_at: 1_760_000_000, stage_at: 1_761_000_000, closed_at: 0, held_at: 0,
    invoices: '["in_1"]' },
  gift: null,
  cancellations: [],
};
const waiting = { id: "c1", received_at: 1_760_000_000, name: "Maria", email: "maria@example.com", contract: "",
  extraordinary: true, reason: "moved abroad", status: "manual", effective_at: 0, user_id: "u1" };

const mocks = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>>,
  billing: {} as Record<string, ReturnType<typeof vi.fn>> }));
vi.mock("../api/customers", () => ({ customersAdmin: mocks.api }));
vi.mock("../api/billing", () => ({ billingAdmin: mocks.billing }));

beforeEach(() => {
  Object.assign(mocks.api, {
    list: vi.fn(async () => [maria]),
    customer: vi.fn(async () => detail),
    give: vi.fn(async () => ({})),
    takeBack: vi.fn(async () => undefined),
    end: vi.fn(async () => ({})),
    dunning: vi.fn(async () => ({})),
    app: vi.fn(async () => ({})),
    cancellations: vi.fn(async () => ({ waiting: [waiting], recent: [] })),
    carryOut: vi.fn(async () => ({ ...waiting, status: "done" })),
  });
  Object.assign(mocks.billing, {
    catalog: vi.fn(async () => ({ items: [{ id: "pro", kind: "plan", name: "Pro", archived_at: 0 },
      { id: "more", kind: "addon", name: "More", archived_at: 0 }], coupons: [] })),
  });
  useStore.setState({ error: "" });
});

afterEach(cleanup);

describe("the customers panel", () => {
  it("lists the customers, searches them, and carries out a cancellation that waits", async () => {
    render(<CustomersTab />);
    const row = await screen.findByTestId("customer");
    expect(row.textContent).toContain("maria@example.com");
    expect(row.textContent).toMatch(/(apps suspended|Apps angehalten)/);
    fireEvent.change(screen.getByLabelText(/(Search by name|Nach Name)/), { target: { value: "mar" } });
    await waitFor(() => expect(mocks.api.list).toHaveBeenLastCalledWith("mar"));
    const cancellation = await screen.findByTestId("waiting-cancellation");
    expect(cancellation.textContent).toMatch(/moved abroad/);
    fireEvent.click(within(cancellation).getByRole("button", { name: /^(End now|Sofort beenden)$/ }));
    await waitFor(() => expect(mocks.api.carryOut).toHaveBeenCalledWith("c1", "now"));
  });

  it("opens a customer and gives a plan, ends a subscription, unlocks an app and holds dunning", async () => {
    render(<CustomersTab />);
    fireEvent.click(await screen.findByRole("button", { name: /^(Open|Öffnen)$/ }));
    const shown = await screen.findByTestId("customer-detail");
    expect(shown.textContent).toContain("plan:pro");
    const gift = within(shown).getByTestId("gift");
    const plan = within(gift).getByRole("combobox", { name: /^(Plan|Tarif)$/ });
    await waitFor(() => expect(plan.textContent).toBe("Pro")); // plans only, the add-on is not offered
    fireEvent.click(plan);
    expect(within(gift).getAllByRole("option").map((o) => o.textContent)).toEqual(["Pro"]);
    fireEvent.click(plan);
    fireEvent.change(within(gift).getByLabelText(/(Note for the admins|Notiz für die Admins)/), { target: { value: " partner " } });
    fireEvent.click(within(gift).getByRole("button", { name: /^(Give the plan|Tarif schenken)$/ }));
    await waitFor(() => expect(mocks.api.give).toHaveBeenCalledWith("u1", "pro", 0, "partner"));
    const sub = within(shown).getByTestId("subscription");
    fireEvent.click(within(sub).getByRole("button", { name: /^(End now|Sofort beenden)$/ }));
    await waitFor(() => expect(mocks.api.end).toHaveBeenCalledWith("u1", "sub_1", true));
    fireEvent.click(within(shown).getByRole("button", { name: /^(Unlock for free|Kostenlos freischalten)$/ }));
    await waitFor(() => expect(mocks.api.app).toHaveBeenCalledWith("u1", "p1p1p1p1p1p1p1p1", "unlock"));
    const dunning = within(shown).getByTestId("dunning");
    expect(dunning.textContent).toMatch(/(Stage apps suspended|Stufe Apps angehalten)/);
    fireEvent.click(within(dunning).getByRole("button", { name: /^(Hold|Anhalten)$/ }));
    await waitFor(() => expect(mocks.api.dunning).toHaveBeenCalledWith("u1", "hold"));
    expect(mocks.api.customer.mock.calls.length).toBeGreaterThan(1); // shown again after each change
  });
});
