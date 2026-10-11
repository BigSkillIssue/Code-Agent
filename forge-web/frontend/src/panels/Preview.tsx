// The live preview: start a dev server, see the ports it listens on, and look at the app.
// The app runs on a host of its own; Forge opens it with a one-time ticket URL from the server.

import { ArrowLeft, ExternalLink, Play, RotateCw, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { PreviewOverview, Program, ProjectApi } from "../api/project";
import { t } from "../lib/i18n";

const POLL_MS = 2500;
const OUTPUT_LINES = 200;

interface Props {
  api: ProjectApi;
  canEdit: boolean;
  active: boolean; // the preview tab is showing
  onError: (message: string) => void;
}

interface Shown {
  port: number;
  path: string;
  url: string; // the ticket URL the frame loads
}

/** The ticket URL, landing on `path` instead of the start page. */
export function landingOn(url: string, path: string): string {
  const parsed = new URL(url);
  parsed.searchParams.set("next", path.startsWith("/") ? path : `/${path}`);
  return parsed.toString();
}

/** The ports with the app's web client first (app projects), the rest as they are. */
export function appFirst<P extends { port: number }>(ports: P[], appPort?: number | null): P[] {
  if (!appPort) return ports;
  return [...ports.filter((p) => p.port === appPort), ...ports.filter((p) => p.port !== appPort)];
}

/** The command a program runs, as the user typed it. */
export function commandOf(program: Program): string {
  const [shell, flag, line] = program.argv;
  if (line !== undefined && /(^|\/)(ba)?sh$/.test(shell) && flag.endsWith("c")) return line;
  return program.argv.join(" ");
}

export function PreviewPanel({ api, canEdit, active, onError }: Props) {
  const [overview, setOverview] = useState<PreviewOverview | null>(null);
  const [shown, setShown] = useState<Shown | null>(null);
  const lastError = useRef("");

  const refresh = useCallback(async () => {
    try {
      setOverview(await api.preview());
      lastError.current = "";
    } catch (err) {
      const message = (err as Error).message;
      if (message !== lastError.current) onError(message); // once, not on every poll
      lastError.current = message;
    }
  }, [api, onError]);

  useEffect(() => {
    if (!active) return;
    void refresh();
    const timer = setInterval(() => void refresh(), POLL_MS);
    return () => clearInterval(timer);
  }, [active, refresh]);

  const show = async (port: number, path = "/") => {
    try {
      const { url } = await api.openPreview(port);
      setShown({ port, path, url: landingOn(url, path) });
    } catch (err) {
      onError((err as Error).message);
    }
  };

  const openTab = async (port: number, path: string) => {
    const tab = window.open("about:blank", "_blank"); // opened now, while the click still counts
    try {
      const { url } = await api.openPreview(port);
      if (tab) {
        tab.opener = null; // the preview must not reach back into Forge
        tab.location.href = landingOn(url, path);
      }
    } catch (err) {
      tab?.close();
      onError((err as Error).message);
    }
  };

  if (overview === null) return <p className="p-3 text-sm text-muted">{t("connecting")}</p>;
  if (shown) {
    return (
      <PreviewFrame
        shown={shown}
        onBack={() => setShown(null)}
        onGo={(path) => void show(shown.port, path)}
        onTab={(path) => void openTab(shown.port, path)}
      />
    );
  }
  return (
    <div className="h-full overflow-y-auto p-3 text-sm" data-testid="preview">
      {!overview.enabled && <p className="mb-3 rounded border border-warn p-2 text-xs">{t("previewOff")}</p>}
      <Programs api={api} canEdit={canEdit} overview={overview} onChange={refresh} onError={onError} />
      <h3 className="mt-4 mb-1 text-xs font-medium uppercase text-muted">{t("ports")}</h3>
      {overview.app_port && <p className="mb-1 text-xs text-muted">{t("appPort", { port: String(overview.app_port) })}</p>}
      {overview.ports.length === 0 && <p className="text-xs text-muted">{t("noPorts")}</p>}
      <div className="flex flex-wrap gap-2">
        {appFirst(overview.ports, overview.app_port).map(({ port }) => (
          <span key={port} className="flex items-center rounded border border-line">
            <button
              type="button"
              className="px-2 py-1 disabled:opacity-50"
              disabled={!overview.enabled}
              onClick={() => void show(port)}
            >
              {port === overview.app_port ? `${t("appPortLabel")} · ` : ""}
              {t("showPort", { port: String(port) })}
            </button>
            <button
              type="button"
              title={t("openInTab")}
              className="border-l border-line px-1.5 py-1 text-muted disabled:opacity-50"
              disabled={!overview.enabled}
              onClick={() => void openTab(port, "/")}
            >
              <ExternalLink className="size-3.5" />
            </button>
          </span>
        ))}
      </div>
    </div>
  );
}

interface ProgramsProps {
  api: ProjectApi;
  canEdit: boolean;
  overview: PreviewOverview;
  onChange: () => Promise<void>;
  onError: (message: string) => void;
}

function Programs({ api, canEdit, overview, onChange, onError }: ProgramsProps) {
  const [command, setCommand] = useState("");
  const [outputOf, setOutputOf] = useState<string | null>(null);

  const run = async (action: () => Promise<unknown>) => {
    try {
      await action();
      await onChange();
    } catch (err) {
      onError((err as Error).message);
    }
  };
  const start = (line: string) => void run(() => api.startProgram(line));
  const running = overview.programs.filter((p) => p.running);
  return (
    <section>
      <h3 className="mb-1 text-xs font-medium uppercase text-muted">{t("devServer")}</h3>
      {canEdit && (
        <>
          <div className="mb-2 flex flex-wrap gap-2">
            {overview.suggestions.map((s) => (
              <button
                key={s.command}
                type="button"
                title={s.label}
                className="flex items-center gap-1 rounded border border-line px-2 py-1 font-mono text-xs"
                onClick={() => start(s.command)}
              >
                <Play className="size-3" /> {s.command}
              </button>
            ))}
          </div>
          <form
            className="mb-2 flex gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (command.trim()) start(command.trim());
              setCommand("");
            }}
          >
            <input
              aria-label={t("ownCommand")}
              placeholder={t("ownCommand")}
              className="min-w-0 flex-1 rounded border border-line bg-card px-2 py-1 font-mono text-xs"
              value={command}
              onChange={(event) => setCommand(event.target.value)}
            />
            <button type="submit" className="rounded border border-line px-2 py-1 text-xs">
              {t("startProgram")}
            </button>
          </form>
        </>
      )}
      <ul className="space-y-1">
        {running.map((program) => (
          <li key={program.id} className="rounded border border-line p-1.5">
            <div className="flex items-center gap-2">
              <span className="size-2 shrink-0 rounded-full bg-ok" aria-label={t("running")} />
              <code className="min-w-0 flex-1 truncate text-xs">{commandOf(program)}</code>
              <button
                type="button"
                className="text-xs text-muted"
                onClick={() => setOutputOf(outputOf === program.id ? null : program.id)}
              >
                {t("output")}
              </button>
              {canEdit && (
                <button
                  type="button"
                  title={t("stopProgram")}
                  className="text-bad"
                  onClick={() => void run(() => api.stopProgram(program.id))}
                >
                  <Square className="size-3.5" />
                </button>
              )}
            </div>
            {outputOf === program.id && <Output api={api} program={program} />}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Output({ api, program }: { api: ProjectApi; program: Program }) {
  const [lines, setLines] = useState<string[]>([]);
  const since = Math.max(0, program.total_lines - OUTPUT_LINES);
  useEffect(() => {
    let current = true;
    api
      .programOutput(program.id, since)
      .then((found) => current && setLines(found.lines))
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [api, program.id, since]);
  return (
    <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap rounded bg-code p-1.5 text-xs" data-testid="output">
      {lines.join("\n") || "…"}
    </pre>
  );
}

interface FrameProps {
  shown: Shown;
  onBack: () => void;
  onGo: (path: string) => void;
  onTab: (path: string) => void;
}

function PreviewFrame({ shown, onBack, onGo, onTab }: FrameProps) {
  const [path, setPath] = useState(shown.path);
  return (
    <div className="flex h-full flex-col" data-testid="preview-frame">
      <form
        className="flex items-center gap-1 border-b border-line p-1"
        onSubmit={(event) => {
          event.preventDefault();
          onGo(path);
        }}
      >
        <button type="button" title={t("back")} className="p-1 text-muted" onClick={onBack}>
          <ArrowLeft className="size-4" />
        </button>
        <span className="px-1 text-xs text-muted">:{shown.port}</span>
        <input
          aria-label={t("path")}
          className="min-w-0 flex-1 rounded border border-line bg-card px-2 py-0.5 font-mono text-xs"
          value={path}
          onChange={(event) => setPath(event.target.value)}
        />
        <button type="submit" title={t("reloadPreview")} className="p-1 text-muted">
          <RotateCw className="size-4" />
        </button>
        <button type="button" title={t("openInTab")} className="p-1 text-muted" onClick={() => onTab(path)}>
          <ExternalLink className="size-4" />
        </button>
      </form>
      <iframe
        key={shown.url}
        title={t("preview")}
        src={shown.url}
        className="min-h-0 flex-1 bg-white"
        // Its own site already; the sandbox also keeps it from navigating Forge's tab away.
        sandbox="allow-scripts allow-same-origin allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox allow-downloads"
        referrerPolicy="no-referrer"
      />
    </div>
  );
}
