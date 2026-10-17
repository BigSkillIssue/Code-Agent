import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { MyBilling, Offer } from "../api/billing";
import { useStore } from "../state/store";
import { BillingPage } from "./Billing";
import { features } from "./Plans";

const pro: Offer = {
  id: "pro", kind: "plan", name: "Pro", description: "For one maker", currency: "eur",
  amounts: { month: 2900, year: 29000 }, trial_days: 14,
  limits: { projects: 10, ai_budget_month_eur: 50, ai_budget_per_product_eur: 20, mac_minutes: null, hosted_apps: 3,
    storage_gb: null, custom_domains: 2, store_publishing: true },
};
const mine: MyBilling = {
  enabled: true, subscriptions: [], apps: [],
  entitlements: { source: "server", projects: 20, ai_month_usd: null, ai_product_usd: null, server_keys: false,
    mac_minutes: 600, hosted_apps: null, storage_mb: 10000, custom_domains: null, store_publishing: true },
};

const noAccount = { enabled: true, connected: false, charges_enabled: false, payouts_enabled: false,
  details_submitted: false, fee_percent: 10 };

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
    connect: vi.fn(async () => ({ ...noAccount })),
    startConnect: vi.fn(async () => ({ url: "https://connect.stripe.com/setup/e/acct_1" })),
    connectDashboard: vi.fn(async () => ({ url: "https://connect.stripe.com/express/acct_1" })),
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
  it("shows the plans side by side, monthly or yearly, and orders only with the waiver", async () => {
    open();
    expect((await screen.findByTestId("my-limits")).textContent).toContain("20");
    const card = await screen.findByTestId("plan");
    expect(card.textContent).toMatch(/29[.,]00/);
    expect(card.textContent).toMatch(/14 (days free|Tage kostenlos)/);
    expect(card.textContent).toMatch(/10 (projects|Projekte)/); // what the plan includes
    expect(card.textContent).toMatch(/(App Store)/);
    expect(screen.getByText(/(save up to 17 %|spare bis zu 17 %)/)).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: /^(Yearly|Jährlich)$/ }));
    expect(card.textContent).toMatch(/24[.,]17/); // 290 a year is 24.17 a month
    expect(card.textContent).toMatch(/290[.,]00/);
    fireEvent.click(within(card).getByRole("button", { name: /^(Choose Pro|Pro wählen)$/ }));
    const dialog = screen.getByRole("dialog");
    const order = screen.getByRole("button", { name: /^(Order with obligation to pay|Zahlungspflichtig bestellen)$/ });
    expect((order as HTMLButtonElement).disabled).toBe(true); // the waiver first
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(order);
    await waitFor(() => expect(mocks.api.checkout).toHaveBeenCalledWith("pro", "year", true));
    await waitFor(() => expect(window.location.assign).toHaveBeenCalledWith("https://checkout.stripe.com/c/pay/cs_1"));
  });

  it("lists what a plan includes, and a limit it leaves out as no limit", () => {
    const lines = features({ projects: 3, hosted_apps: null, storage_gb: 5, custom_domains: 0, store_publishing: false });
    expect(lines).toEqual([
      expect.stringMatching(/^3 (projects|Projekte)$/),
      expect.stringMatching(/^(Unlimited hosted apps|Unbegrenzt viele gehostete Apps)$/),
      expect.stringMatching(/^5 GB/),
    ]); // no AI budget given, no domains, no stores
    expect(features({}).join(" ")).not.toMatch(/NaN|undefined/);
  });

  it("marks the current plan instead of offering it again", async () => {
    mocks.api.me.mockResolvedValue({ ...mine, entitlements: { ...mine.entitlements, source: "plan:pro" } });
    open();
    const card = await screen.findByTestId("plan");
    await waitFor(() => expect(card.textContent).toMatch(/(Your current plan|Dein aktueller Tarif)/));
    expect(within(card).queryByRole("button")).toBeNull();
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
    fireEvent.click(screen.getByRole("button", { name: /^(Choose Pro|Pro wählen)$/ }));
    const order = screen.getByRole("button", { name: /^(Order with obligation to pay|Zahlungspflichtig bestellen)$/ });
    fireEvent.click(screen.getByLabelText(/(right of withdrawal|Widerrufsrecht)/));
    expect((order as HTMLButtonElement).disabled).toBe(true); // the texts first
    expect(screen.getByText(/(accept the legal texts|Rechtstexten zu)/)).toBeTruthy();
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
    fireEvent.click(screen.getByRole("button", { name: /^(Choose Pro|Pro wählen)$/ }));
    fireEvent.click(screen.getByLabelText(/(right of withdrawal|Widerrufsrecht)/));
    const order = screen.getByRole("button", { name: /^(Order with obligation to pay|Zahlungspflichtig bestellen)$/ });
    expect((order as HTMLButtonElement).disabled).toBe(true);
  });

  it("orders an app's one-time price for one of the maker's own apps not paid for yet", async () => {
    const app: Offer = { ...pro, id: "app", kind: "app_once", name: "App", amounts: { once: 4900 }, trial_days: 0 };
    mocks.api.offers.mockResolvedValue([app]);
    const apps = [
      { project_id: "a1a1a1a1a1a1a1a1", name: "Blog", unlocked: true },
      { project_id: "b2b2b2b2b2b2b2b2", name: "Shop", unlocked: false },
    ];
    mocks.api.me.mockResolvedValue({ ...mine, apps });
    open();
    const offer = await screen.findByTestId("offer"); // an extra, below the plans
    expect(offer.textContent).toMatch(/49[.,]00.*(once|einmalig)/);
    expect(screen.getAllByTestId("app-paid").map((p) => p.textContent)).toEqual([
      expect.stringMatching(/^Blog: (paid for|bezahlt)$/),
      expect.stringMatching(/^Shop: (not paid|noch nicht)/),
    ]);
    fireEvent.click(within(offer).getByRole("button", { name: /^(Order|Bestellen)$/ }));
    const dialog = screen.getByRole("dialog");
    const which = within(dialog).getByRole("combobox", { name: /(For which app|Für welche App)/ });
    expect(which.textContent).toBe("Shop");
    fireEvent.click(which);
    expect(within(dialog).getAllByRole("option").map((o) => o.textContent)).toEqual(["Shop"]); // Blog is paid for
    fireEvent.click(which);
    fireEvent.click(within(dialog).getByRole("checkbox"));
    fireEvent.click(screen.getByRole("button", { name: /^(Order with obligation to pay|Zahlungspflichtig bestellen)$/ }));
    await waitFor(() => expect(mocks.api.checkout).toHaveBeenCalledWith("app", "once", true, "b2b2b2b2b2b2b2b2"));
  });

  it("checks a VAT ID with VIES", async () => {
    open();
    const field = await screen.findByLabelText(/^(VAT ID|USt-IdNr\.) \(DE123456789\)$/);
    fireEvent.change(field, { target: { value: " DE 123456789 " } });
    fireEvent.click(screen.getByRole("button", { name: /^(Check|Prüfen)$/ }));
    await waitFor(() => expect(legalMocks.api.vat).toHaveBeenCalledWith("DE 123456789"));
    expect((await screen.findByTestId("vat-result")).textContent).toContain("Muster GmbH");
  });

  it("connects a maker's own Stripe account for their apps' payments", async () => {
    open();
    expect((await screen.findByTestId("connect-state")).textContent).toMatch(/(No Stripe account|Noch kein Stripe-Konto)/);
    expect(screen.getByText(/10 ?%/)).toBeTruthy(); // the server's share is said before
    fireEvent.click(screen.getByRole("button", { name: /^(Connect a Stripe account|Stripe-Konto verbinden)$/ }));
    await waitFor(() => expect(window.location.assign).toHaveBeenCalledWith("https://connect.stripe.com/setup/e/acct_1"));
    cleanup();
    mocks.api.connect.mockResolvedValue({ ...noAccount, connected: true, charges_enabled: true, details_submitted: true });
    open();
    expect((await screen.findByTestId("connect-state")).textContent).toMatch(/(takes payments|nimmt Zahlungen an)/);
    fireEvent.click(screen.getByRole("button", { name: /^(Open your Stripe dashboard|Stripe-Übersicht öffnen)$/ }));
    await waitFor(() => expect(window.location.assign).toHaveBeenCalledWith("https://connect.stripe.com/express/acct_1"));
  });
});
