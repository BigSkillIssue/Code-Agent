// Terminals in the project's sandbox: one tab and one WebSocket per terminal. The first visit
// opens a shell; tabs stay connected while the panel is open, so switching tabs loses nothing.

import { Plus, X } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useRef, useState } from "react";
import type { ProjectApi, TerminalInfo } from "../api/project";
import { t } from "../lib/i18n";

const XtermView = lazy(() => import("./XtermView"));

/** The server closes a terminal's socket with this code when the terminal is gone. */
export const ENDED = 4404;
const MAX_TRIES = 5; // failed connects in a row before we stop and offer a button
export const FONT_SIZE = 13;

export type LinkState = "connecting" | "open" | "ended" | "lost";

export interface LinkEvents {
  output: (data: Uint8Array) => void;
  state: (state: LinkState) => void;
  reset: () => void; // the server replays the terminal's scrollback on every connect
}

/** One terminal's WebSocket: keystrokes and sizes out, output in, reconnecting after drops. */
export class TerminalLink {
  private ws: WebSocket | null = null;
  private size: { cols: number; rows: number } | null = null;
  private failures = 0;
  private stopped = false;
  private timer: ReturnType<typeof setTimeout> | undefined;
  private readonly encoder = new TextEncoder();

  constructor(
    private readonly url: string,
    private readonly events: LinkEvents,
    private readonly makeSocket: (url: string) => WebSocket = (url) => new WebSocket(url),
  ) {}

  connect(): void {
    this.stopped = false;
    this.events.state("connecting");
    const ws = this.makeSocket(this.url);
    ws.binaryType = "arraybuffer";
    this.ws = ws;
    let opened = false;
    ws.onopen = () => {
      opened = true;
      this.failures = 0;
      this.events.reset();
      if (this.size) this.sendSize();
      this.events.state("open");
    };
    ws.onmessage = (event: MessageEvent) => {
      if (event.data instanceof ArrayBuffer) this.events.output(new Uint8Array(event.data));
    };
    ws.onclose = (event: CloseEvent) => {
      this.ws = null;
      if (this.stopped) return;
      if (event.code === ENDED) return this.events.state("ended");
      this.failures = opened ? 1 : this.failures + 1;
      if (this.failures > MAX_TRIES) return this.events.state("lost");
      this.events.state("connecting");
      this.timer = setTimeout(() => this.connect(), Math.min(500 * 2 ** (this.failures - 1), 8000));
    };
  }

  /** Text the user typed (xterm's onData). */
  type(text: string): void {
    this.sendBytes(this.encoder.encode(text));
  }

  /** Raw bytes as a binary string (xterm's onBinary, e.g. some mouse reports). */
  typeBinary(data: string): void {
    this.sendBytes(Uint8Array.from(data, (c) => c.charCodeAt(0) & 0xff));
  }

  resize(cols: number, rows: number): void {
    this.size = { cols, rows };
    this.sendSize();
  }

  close(): void {
    this.stopped = true;
    clearTimeout(this.timer);
    this.ws?.close();
    this.ws = null;
  }

  private sendBytes(data: Uint8Array): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(data);
  }

  private sendSize(): void {
    if (this.size && this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: "resize", ...this.size }));
    }
  }
}

interface Props {
  api: ProjectApi;
  active: boolean; // the terminal tab is showing
  onError: (message: string) => void;
}

const byStart = (a: TerminalInfo, b: TerminalInfo) => a.started_at - b.started_at;
const clamp = (value: number, low: number, high: number) => Math.min(high, Math.max(low, value));

/** About how many columns and rows fit the box, so a new shell starts at its final size. */
export function sizeFor(box: HTMLElement | null): { cols: number; rows: number } {
  if (!box || box.clientWidth === 0 || box.clientHeight === 0) return { cols: 80, rows: 24 };
  const context = document.createElement("canvas").getContext("2d");
  if (!context) return { cols: 80, rows: 24 };
  context.font = `${FONT_SIZE}px ${getComputedStyle(box).fontFamily}`;
  const cell = context.measureText("W").width || FONT_SIZE * 0.6;
  return {
    cols: clamp(Math.floor((box.clientWidth - 16) / cell), 10, 500),
    rows: clamp(Math.floor((box.clientHeight - 8) / Math.ceil(FONT_SIZE * 1.17)), 4, 200),
  };
}

