import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { CatalogItem } from "../api/billing";
import { useStore } from "../state/store";
import { BillingTab } from "./AdminBilling";

const pro: CatalogItem = {
  id: "pro", kind: "plan", name: "Pro", description: "", currency: "eur", amounts: { month: 2900, year: 29000 },
  trial_days: 14, archived_at: 0, stripe_product: "prod_1", prices: {}, synced: true,
  limits: { projects: 10, ai_budget_month_eur: 50, ai_budget_per_product_eur: null, mac_minutes: null,
    hosted_apps: 3, storage_gb: null, custom_domains: null, store_publishing: true },
};

const mocks = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>> }));
vi.mock("../api/billing", () => ({ billingAdmin: mocks.api }));

beforeEach(() => {
  Object.assign(mocks.api, {
    stripe: vi.fn(async () => ({ configured: true, mode: "test", hint: "c0de", updated_at: 1, enabled: true,
      webhook_url: "https://forge.example.com/api/billing/stripe/webhook" })),
    setKeys: vi.fn(async () => ({})),
    catalog: vi.fn(async () => ({ items: [pro], coupons: [] })),
    putItem: vi.fn(async () => ({})),
    removeItem: vi.fn(async () => undefined),
    putCoupon: vi.fn(async () => ({})),
    removeCoupon: vi.fn(async () => undefined),
    sync: vi.fn(async () => ({ created: ["product pro", "price pro/month"], updated: [], archived: [], unchanged: 2 })),
  });
  useStore.setState({ error: "" });
});

afterEach(cleanup);

describe("the billing panel", () => {
  it("shows only the keys' mode and last characters, and takes new keys", async () => {
    render(<BillingTab />);
    expect((await screen.findByTestId("stripe-keys")).textContent).toContain("c0de");
    expect(screen.getByText(/api\/billing\/stripe\/webhook/)).toBeTruthy();
    const secret = screen.getByLabelText(/(Secret key|Geheimer Schlüssel)/) as HTMLInputElement;
    expect(secret.type).toBe("password");
    fireEvent.change(secret, { target: { value: " sk_test_new " } });
    fireEvent.change(screen.getByLabelText(/(signing secret|Signaturgeheimnis)/), { target: { value: "whsec_new" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Save the keys|Schlüssel speichern)$/ }));
    await waitFor(() => expect(mocks.api.setKeys).toHaveBeenCalledWith("sk_test_new", "whsec_new"));
  });

  it("saves a plan with its amounts in cents and its limits", async () => {
    render(<BillingTab />);
    await screen.findByTestId("catalog-item");
    const form = screen.getByTestId("item-form");
    fireEvent.change(within(form).getByLabelText(/(Name for the link|Name für den Link)/), { target: { value: "team" } });
    fireEvent.change(within(form).getByLabelText(/^(Name customers see|Name für Kunden)$/), { target: { value: "Team" } });
    fireEvent.change(within(form).getByLabelText(/^(Per month|Pro Monat)$/), { target: { value: "49,50" } });
    fireEvent.change(within(form).getByLabelText(/^(Projects|Projekte)$/), { target: { value: "25" } });
    fireEvent.click(within(form).getByRole("button", { name: /^(Save the item|Eintrag speichern)$/ }));
    await waitFor(() => expect(mocks.api.putItem).toHaveBeenCalled());
    const [itemId, body] = mocks.api.putItem.mock.calls[0];
    expect(itemId).toBe("team");
    expect(body).toMatchObject({ kind: "plan", name: "Team", amounts: { month: 4950 }, trial_days: 0 });
    expect(body.limits).toMatchObject({ projects: 25, hosted_apps: null, store_publishing: false });
  });

  it("loads an item to change it, removes one and syncs with Stripe", async () => {
    render(<BillingTab />);
    const row = await screen.findByTestId("catalog-item");
    expect(row.textContent).toContain("29.00 EUR");
    fireEvent.click(within(row).getByRole("button", { name: "Pro" }));
    const form = screen.getByTestId("item-form");
    expect((within(form).getByLabelText(/^(Per year|Pro Jahr)$/) as HTMLInputElement).value).toBe("290");
    expect((within(form).getByLabelText(/^(Hosted apps|Gehostete Apps)$/) as HTMLInputElement).value).toBe("3");
    fireEvent.click(within(row).getByRole("button", { name: /^(Remove|Entfernen)$/ }));
    await waitFor(() => expect(mocks.api.removeItem).toHaveBeenCalledWith("pro"));
    fireEvent.click(screen.getByRole("button", { name: /^(Sync with Stripe|Mit Stripe abgleichen)$/ }));
    expect((await screen.findByTestId("sync-report")).textContent).toMatch(/2 (made|angelegt)/);
  });

  it("adds a coupon for some months", async () => {
    render(<BillingTab />);
    await screen.findByTestId("coupons");
    fireEvent.change(screen.getByLabelText(/^Code/), { target: { value: "launch_25" } });
    fireEvent.change(screen.getByLabelText(/^Name$/), { target: { value: "Launch" } });
    fireEvent.change(screen.getByLabelText(/(Percent off|Prozent Rabatt)/), { target: { value: "25" } });
    fireEvent.change(screen.getByLabelText(/^(For|Gilt für)$/), { target: { value: "repeating" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Add the coupon|Gutschein anlegen)$/ }));
    await waitFor(() =>
      expect(mocks.api.putCoupon).toHaveBeenCalledWith(
        "launch-25",
        expect.objectContaining({ code: "LAUNCH_25", percent_off: 25, duration: "repeating", duration_in_months: 3 }),
      ),
    );
  });
});
