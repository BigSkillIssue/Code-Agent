import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { AuthPages, type AuthConfig } from "./Auth";

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
  vi.unstubAllGlobals();
});

const config: AuthConfig = {
  setup_needed: false,
  passwords: true,
  signup: "invite",
  providers: [
    { name: "google", label: "Google" },
    { name: "firma", label: "Firmen-Login" },
  ],
  mail: false,
  dev: false,
};

function show(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <AuthPages config={config} />
    </MemoryRouter>,
  );
}

describe("sign-in page", () => {
  it("offers every provider by its label", () => {
    show("/");
    const google = screen.getByText(/Google$/).closest("a");
    expect(google?.getAttribute("href")).toBe("/api/auth/oauth/google/start");
    expect(screen.getByText(/Firmen-Login$/)).toBeTruthy();
  });

  it("shows why a provider sign-in failed, as text", () => {
    window.history.replaceState(null, "", "/?auth_error=%3Cb%3Eno%3C%2Fb%3E%20verified%20email");
    const { container } = show("/");
    expect(screen.getByRole("alert").textContent).toBe("<b>no</b> verified email");
    expect(container.querySelector("b")).toBeNull();
    expect(window.location.search).toBe("");
  });

  it("carries an invite to the provider", () => {
    window.history.replaceState(null, "", "/signup#token=abc123");
    show("/signup");
    const google = screen.getByText(/Google$/).closest("a");
    expect(google?.getAttribute("href")).toBe("/api/auth/oauth/google/start?invite=abc123");
  });
});

describe("two-factor sign-in", () => {
  it("asks for the code after a right password, then signs in with it", async () => {
    const calls: [string, unknown][] = [];
    vi.stubGlobal(
      "fetch",
      vi.fn(async (path: string, init: RequestInit) => {
        calls.push([path, JSON.parse(String(init.body))]);
        const body = path === "/api/auth/login" ? { totp_required: true } : { id: "u1" };
        return new Response(JSON.stringify(body), { status: 200 });
      }),
    );
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    show("/");
    fireEvent.change(screen.getByLabelText(/^(Email|E-Mail)$/), { target: { value: "ada@example.com" } });
    fireEvent.change(screen.getByLabelText(/^(Password|Passwort)$/), { target: { value: "correct horse battery" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Sign in|Anmelden)$/ }));
    const code = await screen.findByLabelText(/recovery code|Wiederherstellungscode/);
    expect(assign).not.toHaveBeenCalled(); // no session yet
    fireEvent.change(code, { target: { value: " 123456 " } });
    fireEvent.click(screen.getByRole("button", { name: /^(Sign in|Anmelden)$/ }));
    await waitFor(() => expect(assign).toHaveBeenCalledWith("/"));
    expect(calls[1]).toEqual(["/api/auth/totp/verify", { code: "123456" }]);
  });

  it("opens the code step when a provider sign-in comes back for it", () => {
    show("/login?second_factor=1");
    expect(screen.getByLabelText(/recovery code|Wiederherstellungscode/)).toBeTruthy();
  });
});

describe("email confirmation", () => {
  it("confirms the address but leaves signing in to the person", async () => {
    window.history.replaceState(null, "", "/verify#token=abc123");
    const fetch = vi.fn(async () => new Response(JSON.stringify({ id: "u1", status: "active" }), { status: 200 }));
    vi.stubGlobal("fetch", fetch);
    const assign = vi.fn();
    vi.stubGlobal("location", { ...window.location, assign });
    show("/verify");
    expect((await screen.findByRole("status")).textContent).toMatch(/confirmed|bestätigt/);
    expect(screen.getByRole("link", { name: /^(Sign in|Anmelden)$/ }).getAttribute("href")).toBe("/");
    expect(assign).not.toHaveBeenCalled();
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
