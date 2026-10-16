import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyBilling, Offer } from "../api/billing";
import { useStore } from "../state/store";
import { BillingPage } from "./Billing";

const pro: Offer = {
  id: "pro", kind: "plan", name: "Pro", description: "For one maker", currency: "eur",
  amounts: { month: 2900, year: 29000 }, trial_days: 14,
  limits: { projects: 10, ai_budget_month_eur: 50, ai_budget_per_product_eur: 20, mac_minutes: null, hosted_apps: 3,
    storage_gb: null, custom_domains: 2, store_publishing: true },
};
const mine: MyBilling = {
  enabled: true, subscriptions: [],
  entitlements: { source: "server", projects: 20, ai_month_usd: null, ai_product_usd: null, server_keys: false,
    mac_minutes: 600, hosted_apps: null, storage_mb: 10000, custom_domains: null, store_publishing: true },
};

const mocks = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>> }));
vi.mock("../api/billing", () => ({ billing: mocks.api }));
const legalMocks = vi.hoisted(() => ({ api: {} as Record<string, ReturnType<typeof vi.fn>> }));
vi.mock("../api/legal", async (original) => ({ ...(await original<object>()), legal: legalMocks.api }));

beforeEach(() => {
  Object.assign(mocks.api, {
    me: vi.fn(async () => mine),
    offers: vi.fn(async () => [pro]),
    checkout: vi.fn(async () => ({ url: "https://checkout.stripe.com/c/pay/cs_1" })),
    cancel: vi.fn(async () => ({ id: "x1", status: "done", received_at: 1_760_000_000, effective_at: 1_900_000_000 })),
    portal: vi.fn(async () => ({ url: "https://billing.stripe.com/p/session/bps_1" })),
  });
  Object.assign(legalMocks.api, {
    me: vi.fn(async () => ({ missing: [], vat: null })),
    accept: vi.fn(async () => ({ accepted: true })),
    vat: vi.fn(async () => ({ vat_id: "DE123456789", valid: true, name: "Muster GmbH", address: "Berlin" })),
  });
  useStore.setState({ error: "" });
  vi.stubGlobal("location", { ...window.location, assign: vi.fn() });
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function open(path = "/billing") {
  render(
    <MemoryRouter initialEntries={[path]}>
      <BillingPage />
    </MemoryRouter>,
  );
}

describe("the billing page", () => {
  it("shows the limits, and orders only with the waiver, by a button that says it costs money", async () => {
    open();
    expect((await screen.findByTestId("my-limits")).textContent).toContain("20");
    const offer = await screen.findByTestId("offer");
    expect(offer.textContent).toContain("29.00 EUR");
    expect(offer.textContent).toMatch(/14 (days free|Tage kostenlos)/);
    const order = screen.getByRole("button", { name: /^(Order with obligation to pay|Zahlungspflichtig bestellen)$/ });
    expect((order as HTMLButtonElement).disabled).toBe(true); // the waiver first
    fireEvent.click(screen.getAllByRole("radio")[1]); // yearly
    fireEvent.click(screen.getByRole("checkbox"));
    fireEvent.click(order);
    await waitFor(() => expect(mocks.api.checkout).toHaveBeenCalledWith("pro", "year", true));
    await waitFor(() => expect(window.location.assign).toHaveBeenCalledWith("https://checkout.stripe.com/c/pay/cs_1"));
  });

  it("says when payments are not set up and offers nothing then", async () => {
    mocks.api.me.mockResolvedValue({ ...mine, enabled: false });
    open("/billing?cancelled=1");
    expect(await screen.findByText(/(not set up|nicht eingerichtet)/)).toBeTruthy();
    expect(screen.getByText(/(cancelled|abgebrochen)/)).toBeTruthy();
    expect(screen.queryByTestId("offer")).toBeNull();
  });

  it("cancels in two steps without signing in again, and opens Stripe's portal", async () => {
    const subscriptions = [{ item_id: "pro", status: "active", interval: "month", quantity: 1, current_period_end: 1_900_000_000 }];
    mocks.api.me.mockResolvedValue({ ...mine, subscriptions });
    open();
    fireEvent.click(await screen.findByRole("button", { name: /^(Cancel contracts here|Verträge hier kündigen)$/ }));
    expect(mocks.api.cancel).not.toHaveBeenCalled(); // the first step only shows the second
    fireEvent.click(screen.getByRole("button", { name: /^(Cancel now|Jetzt kündigen)$/ }));
    await waitFor(() => expect(mocks.api.cancel).toHaveBeenCalledWith(""));
    expect((await screen.findByTestId("cancelled")).textContent).toMatch(/(takes effect|wird zum)/);
    fireEvent.click(screen.getByRole("button", { name: /^(Payment method and invoices|Zahlungsart und Rechnungen)$/ }));
    await waitFor(() => expect(window.location.assign).toHaveBeenCalledWith("https://billing.stripe.com/p/session/bps_1"));
  });

  it("asks for the current legal texts before the order, each with a link to read it", async () => {
    const missing = [
      { kind: "terms", version: 2, title: "AGB" },
      { kind: "privacy", version: 1, title: "Datenschutz" },
    ];
    legalMocks.api.me.mockResolvedValueOnce({ missing, vat: null }).mockResolvedValue({ missing: [], vat: null });
    open();
    const consent = await screen.findByTestId("consent");
    expect(consent.querySelector('a[href="/rechtstexte/terms"]')).toBeTruthy();
    const order = screen.getByRole("button", { name: /^(Order with obligation to pay|Zahlungspflichtig bestellen)$/ });
    fireEvent.click(screen.getByLabelText(/(right of withdrawal|Widerrufsrecht)/));
    expect((order as HTMLButtonElement).disabled).toBe(true); // the texts first
    const accept = screen.getByRole("button", { name: /^(Accept|Zustimmen)$/ });
    fireEvent.click(screen.getByLabelText(/AGB \((version|Version) 2\)/));
    expect((accept as HTMLButtonElement).disabled).toBe(true); // every text, not only one
    fireEvent.click(screen.getByLabelText(/Datenschutz \((version|Version) 1\)/));
    fireEvent.click(accept);
    await waitFor(() => expect(legalMocks.api.accept).toHaveBeenCalledTimes(2));
    expect(legalMocks.api.accept).toHaveBeenCalledWith("terms", 2);
    expect(legalMocks.api.accept).toHaveBeenCalledWith("privacy", 1);
    await waitFor(() => expect(screen.queryByTestId("consent")).toBeNull());
    expect((order as HTMLButtonElement).disabled).toBe(false);
  });

  it("says which text is not published yet, and orders nothing then", async () => {
    legalMocks.api.me.mockResolvedValue({ missing: [{ kind: "aup", version: 0, title: "aup" }], vat: null });
    open();
    expect((await screen.findByTestId("consent")).textContent).toMatch(/(not published yet|noch nicht veröffentlicht)/);
    expect(screen.queryByRole("button", { name: /^(Accept|Zustimmen)$/ })).toBeNull();
    fireEvent.click(screen.getByLabelText(/(right of withdrawal|Widerrufsrecht)/));
    const order = screen.getByRole("button", { name: /^(Order with obligation to pay|Zahlungspflichtig bestellen)$/ });
    expect((order as HTMLButtonElement).disabled).toBe(true);
  });

  it("checks a VAT ID with VIES", async () => {
    open();
    const field = await screen.findByLabelText(/^(VAT ID|USt-IdNr\.) \(DE123456789\)$/);
    fireEvent.change(field, { target: { value: " DE 123456789 " } });
    fireEvent.click(screen.getByRole("button", { name: /^(Check|Prüfen)$/ }));
    await waitFor(() => expect(legalMocks.api.vat).toHaveBeenCalledWith("DE 123456789"));
    expect((await screen.findByTestId("vat-result")).textContent).toContain("Muster GmbH");
  });
});
