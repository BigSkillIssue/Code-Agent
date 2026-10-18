import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { Chat } from "./api/types";
import { ChatLink, ProjectPage } from "./App";
import { Sidebar } from "./components/Sidebar";
import { useStore } from "./state/store";

const chat = (id: string, state: Chat["state"] = "idle"): Chat => ({
  id, project_id: "p1", title: `chat ${id}`, state, mode: "edits", model: "", effort: "auto", can_write: true, created_at: 0, updated_at: 0,
});

const mocks = vi.hoisted(() => ({ get: vi.fn(), del: vi.fn() }));
vi.mock("./api/client", () => ({
  api: { get: mocks.get, delete: mocks.del, post: vi.fn(), patch: vi.fn() },
  ApiError: class extends Error {},
}));
vi.mock("./components/ChatView", () => ({
  ChatView: ({ chat: shown }: { chat: Chat }) => {
    const { clearChat } = useStore();
    return (
      <div>
        <p data-testid="chat">{shown.title}</p>
        <button type="button" onClick={() => void clearChat(shown)}>clear</button>
      </div>
    );
  },
}));

beforeEach(() => {
  mocks.get.mockReset();
  mocks.del.mockReset().mockResolvedValue(undefined);
  useStore.setState({
    chats: {}, chatData: {}, socket: null, error: "",
    projects: [{ id: "p1", name: "Shop", role: "editor", source: "empty", created_at: 0, updated_at: 0 },
               { id: "p2", name: "Blog", role: "viewer", source: "empty", created_at: 0, updated_at: 0 }],
  });
});

afterEach(cleanup);

function at(path: string) {
  render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/p/:projectId" element={<ProjectPage />} />
        <Route path="/c/:chatId" element={<ChatLink />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("a project is its one chat", () => {
  it("opens the project's chat and clearing it shows the empty one that takes its place", async () => {
    mocks.get.mockResolvedValueOnce(chat("c1")).mockResolvedValueOnce(chat("c2"));
    at("/p/p1");
    expect((await screen.findByTestId("chat")).textContent).toBe("chat c1");
    expect(mocks.get).toHaveBeenCalledWith("/api/projects/p1/chat");
    fireEvent.click(screen.getByRole("button", { name: "clear" }));
    await waitFor(() => expect(screen.getByTestId("chat").textContent).toBe("chat c2"));
    expect(mocks.del).toHaveBeenCalledWith("/api/chats/c1");
  });

  it("sends an old chat link to its project", async () => {
    mocks.get.mockResolvedValue(chat("c1")); // the old link, then the project's chat
    at("/c/c1");
    expect((await screen.findByTestId("chat")).textContent).toBe("chat c1");
    expect(mocks.get).toHaveBeenCalledWith("/api/projects/p1/chat");
  });

  it("lists projects only, with the state of their chat", () => {
    useStore.setState({ chats: { p1: chat("c1", "running") } });
    render(
      <MemoryRouter>
        <Sidebar activeProject="p1" />
      </MemoryRouter>,
    );
    const links = screen.getAllByRole("link").filter((l) => l.getAttribute("href")?.startsWith("/p/"));
    expect(links.map((l) => [l.getAttribute("href"), l.textContent])).toEqual([["/p/p1", "Shop"], ["/p/p2", "Blog"]]);
    expect(links[0].querySelector(".animate-pulse")).toBeTruthy(); // its chat is running
    expect(screen.queryByTitle(/New chat|Neuer Chat/)).toBeNull();
  });
});
