import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { ProjectApi, SnapshotList } from "../api/project";
import { SnapshotsPanel } from "./Snapshots";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const listed = (storage: "node" | "local"): SnapshotList => ({
  enabled: true,
  storage,
  snapshots: [
    { id: "a".repeat(32), created_at: 1_790_000_000, chat_id: "c1", chat: "Shop page", commit: "abcdef1", kind: "incr", size: 2_500_000, state: "stored", error: "", where: storage, node: storage === "node" ? "vault-1" : "" },
    { id: "b".repeat(32), created_at: 1_789_990_000, chat_id: "c1", chat: "", commit: "", kind: "full", size: 0, state: "failed", error: "the snapshot is larger than 200 MB", where: "local", node: "" },
  ],
});

const api = (list: SnapshotList) =>
  ({
    snapshots: vi.fn(async () => list),
    restoreSnapshot: vi.fn(async () => ({ branch: "restore/2026-09-21-1406", commit: "f".repeat(40) })),
  }) as unknown as ProjectApi & { restoreSnapshot: ReturnType<typeof vi.fn> };

describe("the backups tab", () => {
  it("lists the backups with time, chat, commit and size, and says they are on the Forge server", async () => {
    render(<SnapshotsPanel api={api(listed("local"))} refreshKey={0} onError={vi.fn()} />);
    expect(await screen.findByText("abcdef1")).toBeTruthy();
    expect(screen.getByText(/Noch kein Speicher-Server|No storage server yet/)).toBeTruthy();
    expect(screen.getByText(/Shop page · 2\.4 MB/)).toBeTruthy();
    expect(screen.getByText(/larger than 200 MB/)).toBeTruthy();
    expect(screen.getAllByRole("button", { name: /Als Branch wiederherstellen|Restore as branch/ })).toHaveLength(1);
  });

  it("does not show the hint once a storage server keeps them", async () => {
    render(<SnapshotsPanel api={api(listed("node"))} refreshKey={0} onError={vi.fn()} />);
    expect(await screen.findByText("abcdef1")).toBeTruthy();
    expect(screen.queryByText(/Noch kein Speicher-Server|No storage server yet/)).toBeNull();
  });

  it("restores a backup as a branch only after a confirmation", async () => {
    const client = api(listed("node"));
    const confirm = vi.spyOn(window, "confirm").mockReturnValueOnce(false).mockReturnValueOnce(true);
    render(<SnapshotsPanel api={client} refreshKey={0} onError={vi.fn()} />);
    const button = await screen.findByRole("button", { name: /Als Branch wiederherstellen|Restore as branch/ });
    fireEvent.click(button);
    expect(client.restoreSnapshot).not.toHaveBeenCalled();
    fireEvent.click(button);
    await waitFor(() => expect(client.restoreSnapshot).toHaveBeenCalledWith("a".repeat(32)));
    expect(await screen.findByText(/restore\/2026-09-21-1406/)).toBeTruthy();
    expect(confirm).toHaveBeenCalledTimes(2);
  });

  it("is a tab for editors only", async () => {
    const { ProjectPanel } = await import("./ProjectPanel");
    const client = Object.assign(api(listed("local")), { product: vi.fn(async () => ({ product: false })) }) as unknown as ProjectApi;
    const props = { projectId: "p1", refreshKey: 0, onClose: vi.fn(), onError: vi.fn(), api: client };
    const { unmount } = render(<ProjectPanel {...props} canEdit={false} />);
    expect(screen.queryByRole("tab", { name: /Sicherungen|Backups/ })).toBeNull();
    unmount();
    render(<ProjectPanel {...props} canEdit />);
    fireEvent.click(screen.getByRole("tab", { name: /Sicherungen|Backups/ }));
    expect(await screen.findByTestId("snapshots")).toBeTruthy();
  });
});
