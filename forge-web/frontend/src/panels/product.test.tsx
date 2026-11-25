import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProjectApi } from "../api/project";
import { ProductPanel } from "./Product";

afterEach(cleanup);

const api = (info: unknown) => ({ product: vi.fn(async () => info) }) as unknown as ProjectApi;

describe("the product tab", () => {
  it("says what the agent made and where it runs", async () => {
    const info = {
      product: true, name: "lehm", kind: "fullstack", clients: ["web", "android"], payments: true, database: true, hosted: true,
      staging_url: "https://lehm.staging.apps.example", production_url: "",
      deploys: [{ environment: "staging", status: "live", step: "done", commit: "abcdef1234", error: "", created_at: 1 }],
    };
    render(<ProductPanel api={api(info)} refreshKey={0} onError={vi.fn()} />);
    expect(await screen.findByText("lehm")).toBeTruthy();
    expect(screen.getByText(/Android/).textContent).toMatch(/Web · Android/);
    expect(screen.getByRole("link", { name: "https://lehm.staging.apps.example" })).toBeTruthy();
    expect(screen.getByText("abcdef1")).toBeTruthy();
  });

  it("invites a first request when there is no product yet", async () => {
    render(<ProductPanel api={api({ product: false })} refreshKey={0} onError={vi.fn()} />);
    expect(await screen.findByText(/No product yet|Noch kein Produkt/)).toBeTruthy();
  });
});