export function TerminalPanel({ api, active, onError }: Props) {
  const [terminals, setTerminals] = useState<TerminalInfo[] | null>(null);
  const [current, setCurrent] = useState<string | null>(null);
  const [states, setStates] = useState<Record<string, LinkState>>({});
  const [attempt, setAttempt] = useState<Record<string, number>>({});
  const loading = useRef(false);
  const body = useRef<HTMLDivElement>(null);

  const open = useCallback(async () => {
    try {
      const { cols, rows } = sizeFor(body.current);
      const created = await api.openTerminal(cols, rows);
      setTerminals((list) => [...(list ?? []), created]);
      setCurrent(created.id);
    } catch (err) {
      onError((err as Error).message);
    }
  }, [api, onError]);

  useEffect(() => {
    if (!active || loading.current) return;
    loading.current = true;
    api
      .terminals()
      .then(async (found) => {
        const sorted = [...found].sort(byStart);
        setTerminals(sorted);
        if (sorted.length) setCurrent(sorted[sorted.length - 1].id);
        else await open();
      })
      .catch((err: Error) => {
        loading.current = false;
        onError(err.message);
      });
  }, [active, api, open, onError]);

  const close = async (id: string) => {
    try {
      await api.closeTerminal(id);
    } catch {
      // already gone (it ended, or the sandbox stopped): just drop the tab
    }
    setTerminals((list) => (list ?? []).filter((term) => term.id !== id));
  };

  const setState = useCallback((id: string, state: LinkState) => {
    setStates((all) => (all[id] === state ? all : { ...all, [id]: state }));
  }, []);

  // After a tab closes, the newest one left shows.
  const shown = terminals?.some((term) => term.id === current) ? current : (terminals?.at(-1)?.id ?? null);
  return (
    <div className="flex h-full flex-col" data-testid="terminals">
      <div className="flex items-center gap-1 overflow-x-auto border-b border-line px-1" role="tablist">
        {(terminals ?? []).map((term, index) => {
          const ended = states[term.id] === "ended" || !term.running;
          return (
            <div key={term.id} className={`flex items-center rounded-t ${shown === term.id ? "bg-panel" : ""}`}>
              <button
                type="button"
                role="tab"
                aria-selected={shown === term.id}
                className={`px-2 py-1 text-xs ${ended ? "text-muted line-through" : ""}`}
                onClick={() => setCurrent(term.id)}
              >
                {t("terminalN", { n: String(index + 1) })}
              </button>
              <button
                type="button"
                title={t("closeTerminal")}
                className="p-0.5 text-muted"
                onClick={() => void close(term.id)}
              >
                <X className="size-3" />
              </button>
            </div>
          );
        })}
        <button type="button" title={t("newTerminal")} className="p-1 text-muted" onClick={() => void open()}>
          <Plus className="size-4" />
        </button>
      </div>
      <div ref={body} className="relative min-h-0 flex-1 font-mono">
        {terminals === null && <p className="p-3 font-sans text-sm text-muted">{t("connecting")}</p>}
        {terminals?.length === 0 && <p className="p-3 font-sans text-sm text-muted">{t("noTerminals")}</p>}
        {(terminals ?? []).map((term) => {
          const showing = shown === term.id;
          const state = states[term.id];
          return (
            <div key={term.id} className={`absolute inset-0 flex-col ${showing ? "flex" : "hidden"}`}>
              {(state === "ended" || state === "lost") && (
                <div className="flex items-center gap-2 border-b border-line bg-panel px-2 py-1 font-sans text-xs">
                  <span>{state === "ended" ? t("terminalEnded") : t("terminalLost")}</span>
                  {state === "lost" && (
                    <button
                      type="button"
                      className="rounded border border-line px-2"
                      onClick={() => setAttempt((all) => ({ ...all, [term.id]: (all[term.id] ?? 0) + 1 }))}
                    >
                      {t("reconnect")}
                    </button>
                  )}
                  <button type="button" className="rounded border border-line px-2" onClick={() => void close(term.id)}>
                    {t("closeTerminal")}
                  </button>
                </div>
              )}
              <Suspense fallback={<p className="p-3 font-sans text-sm text-muted">{t("connecting")}</p>}>
                <XtermView
                  key={attempt[term.id] ?? 0}
                  url={api.terminalUrl(term.id)}
                  active={active && showing}
                  onState={(next) => setState(term.id, next)}
                />
              </Suspense>
            </div>
          );
        })}
      </div>
    </div>
  );
}
