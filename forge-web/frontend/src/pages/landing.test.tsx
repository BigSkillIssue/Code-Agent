import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SignedOut } from "../App";
import type { AuthConfig } from "./Auth";
import LandingPage, { type PublicPlans } from "./Landing";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const config: AuthConfig = { setup_needed: false, passwords: true, signup: "open", providers: [], mail: false, dev: false };
const offer: PublicPlans = {
  trial: { days: 14, credit_usd: 5 },
  plans: [
    {
      id: "maker", kind: "plan", name: "Maker", description: "For one product", currency: "eur",
      amounts: { month: 1900, year: 19000 }, trial_days: 0,
      limits: { projects: 3, ai_budget_month_eur: 20, ai_budget_per_product_eur: null, mac_minutes: null, hosted_apps: 1, storage_gb: 5, custom_domains: 0, store_publishing: false },
    },
    {
      id: "studio", kind: "plan", name: "Studio", description: "", currency: "eur",
      amounts: { month: 4900 }, trial_days: 0,
      limits: { projects: null, ai_budget_month_eur: 60, ai_budget_per_product_eur: null, mac_minutes: 300, hosted_apps: 5, storage_gb: 20, custom_domains: 3, store_publishing: true },
    },
  ],
};

/** fetch that answers the public plans and records every URL asked for. */
function serve(answer: PublicPlans = offer) {
  const asked: string[] = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    asked.push(url);
    return new Response(JSON.stringify(answer), { status: 200, headers: { "Content-Type": "application/json" } });
  }));
  return asked;
}

function landing(path = "/") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SignedOut config={config} />
    </MemoryRouter>,
  );
}

describe("the start page", () => {
  it("shows the plans and the trial the server offers", async () => {
    const asked = serve();
    render(
      <MemoryRouter>
        <LandingPage config={config} />
      </MemoryRouter>,
    );
    const plans = await screen.findAllByTestId("landing-plan");
    expect(plans.map((p) => within(p).getByRole("heading").textContent)).toEqual(["Maker", "Studio"]);
    expect(within(plans[0]).getByText(/19[.,]00/)).toBeTruthy();
    expect(within(plans[0]).getByText(/190[.,]00/)).toBeTruthy(); // the yearly price
    expect(within(plans[1]).getByText(/49[.,]00/)).toBeTruthy();
    expect(within(screen.getByTestId("landing-prices")).getByText(/14/)).toBeTruthy();
    expect(asked).toEqual(["/api/public/plans"]);
  });

  it("says when there are no plans yet", async () => {
    serve({ plans: [], trial: null });
    render(
      <MemoryRouter>
        <LandingPage config={config} />
      </MemoryRouter>,
    );
    expect(await screen.findByText(/plans are being prepared|Tarife werden gerade eingerichtet/)).toBeTruthy();
    expect(screen.queryByTestId("landing-plan")).toBeNull();
  });

  it("keeps signing in one click away", async () => {
    serve();
    landing();
    await screen.findByTestId("landing");
    const signIn = screen.getAllByRole("link", { name: /^(Sign in|Anmelden)$/ });
    expect(signIn.length).toBeGreaterThan(0);
    expect(signIn.every((link) => link.getAttribute("href") === "/anmelden")).toBe(true);
    expect((await screen.findByRole("link", { name: /^(Try it free|Kostenlos testen)$/ })).getAttribute("href")).toBe("/signup");
    cleanup();
    landing("/anmelden");
    expect(screen.getByLabelText(/^(Email|E-Mail)$/)).toBeTruthy(); // the sign-in form
  });

  it("sends a provider's error back to the sign-in, not to the start page", () => {
    serve();
    window.history.replaceState(null, "", "/?auth_error=the%20sign-in%20was%20cancelled");
    landing("/?auth_error=the%20sign-in%20was%20cancelled");
    window.history.replaceState(null, "", "/");
    expect(screen.queryByTestId("landing")).toBeNull();
    expect(screen.getByRole("alert").textContent).toMatch(/cancelled|abgebrochen/);
  });

  it("opens and closes each answer", async () => {
    serve();
    landing();
    const question = await screen.findByRole("button", { name: /know how to code|programmieren/ });
    expect(question.getAttribute("aria-expanded")).toBe("false");
    expect(screen.queryByText(/You describe what you need|Du beschreibst, was du brauchst/)).toBeNull();
    fireEvent.click(question);
    expect(question.getAttribute("aria-expanded")).toBe("true");
    expect(screen.getByText(/You describe what you need|Du beschreibst, was du brauchst/)).toBeTruthy();
    fireEvent.click(question);
    expect(screen.queryByText(/You describe what you need|Du beschreibst, was du brauchst/)).toBeNull();
  });

  it("links only to this site, with the legal pages in the footer", async () => {
    const asked = serve();
    const { container } = landing();
    await screen.findAllByTestId("landing-plan");
    for (const question of screen.getAllByRole("button", { expanded: false })) fireEvent.click(question);
    const urls = Array.from(container.querySelectorAll("[href], [src], [action], [srcset]")).flatMap((node) =>
      ["href", "src", "action", "srcset"].map((name) => node.getAttribute(name)).filter((value): value is string => value !== null),
    );
    expect(urls.length).toBeGreaterThan(5);
    for (const url of urls) expect(url, url).toMatch(/^\/(?!\/)/);
    // No address of another host anywhere (an SVG's namespace is a name, not a request).
    expect(container.innerHTML.replaceAll('xmlns="http://www.w3.org/2000/svg"', "")).not.toMatch(/https?:\/\//);
    const footer = container.querySelector("footer")!;
    const hrefs = Array.from(footer.querySelectorAll("a")).map((a) => a.getAttribute("href"));
    expect(hrefs).toEqual(["/impressum", "/rechtstexte/privacy", "/rechtstexte/terms", "/kuendigen"]);
    expect(asked.every((url) => url.startsWith("/"))).toBe(true);
  });
});
