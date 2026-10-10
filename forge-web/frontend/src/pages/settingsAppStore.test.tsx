import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../state/store";
import { AppStoreSection } from "./SettingsAppStore";

const mocks = vi.hoisted(() => ({ get: vi.fn(), save: vi.fn(), check: vi.fn(), remove: vi.fn() }));
vi.mock("../api/apple", () => ({ appStoreKey: mocks }));

const view = {
  key_id: "ABC123DEFG", issuer_id: "69a6de7e-1111-47e3-e053-5b8c7c11a4d1", team_id: "TEAM123456",
  created_at: 1, checked_at: 2, check_ok: true, check_message: "The key works: 1 app in App Store Connect.",
};

beforeEach(() => {
  mocks.get.mockReset().mockResolvedValue(null);
  mocks.save.mockReset().mockResolvedValue(view);
  mocks.check.mockReset().mockResolvedValue({ ...view, check_ok: false, check_message: "This API key cannot use the Provisioning endpoints." });
  mocks.remove.mockReset().mockResolvedValue(undefined);
  useStore.setState({ error: "" });
});

afterEach(cleanup);

describe("the App Store Connect key", () => {
  it("is saved from the .p8 file and its check is shown, never the key", async () => {
    render(<AppStoreSection />);
    fireEvent.change(await screen.findByPlaceholderText("ABC123DEFG"), { target: { value: " ABC123DEFG " } });
    fireEvent.change(screen.getByPlaceholderText("TEAM123456"), { target: { value: "TEAM123456" } });
    fireEvent.change(screen.getByLabelText(/^(Issuer ID|Aussteller-ID \(Issuer ID\))$/), { target: { value: view.issuer_id } });
    const pem = "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n";
    const file = new File([pem], "AuthKey_ABC123DEFG.p8");
    fireEvent.change(screen.getByLabelText(/AuthKey_….p8/), { target: { files: [file] } });
    mocks.get.mockResolvedValue(view);
    fireEvent.submit(screen.getByRole("button", { name: /^(Save|Speichern)$/ }).closest("form")!);
    await waitFor(() =>
      expect(mocks.save).toHaveBeenCalledWith({ key_id: "ABC123DEFG", issuer_id: view.issuer_id, team_id: "TEAM123456", private_key: pem }),
    );
    expect((await screen.findByTestId("appstore-key")).textContent).toContain("The key works");
    expect(document.body.textContent).not.toContain("PRIVATE KEY");
  });

  it("checks the key again and shows what Apple says", async () => {
    mocks.get.mockResolvedValue(view);
    render(<AppStoreSection />);
    mocks.get.mockResolvedValue({ ...view, check_ok: false, check_message: "This API key cannot use the Provisioning endpoints." });
    fireEvent.click(await screen.findByRole("button", { name: /^(Check|Prüfen)$/ }));
    await waitFor(() => expect(screen.getByTestId("appstore-key").textContent).toContain("Provisioning"));
    expect(mocks.check).toHaveBeenCalled();
  });
});
