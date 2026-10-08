import { cleanup, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it } from "vitest";
import { AuthPages, type AuthConfig } from "./Auth";

afterEach(() => {
  cleanup();
  window.history.replaceState(null, "", "/");
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
