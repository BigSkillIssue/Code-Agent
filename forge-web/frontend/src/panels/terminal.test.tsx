import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProjectApi, TerminalInfo } from "../api/project";
import { ProjectPanel } from "./ProjectPanel";
import { ENDED, TerminalLink, TerminalPanel, type LinkState } from "./Terminal";

// xterm.js needs a real layout engine; this stand-in shows which terminal it is and can report
// connection states the way the real view would.
const mounts: string[] = [];
vi.mock("./XtermView", () => ({
  default: (props: { url: string; active: boolean; onState: (s: LinkState) => void }) => {
    mounts.push(props.url);
    return (
      <div data-testid="view" data-url={props.url} data-active={String(props.active)}>
        <button type="button" onClick={() => props.onState("ended")}>
          fake-ended
        </button>
        <button type="button" onClick={() => props.onState("lost")}>
          fake-lost
        </button>
      </div>
    );
  },
}));

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  mounts.length = 0;
});

class FakeSocket {
  static made: FakeSocket[] = [];
  readyState = 0;
  binaryType = "blob";
  sent: (string | Uint8Array)[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: ((event: CloseEvent) => void) | null = null;
  constructor(readonly url: string) {
    FakeSocket.made.push(this);
  }
  send(data: string | Uint8Array) {
    this.sent.push(data);
  }
  close() {
    this.readyState = 3;
  }
  open() {
    this.readyState = 1;
    this.onopen?.();
  }
  drop(code: number) {
    this.readyState = 3;
    this.onclose?.({ code } as CloseEvent);
  }
}

function newLink() {
  FakeSocket.made = [];
  const events = { output: vi.fn(), state: vi.fn(), reset: vi.fn() };
  const link = new TerminalLink("ws://x/t1", events, (url) => new FakeSocket(url) as unknown as WebSocket);
  return { link, events, socket: () => FakeSocket.made[FakeSocket.made.length - 1] };
}

describe("TerminalLink", () => {
  it("sends keystrokes as bytes and the size as a message, and shows output", () => {
    const { link, events, socket } = newLink();
    link.resize(100, 30); // before the socket is open: sent once it is
    link.connect();
    expect(socket().binaryType).toBe("arraybuffer");
    socket().open();
    expect(events.reset).toHaveBeenCalledTimes(1);
    expect(socket().sent).toEqual([JSON.stringify({ type: "resize", cols: 100, rows: 30 })]);
    link.type("ls ü\r");
    expect(socket().sent[1]).toEqual(new TextEncoder().encode("ls ü\r"));
    link.typeBinary("\x1b\xff");
    expect(Array.from(socket().sent[2] as Uint8Array)).toEqual([0x1b, 0xff]);
    socket().onmessage?.({ data: new Uint8Array([104, 105]).buffer } as MessageEvent);
    expect(events.output).toHaveBeenCalledWith(new Uint8Array([104, 105]));
    expect(events.state).toHaveBeenLastCalledWith("open");
  });

  it("stops when the terminal has ended and reconnects after other drops", () => {
    vi.useFakeTimers();
    const { link, events, socket } = newLink();
    link.connect();
    socket().open();
    socket().drop(1012); // the server restarts
    expect(events.state).toHaveBeenLastCalledWith("connecting");
    vi.advanceTimersByTime(600);
    expect(FakeSocket.made).toHaveLength(2);
    socket().open();
    expect(events.reset).toHaveBeenCalledTimes(2); // the replayed scrollback replaces the old screen
    socket().drop(ENDED);
    expect(events.state).toHaveBeenLastCalledWith("ended");
    vi.advanceTimersByTime(60_000);
    expect(FakeSocket.made).toHaveLength(2);
  });

  it("gives up after failing to connect several times in a row", () => {
    vi.useFakeTimers();
    const { link, events, socket } = newLink();
    link.connect();
    for (let i = 0; i < 6; i++) {
      socket().drop(1006);
      vi.advanceTimersByTime(10_000);
    }
    expect(events.state).toHaveBeenLastCalledWith("lost");
    expect(FakeSocket.made).toHaveLength(6);
    link.close();
  });
});

function terminal(id: string, startedAt: number, running = true): TerminalInfo {
  return { id, cols: 80, rows: 24, started_at: startedAt, running, exit_code: running ? null : 0, attached: 0 };
}

function fakeApi(existing: TerminalInfo[] = []) {
  let next = 0;
  const api = {
    terminals: vi.fn(async () => existing),
    openTerminal: vi.fn(async () => terminal(`tnew${++next}`, 100 + next)),
    closeTerminal: vi.fn(async () => undefined),
    terminalUrl: (id: string) => `ws://host/${id}`,
  };
  return api;
}

describe("TerminalPanel", () => {
  it("opens a shell on the first visit, adds and closes tabs", async () => {
    const api = fakeApi();
    const { rerender } = render(<TerminalPanel api={api as unknown as ProjectApi} active={false} onError={vi.fn()} />);
    expect(api.terminals).not.toHaveBeenCalled(); // nothing happens until the tab is shown
    rerender(<TerminalPanel api={api as unknown as ProjectApi} active onError={vi.fn()} />);
    await screen.findByRole("tab", { name: "Terminal 1" });
    expect(api.openTerminal).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTitle(/^(New terminal|Neues Terminal)$/));
    await screen.findByRole("tab", { name: "Terminal 2" });
    const views = screen.getAllByTestId("view");
    expect(views.map((v) => v.dataset.active)).toEqual(["false", "true"]); // the new one shows
    fireEvent.click(screen.getByRole("tab", { name: "Terminal 1" }));
    expect(screen.getAllByTestId("view").map((v) => v.dataset.active)).toEqual(["true", "false"]);
    fireEvent.click(screen.getAllByTitle(/^(Close terminal|Terminal schließen)$/)[0]);
    await waitFor(() => expect(api.closeTerminal).toHaveBeenCalledWith("tnew1"));
    expect(screen.getAllByTestId("view").map((v) => v.dataset.url)).toEqual(["ws://host/tnew2"]);
  });

  it("reattaches to existing terminals in the order they were started", async () => {
    const api = fakeApi([terminal("tb", 2), terminal("ta", 1, false)]);
    render(<TerminalPanel api={api as unknown as ProjectApi} active onError={vi.fn()} />);
    await screen.findByRole("tab", { name: "Terminal 2" });
    expect(api.openTerminal).not.toHaveBeenCalled();
    expect(screen.getAllByTestId("view").map((v) => v.dataset.url)).toEqual(["ws://host/ta", "ws://host/tb"]);
    expect(screen.getByRole("tab", { name: "Terminal 1" }).className).toContain("line-through"); // exited
  });

  it("shows when a terminal ended or lost its connection, and reconnects on request", async () => {
    const api = fakeApi([terminal("ta", 1)]);
    render(<TerminalPanel api={api as unknown as ProjectApi} active onError={vi.fn()} />);
    await screen.findByTestId("view");
    act(() => fireEvent.click(screen.getByText("fake-lost")));
    expect(screen.getByText(/connection to this terminal|Verbindung zu diesem Terminal/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^(Reconnect|Neu verbinden)$/ }));
    expect(mounts.filter((url) => url === "ws://host/ta").length).toBeGreaterThanOrEqual(2); // a fresh view
    act(() => fireEvent.click(screen.getByText("fake-ended")));
    expect(screen.getByText(/terminal has ended|Terminal ist beendet/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /^(Reconnect|Neu verbinden)$/ })).toBeNull();
  });

  it("reports errors from the server", async () => {
    const api = fakeApi();
    api.openTerminal.mockRejectedValueOnce(new Error("at most 16 terminals"));
    const onError = vi.fn();
    render(<TerminalPanel api={api as unknown as ProjectApi} active onError={onError} />);
    await waitFor(() => expect(onError).toHaveBeenCalledWith("at most 16 terminals"));
  });
});

describe("ProjectPanel terminal tab", () => {
  it("is there for editors only", () => {
    const api = { ...fakeApi(), list: vi.fn(async () => ({ path: "", entries: [] })) } as unknown as ProjectApi;
    render(<ProjectPanel projectId="p" canEdit={false} refreshKey={0} onClose={vi.fn()} onError={vi.fn()} api={api} />);
    expect(screen.queryByRole("tab", { name: "Terminal" })).toBeNull();
    cleanup();
    render(<ProjectPanel projectId="p" canEdit refreshKey={0} onClose={vi.fn()} onError={vi.fn()} api={api} />);
    expect(screen.getByRole("tab", { name: "Terminal" })).toBeTruthy();
  });
});
