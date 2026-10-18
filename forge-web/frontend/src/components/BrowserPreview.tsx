// The browser preview next to the chat (W47), as in Claude Code: the app the agent is building,
// with back, forward, reload, the address, the port, device sizes, a new tab and the dev
// server's console. The app runs on a host of its own and opens with a one-time ticket, so this
// keeps its own history of addresses (a frame of another site cannot be asked for its own).

import { ArrowLeft, ArrowRight, ExternalLink, Monitor, RotateCw, Smartphone, Tablet, TerminalSquare, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { projectApi, type PreviewOverview, type ProjectApi } from "../api/project";
import { t } from "../lib/i18n";
import { appFirst, landingOn, Output, Programs } from "../panels/Preview";
import { Segmented } from "./Segmented";
import { Select } from "./Select";

const POLL_MS = 2500;
export type Device = "desktop" | "tablet" | "phone";
export const DEVICE_WIDTH: Record<Device, number | null> = { desktop: null, tablet: 820, phone: 390 };

interface History {
  paths: string[];
  index: number;
}

/** The ports that appeared since the last look (W47: the preview opens by itself on one). */
export function newPorts(before: number[], now: number[]): number[] {
  return now.filter((port) => !before.includes(port));
}

/** The project's preview overview, looked at every few seconds while `active`. */
export function usePreviewOverview(api: ProjectApi, active: boolean, onError?: (message: string) => void) {
  const [overview, setOverview] = useState<PreviewOverview | null>(null);
  const lastError = useRef("");
  const report = useRef(onError);
  report.current = onError;
  const refresh = useCallback(async () => {
    try {
      setOverview(await api.preview());
      lastError.current = "";
    } catch (err) {
      const message = (err as Error).message;
      if (message !== lastError.current) report.current?.(message); // once, not on every poll
      lastError.current = message;
    }
  }, [api]);
  useEffect(() => {
    if (!active) return;
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [active, refresh]);
  return { overview, refresh };
}

interface Props {
  projectId: string;
  canEdit: boolean;
  turns: number; // grows when an agent turn ends: the page reloads (if wanted)
  port?: number | null; // the port to show first (one that just appeared)
  onClose: () => void;
  onError: (message: string) => void;
  api?: ProjectApi; // tests pass their own
}

export function BrowserPreview({ projectId, canEdit, turns, port: wanted, onClose, onError, api: given }: Props) {
  const api = useMemo(() => given ?? projectApi(projectId), [given, projectId]);
  const { overview, refresh } = usePreviewOverview(api, true, onError);
  const [port, setPort] = useState<number | null>(wanted ?? null);
  const [history, setHistory] = useState<History>({ paths: ["/"], index: 0 });
  const [address, setAddress] = useState("/");
  const [url, setUrl] = useState<string | null>(null);
  const [device, setDevice] = useState<Device>("desktop");
  const [showConsole, setShowConsole] = useState(false);
  const [autoReload, setAutoReload] = useState(true);
  const ports = useMemo(() => appFirst(overview?.ports ?? [], overview?.app_port).map((p) => p.port), [overview]);
  const path = history.paths[history.index];

  useEffect(() => {
    if (wanted) setPort(wanted);
  }, [wanted]);
  useEffect(() => {
    if (port === null && ports.length > 0) setPort(ports[0]); // the app first
  }, [port, ports]);

  const report = useRef(onError);
  report.current = onError;
  const open = useCallback(
    async (to: string) => {
      if (port === null) return;
      try {
        const { url: ticket } = await api.openPreview(port);
        setUrl(landingOn(ticket, to));
        setAddress(to);
      } catch (err) {
        report.current((err as Error).message);
      }
    },
    [api, port],
  );
  useEffect(() => {
    if (port !== null && overview?.enabled) void open(path);
  }, [port, path, overview?.enabled, open]);
  const firstTurns = useRef(turns);
  const afterTurn = useRef<() => void>(() => {});
  afterTurn.current = () => {
    if (autoReload) void open(path); // the agent may have changed the app
  };
  useEffect(() => {
    if (turns !== firstTurns.current) afterTurn.current();
  }, [turns]);

  const go = (to: string) => {
    const clean = to.startsWith("/") ? to : `/${to}`;
    setHistory((h) => ({ paths: [...h.paths.slice(0, h.index + 1), clean], index: h.index + 1 }));
  };
  const step = (by: number) => setHistory((h) => ({ ...h, index: Math.min(h.paths.length - 1, Math.max(0, h.index + by)) }));
  const openTab = async () => {
    if (port === null) return;
    const tab = window.open("about:blank", "_blank"); // opened now, while the click still counts
    try {
      const { url: ticket } = await api.openPreview(port);
      if (tab) {
        tab.opener = null; // the preview must not reach back into Forge
        tab.location.href = landingOn(ticket, path);
      }
    } catch (err) {
      tab?.close();
      onError((err as Error).message);
    }
  };

  const width = DEVICE_WIDTH[device];
  const running = overview?.programs.filter((p) => p.running) ?? [];
  const icon = "rounded p-1 text-muted hover:text-fg disabled:opacity-40";
  return (
    <section className="flex h-full min-w-0 flex-col bg-card" data-testid="browser-preview" aria-label={t("preview")}>
      <form
        className="flex flex-wrap items-center gap-1 border-b border-line px-2 py-1.5"
        onSubmit={(event) => {
          event.preventDefault();
          if (address === path) void open(path);
          else go(address);
        }}
      >
        <button type="button" title={t("back")} aria-label={t("back")} className={icon} disabled={history.index === 0} onClick={() => step(-1)}>
          <ArrowLeft className="size-4" />
        </button>
        <button type="button" title={t("forward")} aria-label={t("forward")} className={icon} disabled={history.index >= history.paths.length - 1} onClick={() => step(1)}>
          <ArrowRight className="size-4" />
        </button>
        <button type="button" title={t("reloadPreview")} aria-label={t("reloadPreview")} className={icon} disabled={!url} onClick={() => void open(path)}>
          <RotateCw className="size-4" />
        </button>
        {ports.length > 0 && (
          <Select
            compact
            ariaLabel={t("ports")}
            value={String(port ?? ports[0])}
            onChange={(value) => setPort(Number(value))}
            options={ports.map((p) => ({ value: String(p), label: `:${p}`, description: p === overview?.app_port ? t("appPortLabel") : undefined }))}
          />
        )}
        <input
          aria-label={t("path")}
          className="min-w-[6rem] flex-1 rounded-full border border-line bg-panel px-3 py-1 font-mono text-xs outline-none focus:border-accent"
          value={address}
          onChange={(event) => setAddress(event.target.value)}
        />
        <Segmented
          ariaLabel={t("deviceSize")}
          value={device}
          onChange={setDevice}
          options={[
            { value: "desktop", label: <Monitor className="size-3.5" aria-label={t("deviceDesktop")} /> },
            { value: "tablet", label: <Tablet className="size-3.5" aria-label={t("deviceTablet")} /> },
            { value: "phone", label: <Smartphone className="size-3.5" aria-label={t("devicePhone")} /> },
          ]}
        />
        <button type="button" title={t("openInTab")} aria-label={t("openInTab")} className={icon} disabled={!url} onClick={() => void openTab()}>
          <ExternalLink className="size-4" />
        </button>
        <button
          type="button"
          title={t("previewConsole")}
          aria-label={t("previewConsole")}
          aria-pressed={showConsole}
          className={`rounded p-1 ${showConsole ? "text-accent" : "text-muted hover:text-fg"}`}
          onClick={() => setShowConsole(!showConsole)}
        >
          <TerminalSquare className="size-4" />
        </button>
        <button type="button" title={t("close")} aria-label={t("close")} className={icon} onClick={onClose}>
          <X className="size-4" />
        </button>
      </form>
      <div className="relative flex min-h-0 flex-1 justify-center overflow-auto bg-panel">
        {overview === null && <p className="p-4 text-sm text-muted">{t("connecting")}</p>}
        {overview !== null && !overview.enabled && <p className="m-4 rounded border border-warn p-2 text-xs">{t("previewOff")}</p>}
        {overview?.enabled && ports.length === 0 && (
          <div className="w-full max-w-lg p-4 text-sm">
            <p className="mb-3 text-muted">{t("previewNothing")}</p>
            <Programs api={api} canEdit={canEdit} overview={overview} onChange={refresh} onError={onError} />
          </div>
        )}
        {overview?.enabled && url && ports.length > 0 && (
          <div
            data-testid="device-frame"
            className={`h-full shrink-0 ${width ? "my-3 overflow-hidden rounded-xl border border-line shadow-lg" : "w-full"}`}
            style={width ? { width: `${width}px`, maxWidth: "100%", height: "calc(100% - 1.5rem)" } : undefined}
          >
            <iframe
              key={url}
              title={t("preview")}
              src={url}
              className="size-full bg-white"
              // Its own site already; the sandbox also keeps it from navigating Forge's tab away.
              sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads"
              referrerPolicy="no-referrer"
            />
          </div>
        )}
      </div>
      {showConsole && (
        <div className="max-h-[40%] overflow-y-auto border-t border-line p-2 text-xs" data-testid="preview-console">
          <label className="mb-1 flex items-center gap-2 text-muted">
            <input type="checkbox" checked={autoReload} onChange={(event) => setAutoReload(event.target.checked)} />
            {t("reloadAfterTurn")}
          </label>
          {running.length === 0 && <p className="text-muted">{t("noProgramRunning")}</p>}
          {running.map((program) => (
            <Output key={program.id} api={api} program={program} />
          ))}
        </div>
      )}
    </section>
  );
}

const PREVIEW_WIDTH_KEY = "forge.previewWidth";

function storedWidth(): number {
  try {
    return Number(window.localStorage.getItem(PREVIEW_WIDTH_KEY)) || 560;
  } catch {
    return 560;
  }
}

/** The preview next to the chat (W47): its width, dragged at its left edge, and whether it is
open; it opens by itself when a dev server starts listening while the agent works. */
export function usePreviewPane(projectId: string, running: boolean, turns: number, given?: ProjectApi) {
  const [open, setOpen] = useState(false);
  const [port, setPort] = useState<number | null>(null);
  const [width, setWidth] = useState(storedWidth);
  const api = useMemo(() => given ?? projectApi(projectId), [given, projectId]);
  const { overview, refresh } = usePreviewOverview(api, !open && running);
  const known = useRef<number[] | null>(null);
  const lookAgain = useRef(() => {});
  lookAgain.current = () => {
    if (!open) void refresh(); // a server started in the last moments of a turn
  };
  useEffect(() => {
    if (turns > 0) lookAgain.current();
  }, [turns]);
  useEffect(() => {
    if (!overview) return;
    const now = overview.ports.map((p) => p.port);
    const fresh = known.current === null ? [] : newPorts(known.current, now);
    known.current = now;
    if (fresh.length > 0 && overview.enabled) {
      setPort(fresh[0]);
      setOpen(true);
    }
  }, [overview]);
  const resize = (next: number) => {
    const clamped = Math.round(Math.min(Math.max(next, 320), window.innerWidth * 0.75));
    setWidth(clamped);
    try {
      window.localStorage.setItem(PREVIEW_WIDTH_KEY, String(clamped));
    } catch {
      // private mode: the width just resets
    }
  };
  return { open, setOpen, port, width, resize };
}

/** The handle between the chat and the preview: drag it to share the room. */
export function SplitHandle({ onResize }: { onResize: (width: number) => void }) {
  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={t("resizePreview")}
      className="hidden w-1.5 shrink-0 cursor-col-resize bg-line/40 hover:bg-accent/40 lg:block"
      onPointerDown={(event) => {
        event.currentTarget.setPointerCapture(event.pointerId);
        const move = (e: PointerEvent) => onResize(window.innerWidth - e.clientX);
        const up = () => {
          window.removeEventListener("pointermove", move);
          window.removeEventListener("pointerup", up);
        };
        window.addEventListener("pointermove", move);
        window.addEventListener("pointerup", up);
      }}
    />
  );
}
