import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Attachment } from "../api/types";
import { BUILTIN_COMMANDS } from "../lib/completion";
import { MessageFiles } from "./Attachments";
import { Composer } from "./Composer";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const stored = (name: string, media_type = "image/png"): Attachment => ({
  path: `.forge/attachments/20261018-ab12cd34-${name}`, name, media_type, size: 68,
});

function setup() {
  const onSend = vi.fn().mockResolvedValue(undefined);
  let finish: (value: Attachment) => void = () => undefined;
  const upload = vi.fn(
    (_file: File) =>
      new Promise<Attachment>((resolve) => {
        finish = resolve; // the test says when the upload is done
      }),
  );
  render(<Composer running={false} onSend={onSend} onStop={vi.fn()} commands={BUILTIN_COMMANDS} upload={upload} />);
  const box = screen.getByRole("textbox");
  return { onSend, upload, box, done: (value: Attachment) => act(async () => finish(value)) };
}

const png = (name = "shot.png", size = 68) => new File([new Uint8Array(size)], name, { type: "image/png" });

describe("files in the composer", () => {
  it("uploads a picked file, waits for it and sends it with the message", async () => {
    const { onSend, upload, box, done } = setup();
    fireEvent.change(screen.getByTestId("file-picker"), { target: { files: [png()] } });
    expect(upload).toHaveBeenCalledTimes(1);
    expect(screen.getByLabelText(/Uploading|Wird hochgeladen/)).toBeTruthy();
    fireEvent.change(box, { target: { value: "What is on it?" } });
    expect(screen.getByRole("button", { name: /Send|Senden/ }).hasAttribute("disabled")).toBe(true);
    await done(stored("shot.png"));
    expect(screen.queryByLabelText(/Uploading|Wird hochgeladen/)).toBeNull();
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(onSend).toHaveBeenCalledWith("What is on it?", [stored("shot.png")]);
    expect(screen.queryByTestId("file-chip")).toBeNull(); // sent: the chips go
  });

  it("takes pasted and dropped pictures, refuses big ones and can remove a chip", async () => {
    const { onSend, upload, box, done } = setup();
    fireEvent.paste(box, { clipboardData: { files: [png("pasted.png")] } });
    await done(stored("pasted.png"));
    fireEvent.drop(screen.getByTestId("composer"), { dataTransfer: { files: [png("dropped.png")], types: ["Files"] } });
    await done(stored("dropped.png"));
    fireEvent.change(screen.getByTestId("file-picker"), { target: { files: [png("huge.png", 21 * 1024 * 1024)] } });
    expect(upload).toHaveBeenCalledTimes(2); // the big one never went
    expect(screen.getByText("huge.png").closest("[data-testid=file-chip]")?.getAttribute("title")).toMatch(/20 MB/);
    fireEvent.click(screen.getByRole("button", { name: /Remove huge.png|huge.png entfernen/ }));
    fireEvent.click(screen.getByRole("button", { name: /Remove pasted.png|pasted.png entfernen/ }));
    fireEvent.change(box, { target: { value: "Both?" } });
    await act(async () => {
      fireEvent.keyDown(box, { key: "Enter" });
    });
    expect(onSend).toHaveBeenCalledWith("Both?", [stored("dropped.png")]);
  });
});

describe("files of a sent message", () => {
  it("shows a picture from its own bytes and any other file as a chip", async () => {
    const fetched = vi.fn(async () => new Response(new Uint8Array([137, 80, 78, 71])));
    vi.stubGlobal("fetch", fetched);
    vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:shot"), revokeObjectURL: vi.fn() }));
    render(<MessageFiles projectId="p1" files={[stored("shot.png"), stored("spec.pdf", "application/pdf")]} />);
    const picture = await screen.findByRole("img", { name: "shot.png" });
    expect(picture.getAttribute("src")).toBe("blob:shot");
    expect(fetched).toHaveBeenCalledWith(
      `/api/projects/p1/files/raw?path=${encodeURIComponent(".forge/attachments/20261018-ab12cd34-shot.png")}`,
      { credentials: "same-origin" },
    );
    await waitFor(() => expect(screen.getByText("spec.pdf")).toBeTruthy());
  });
});
