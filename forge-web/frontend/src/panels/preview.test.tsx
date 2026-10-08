import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { PreviewOverview, Program, ProjectApi } from "../api/project";
import { commandOf, landingOn, PreviewPanel } from "./Preview";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

function program(id: string, line: string, running = true): Program {
  return { id, name: "preview", argv: ["/bin/sh", "-c", line], cwd: "/workspace", started_at: 1, running, exit_code: null, total_lines: 3 };
}

const TICKET = "http://p5173-abc.localhost:8420/__forge_preview/enter?ticket=T1&next=%2F";

function fakeApi(overview: Partial<PreviewOverview> = {}) {
  const state: PreviewOverview = {
    enabled: true,
    suggestions: [{ label: "dev (package.json)", command: "npm install && npm run dev" }],
    programs: [program("p1", "npm run dev"), program("p2", "old thing", false)],
    ports: [{ port: 5173, address: "::1" }],
    ...overview,
  };
  return {
    preview: vi.fn(async () => state),
    startProgram: vi.fn(async (command: string) => program("p9", command)),
    stopProgram: vi.fn(async () => undefined),
    programOutput: vi.fn(async () => ({ lines: ["VITE ready", "Local: http://localhost:5173/"], from: 0, next: 2, running: true })),
    openPreview: vi.fn(async () => ({ url: TICKET })),
  };
}

const asApi = (api: ReturnType<typeof fakeApi>) => api as unknown as ProjectApi;

describe("helpers", () => {
  it("lands the ticket on a path and shows commands as typed", () => {
    expect(landingOn(TICKET, "/about?x=1")).toContain("next=%2Fabout%3Fx%3D1");
    expect(landingOn(TICKET, "docs")).toContain("next=%2Fdocs");
    expect(new URL(landingOn(TICKET, "/a")).searchParams.get("ticket")).toBe("T1");
    expect(commandOf(program("p", "npm run dev"))).toBe("npm run dev");
    expect(commandOf({ ...program("p", ""), argv: ["python3", "-m", "http.server"] })).toBe("python3 -m http.server");
  });
});

describe("PreviewPanel", () => {
  it("starts suggested and own commands, stops programs and shows their output", async () => {
    const api = fakeApi();
    render(<PreviewPanel api={asApi(api)} canEdit active onError={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /npm install && npm run dev/ }));
    await waitFor(() => expect(api.startProgram).toHaveBeenCalledWith("npm install && npm run dev"));
    fireEvent.change(screen.getByRole("textbox"), { target: { value: "  python3 app.py  " } });
    fireEvent.click(screen.getByRole("button", { name: /^(Start|Starten)$/ }));
    await waitFor(() => expect(api.startProgram).toHaveBeenCalledWith("python3 app.py"));
    expect(screen.queryByText("old thing")).toBeNull(); // only running programs are listed
    const row = screen.getByText("npm run dev").closest("li")!;
    fireEvent.click(within(row).getByRole("button", { name: /^(Output|Ausgabe)$/ }));
    expect(await screen.findByText(/VITE ready/)).toBeTruthy();
    expect(api.programOutput).toHaveBeenCalledWith("p1", 0);
    fireEvent.click(within(row).getByTitle(/^(Stop|Stoppen)$/));
    await waitFor(() => expect(api.stopProgram).toHaveBeenCalledWith("p1"));
  });

  it("shows a port in a sandboxed frame and goes to other paths with a fresh ticket", async () => {
    const api = fakeApi();
    render(<PreviewPanel api={asApi(api)} canEdit active onError={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: /5173/ }));
    const frame = (await screen.findByTitle(/^(Preview|Vorschau)$/)) as HTMLIFrameElement;
    expect(frame.src).toBe(landingOn(TICKET, "/"));
    expect(frame.getAttribute("sandbox")).not.toContain("allow-top-navigation");
    fireEvent.change(screen.getByRole("textbox", { name: /^(Path|Pfad)$/ }), { target: { value: "/about" } });
    fireEvent.click(screen.getByTitle(/^(Reload|Neu laden)$/));
    await waitFor(() => expect(api.openPreview).toHaveBeenCalledTimes(2)); // tickets work once
    expect((screen.getByTitle(/^(Preview|Vorschau)$/) as HTMLIFrameElement).src).toContain("next=%2Fabout");
    fireEvent.click(screen.getByTitle(/^(Back|Zurück)$/));
    expect(await screen.findByTestId("preview")).toBeTruthy();
  });

  it("opens a new tab that cannot reach back into Forge", async () => {
    const api = fakeApi();
    const tab = { opener: "forge" as unknown, location: { href: "" }, close: vi.fn() };
    const open = vi.spyOn(window, "open").mockReturnValue(tab as unknown as Window);
    render(<PreviewPanel api={asApi(api)} canEdit active onError={vi.fn()} />);
    fireEvent.click(await screen.findByTitle(/^(Open in a new tab|In neuem Tab öffnen)$/));
    await waitFor(() => expect(tab.location.href).toBe(landingOn(TICKET, "/")));
    expect(open).toHaveBeenCalledWith("about:blank", "_blank");
    expect(tab.opener).toBeNull();
  });

  it("lets viewers look but not start or stop, and explains when previews are off", async () => {
    const api = fakeApi({ enabled: false });
    render(<PreviewPanel api={asApi(api)} canEdit={false} active onError={vi.fn()} />);
    expect(await screen.findByText(/preview domain|Vorschau-Domain/)).toBeTruthy();
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(screen.queryByTitle(/^(Stop|Stoppen)$/)).toBeNull();
    expect((screen.getByRole("button", { name: /5173/ }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("does not poll while hidden and reports a failing poll once", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const api = fakeApi();
    const onError = vi.fn();
    const { rerender } = render(<PreviewPanel api={asApi(api)} canEdit active={false} onError={onError} />);
    expect(api.preview).not.toHaveBeenCalled();
    api.preview.mockRejectedValue(new Error("the project's sandbox is not reachable"));
    rerender(<PreviewPanel api={asApi(api)} canEdit active onError={onError} />);
    await vi.advanceTimersByTimeAsync(8000);
    expect(api.preview.mock.calls.length).toBeGreaterThanOrEqual(3);
    expect(onError).toHaveBeenCalledTimes(1);
    vi.useRealTimers();
  });
});
