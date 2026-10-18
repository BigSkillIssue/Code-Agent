import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PreviewOverview, Program, ProjectApi } from "../api/project";
import { BrowserPreview, newPorts, usePreviewPane } from "./BrowserPreview";

afterEach(cleanup);

const TICKET = "http://p5173-abc.localhost:8420/__forge_preview/enter?ticket=T1&next=%2F";
const program: Program = {
  id: "p1", name: "preview", argv: ["/bin/sh", "-c", "npm run dev"], cwd: "/workspace", started_at: 1,
  running: true, exit_code: null, total_lines: 2,
};

function fakeApi(overview: Partial<PreviewOverview> = {}) {
  const state: PreviewOverview = {
    enabled: true, suggestions: [{ label: "dev", command: "npm run dev" }], programs: [program],
    ports: [{ port: 5173, address: "::1" }], ...overview,
  };
  const api = {
    preview: vi.fn(async () => state),
    openPreview: vi.fn(async () => ({ url: TICKET })),
    programOutput: vi.fn(async () => ({ lines: ["VITE ready"], from: 0, next: 1, running: true })),
    startProgram: vi.fn(),
    stopProgram: vi.fn(),
  };
  return { api, asApi: api as unknown as ProjectApi };
}

function shown(api: ProjectApi, turns = 0) {
  return (
    <BrowserPreview projectId="p1" canEdit turns={turns} onClose={vi.fn()} onError={vi.fn()} api={api} />
  );
}

const landing = () => new URL(screen.getByTitle(/^(Preview|Vorschau)$/).getAttribute("src") ?? "").searchParams.get("next");

describe("the browser preview", () => {
  it("finds the ports that just appeared", () => {
    expect(newPorts([5173], [5173, 8000])).toEqual([8000]);
    expect(newPorts([], [])).toEqual([]);
  });

  it("opens the app, goes to an address, back and forward again", async () => {
    const { api, asApi } = fakeApi();
    render(shown(asApi));
    await waitFor(() => expect(landing()).toBe("/"));
    const address = screen.getByLabelText(/^(Path|Pfad)$/);
    fireEvent.change(address, { target: { value: "/about" } });
    fireEvent.submit(address.closest("form") as HTMLFormElement);
    await waitFor(() => expect(landing()).toBe("/about"));
    fireEvent.click(screen.getByRole("button", { name: /^(Back|Zurück)$/ }));
    await waitFor(() => expect(landing()).toBe("/"));
    const forward = screen.getByRole("button", { name: /^(Forward|Vor)$/ });
    expect(forward.hasAttribute("disabled")).toBe(false);
    fireEvent.click(forward);
    await waitFor(() => expect(landing()).toBe("/about"));
    expect(api.openPreview).toHaveBeenCalledWith(5173);
  });

  it("shows phone and tablet sizes and reloads after the agent's turn", async () => {
    const { api, asApi } = fakeApi();
    const view = render(shown(asApi));
    await waitFor(() => expect(landing()).toBe("/"));
    fireEvent.click(screen.getByRole("radio", { name: /^(Phone|Handy)$/ }));
    expect(screen.getByTestId("device-frame").style.width).toBe("390px");
    fireEvent.click(screen.getByRole("radio", { name: /^Tablet$/ }));
    expect(screen.getByTestId("device-frame").style.width).toBe("820px");
    const before = api.openPreview.mock.calls.length;
    view.rerender(shown(asApi, 1)); // a turn ended
    await waitFor(() => expect(api.openPreview.mock.calls.length).toBe(before + 1));
  });

  it("offers to start a dev server when nothing listens, and shows the console", async () => {
    const { api, asApi } = fakeApi({ ports: [] });
    render(shown(asApi));
    expect(await screen.findByText(/No program listens|Noch lauscht kein Programm/)).toBeTruthy();
    expect(screen.getByRole("button", { name: /npm run dev/ })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /Console|Konsole/ }));
    expect(await screen.findByText("VITE ready")).toBeTruthy();
    expect(api.openPreview).not.toHaveBeenCalled();
  });
});

describe("the preview next to the chat", () => {
  it("opens by itself on a port that appears while the agent works", async () => {
    const { api, asApi } = fakeApi();
    const ports = [{ port: 5173, address: "::1" }];
    api.preview.mockImplementation(async () => ({ enabled: true, suggestions: [], programs: [], ports: [...ports] }));
    const pane = renderHook(({ running, turns }) => usePreviewPane("p1", running, turns, asApi), {
      initialProps: { running: true, turns: 0 },
    });
    await waitFor(() => expect(api.preview).toHaveBeenCalled());
    expect(pane.result.current.open).toBe(false); // a server that was already there opens nothing
    ports.push({ port: 8000, address: "::1" });
    pane.rerender({ running: false, turns: 1 }); // the turn ended: one more look
    await waitFor(() => expect(pane.result.current.open).toBe(true));
    expect(pane.result.current.port).toBe(8000);
    act(() => pane.result.current.resize(100));
    expect(pane.result.current.width).toBe(320); // never narrower than that
  });
});
