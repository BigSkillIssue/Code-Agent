import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Project } from "../api/types";
import { NewProjectDialog } from "./NewProjectDialog";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const project: Project = { id: "p1", name: "App", role: "owner", source: "git", created_at: 0, updated_at: 0 };

function setup(isAdmin = false, create = vi.fn().mockResolvedValue(project), appleApps = false) {
  const onCreated = vi.fn();
  const forget = vi.fn();
  render(<NewProjectDialog isAdmin={isAdmin} appleApps={appleApps} create={create} forget={forget} onCreated={onCreated} onClose={vi.fn()} />);
  fireEvent.change(screen.getAllByRole("textbox")[0], { target: { value: " App " } });
  return { create, forget, onCreated };
}

const pick = (label: RegExp) => fireEvent.click(screen.getByLabelText(label));

describe("NewProjectDialog", () => {
  it("clones a git repository", async () => {
    const { create, onCreated } = setup();
    pick(/^(Git repository|Git-Repository)$/);
    fireEvent.change(screen.getByPlaceholderText("https://github.com/…"), { target: { value: "https://github.com/ada/app.git" } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("dialog").querySelector("form")!);
    });
    expect(create).toHaveBeenCalledWith({ name: "App", source: "git", url: "https://github.com/ada/app.git", folder: "" });
    expect(onCreated).toHaveBeenCalledWith(project);
  });

  it("uploads a ZIP after creating the project, and removes it again if that fails", async () => {
    const fetches: string[] = [];
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
      fetches.push(`${init?.method} ${String(input)}`);
      if (String(input).endsWith("/import/zip")) return new Response(JSON.stringify({ detail: "unsafe archive" }), { status: 422 });
      return new Response(null, { status: 204 });
    });
    const { create, forget, onCreated } = setup();
    pick(/^(ZIP file|ZIP-Datei)$/);
    const file = new File(["PK"], "site.zip", { type: "application/zip" });
    fireEvent.change(screen.getByLabelText(/^(ZIP file|ZIP-Datei)$/, { selector: "input[type=file]" }), { target: { files: [file] } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("dialog").querySelector("form")!);
    });
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ source: "zip" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toBe("unsafe archive"));
    expect(fetches).toEqual(["PUT /api/projects/p1/import/zip", "DELETE /api/projects/p1"]);
    expect(forget).toHaveBeenCalledWith("p1");
    expect(onCreated).not.toHaveBeenCalled();
  });

  it("offers server folders to admins only", () => {
    setup(false);
    expect(screen.queryByLabelText(/^(Server folder|Ordner auf dem Server)$/)).toBeNull();
    cleanup();
    setup(true);
    expect(screen.getByLabelText(/^(Server folder|Ordner auf dem Server)$/)).toBeTruthy();
  });

  it("offers full-stack apps to everyone", async () => {
    const { create } = setup(false, vi.fn().mockResolvedValue({ ...project, source: "app", kind: "app" }));
    pick(/^(App \(server \+ web\)|App \(Server \+ Web\))$/);
    expect(screen.getByText(/independent reviewer|unabhängiger Prüfer/)).toBeTruthy();
    await act(async () => {
      fireEvent.submit(screen.getByRole("dialog").querySelector("form")!);
    });
    expect(create).toHaveBeenCalledWith({ name: "App", source: "app", url: "", folder: "" });
  });

  it("offers Apple apps where this user may build them, with an optional bundle id", async () => {
    setup();
    expect(screen.queryByLabelText(/^(Apple app|Apple-App)$/)).toBeNull();
    cleanup();
    const { create } = setup(false, vi.fn().mockResolvedValue({ ...project, source: "apple", kind: "apple" }), true);
    pick(/^(Apple app|Apple-App)$/);
    expect(screen.getByText(/SwiftUI/)).toBeTruthy();
    fireEvent.change(screen.getByPlaceholderText("com.example.app"), { target: { value: " de.ada.app " } });
    await act(async () => {
      fireEvent.submit(screen.getByRole("dialog").querySelector("form")!);
    });
    expect(create).toHaveBeenCalledWith({ name: "App", source: "apple", url: "", folder: "", bundle_id: "de.ada.app" });
  });
});
