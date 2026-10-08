import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { GitStatus, ProjectApi } from "../api/project";
import { Changes } from "./Changes";
import { ProjectPanel } from "./ProjectPanel";

// The real editor (CodeMirror) needs a browser layout engine; a text box stands in for it.
vi.mock("./CodeEditor", () => ({
  default: (props: { value: string; readOnly: boolean; onChange: (v: string) => void }) => (
    <textarea aria-label="editor" value={props.value} readOnly={props.readOnly} onChange={(e) => props.onChange(e.target.value)} />
  ),
}));

afterEach(cleanup);

const status: GitStatus = {
  repo: true, branch: "main", upstream: null, ahead: 1, behind: 0,
  files: [
    { path: "app.py", index: "M", worktree: " ", from: null },
    { path: "notes.md", index: " ", worktree: "M", from: null },
    { path: "new.txt", index: "?", worktree: "?", from: null },
  ],
};

function fakeApi(overrides: Partial<ProjectApi> = {}): ProjectApi {
  const api = {
    list: vi.fn(async (path: string) => ({
      path,
      entries:
        path === ""
          ? [
              { name: ".git", type: "dir", size: 0, mtime: 0 },
              { name: "src", type: "dir", size: 0, mtime: 0 },
              { name: "README.md", type: "file", size: 5, mtime: 1 },
            ]
          : [{ name: "main.py", type: "file", size: 9, mtime: 1 }],
    })),
    read: vi.fn(async (path: string) => ({ path, size: 9, mtime: 42, truncated: false, binary: false, text: "print(1)\n" })),
    save: vi.fn(async (path: string) => ({ path, mtime: 43 })),
    status: vi.fn(async () => status),
    branches: vi.fn(async () => ({ current: "main", branches: [{ name: "main", commit: "abc", upstream: null }] })),
    log: vi.fn(async () => ({ commits: [{ commit: "abcdef123", author: "Ada", email: "a@x", time: 1, subject: "first" }] })),
    remote: vi.fn(async () => ({ url: "https://github.com/ada/app.git", problem: null })),
    diff: vi.fn(async () => ({ diff: "-old\n+new\n", truncated: false })),
    stage: vi.fn(async () => ({})),
    unstage: vi.fn(async () => ({})),
    discard: vi.fn(async () => ({})),
    commit: vi.fn(async () => ({ commit: "def" })),
    push: vi.fn(async () => ({ ok: true, output: "" })),
    pull: vi.fn(async () => ({ ok: true, merged: true, output: "" })),
    downloadUrl: (path: string) => `/raw?path=${path}`,
    ...overrides,
  };
  return api as unknown as ProjectApi;
}

describe("Changes", () => {
  it("lists staged and unstaged files and stages, commits and pushes", async () => {
    const api = fakeApi();
    render(<Changes api={api} canEdit refreshKey={0} onError={vi.fn()} />);
    const panel = await screen.findByTestId("changes");
    await screen.findByText("notes.md");
    expect(within(panel).getByText("app.py")).toBeTruthy();
    fireEvent.click(within(screen.getByText("notes.md").closest("li")!).getByText(/^(Stage|Vormerken)$/));
    await waitFor(() => expect(api.stage).toHaveBeenCalledWith(["notes.md"]));
    fireEvent.click(screen.getByText("notes.md"));
    await screen.findByText("+new");
    fireEvent.change(screen.getByRole("textbox", { name: /commit/i }), { target: { value: "Fix it" } });
    fireEvent.click(screen.getByRole("button", { name: /^(Commit|Committen)$/ }));
    await waitFor(() => expect(api.commit).toHaveBeenCalledWith("Fix it"));
    fireEvent.click(screen.getByRole("button", { name: "Push" }));
    await waitFor(() => expect(api.push).toHaveBeenCalled());
  });

  it("asks before discarding", async () => {
    const api = fakeApi();
    const confirm = vi.spyOn(window, "confirm").mockReturnValue(false);
    render(<Changes api={api} canEdit refreshKey={0} onError={vi.fn()} />);
    const row = (await screen.findByText("new.txt")).closest("li")!;
    fireEvent.click(within(row).getByText(/^(Discard|Verwerfen)$/));
    expect(confirm).toHaveBeenCalled();
    expect(api.discard).not.toHaveBeenCalled();
    confirm.mockRestore();
  });

  it("shows viewers everything but no buttons that change something", async () => {
    render(<Changes api={fakeApi()} canEdit={false} refreshKey={0} onError={vi.fn()} />);
    await screen.findByText("notes.md");
    expect(screen.queryByText(/^(Stage|Vormerken)$/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Push" })).toBeNull();
    expect(screen.queryByRole("textbox", { name: /commit/i })).toBeNull();
  });
});

describe("Files", () => {
  it("opens folders and files, hides .git, and saves with the file's time", async () => {
    const api = fakeApi();
    render(<ProjectPanel projectId="p1" canEdit refreshKey={0} onClose={vi.fn()} onError={vi.fn()} api={api} />);
    await screen.findByText("README.md");
    expect(screen.queryByText(".git")).toBeNull();
    fireEvent.click(screen.getByText("src"));
    fireEvent.click(await screen.findByText("main.py"));
    const editor = await screen.findByRole("textbox", { name: "editor" });
    fireEvent.change(editor, { target: { value: "print(2)\n" } });
    await act(async () => {
      fireEvent.keyDown(editor, { key: "s", ctrlKey: true });
    });
    expect(api.save).toHaveBeenCalledWith("src/main.py", "print(2)\n", 42);
  });

  it("says when the file changed meanwhile instead of overwriting it", async () => {
    const api = fakeApi({ save: vi.fn().mockRejectedValue(new ApiError(409, "changed")) });
    render(<ProjectPanel projectId="p1" canEdit refreshKey={0} onClose={vi.fn()} onError={vi.fn()} api={api} />);
    fireEvent.click(await screen.findByText("README.md"));
    const editor = await screen.findByRole("textbox", { name: "editor" });
    fireEvent.change(editor, { target: { value: "x" } });
    await act(async () => {
      fireEvent.click(screen.getByTitle(/^(Save|Speichern)$/));
    });
    expect((await screen.findByRole("alert")).textContent).toMatch(/changed|geändert/);
  });

  it("opens files read-only for viewers", async () => {
    render(<ProjectPanel projectId="p1" canEdit={false} refreshKey={0} onClose={vi.fn()} onError={vi.fn()} api={fakeApi()} />);
    fireEvent.click(await screen.findByText("README.md"));
    const editor = (await screen.findByRole("textbox", { name: "editor" })) as HTMLTextAreaElement;
    expect(editor.readOnly).toBe(true);
    expect(screen.queryByTitle(/^(Save|Speichern)$/)).toBeNull();
    expect(screen.queryByTitle(/^(New file|Neue Datei)$/)).toBeNull();
  });
});
