import { Eraser, Globe, Loader2, MonitorPlay, PanelRight } from "lucide-react";
import { type CSSProperties, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router-dom";
import type { Chat, ChatEffort, ChatMode, StoredItem } from "../api/types";
import { BUILTIN_COMMANDS, type SlashCommand } from "../lib/completion";
import { t } from "../lib/i18n";
import { useStore } from "../state/store";
import { buildTranscript } from "../state/transcript";
import { ProjectPanel } from "../panels/ProjectPanel";
import { BrowserPreview, SplitHandle, usePreviewPane } from "./BrowserPreview";
import { Composer } from "./Composer";
import { GuideBar } from "./Guide";
import { LogoMark } from "./Logo";
import { Select } from "./Select";
import { TodoPanel } from "./TodoPanel";
import { Transcript } from "./Transcript";

const MODES: { value: ChatMode; label: string }[] = [
  { value: "ask", label: t("modeAsk") },
  { value: "edits", label: t("modeEdits") },
  { value: "auto", label: t("modeAuto") },
];

// How hard the model thinks (W45), as Forge's effort; models that do not reason ignore it.
const EFFORTS: { value: ChatEffort; label: string; description: string }[] = [
  { value: "auto", label: t("effortAuto"), description: t("effortAutoHint") },
  { value: "low", label: t("effortLow"), description: t("effortLowHint") },
  { value: "medium", label: t("effortMedium"), description: t("effortMediumHint") },
  { value: "high", label: t("effortHigh"), description: t("effortHighHint") },
];

/** The slash commands the chat's worker announced last (its own and the project's). */
export function chatCommands(items: StoredItem[]): SlashCommand[] {
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i].item;
    if (item.type === "ready" && item.commands && item.commands.length > 0) return item.commands;
  }
  return BUILTIN_COMMANDS;
}

function ModelPicker({ chat, onChange }: { chat: Chat; onChange: (model: string) => void }) {
  const { models, loadModels } = useStore();
  useEffect(() => {
    if (models === null) void loadModels().catch(() => undefined);
  }, [models, loadModels]);
  const known = (models ?? []).some((m) => m.id === chat.model);
  return (
    <Select
      compact
      ariaLabel={t("model")}
      className="max-w-[12rem] min-w-0"
      value={chat.model}
      disabled={!chat.can_write}
      onChange={onChange}
      options={[
        { value: "", label: t("defaultModel") },
        ...(!known && chat.model ? [{ value: chat.model, label: chat.model }] : []),
        ...(models ?? []).map((m) => ({
          value: m.id,
          label: m.model,
          description: m.key === "own" ? t("ownKey") : t("serverKey"),
        })),
      ]}
    />
  );
}

const PANEL_KEY = "forge.panel";

function storedPanel(): boolean {
  try {
    return window.localStorage.getItem(PANEL_KEY) === "open";
  } catch {
    return false;
  }
}

/** The guided start's steps 3 and 4 (W72), when the onboarding page sent the chat a first task;
 * the router's state is cleared at once, so a reload does not show them again. */
function useGuide() {
  const location = useLocation();
  const navigate = useNavigate();
  const [prompt] = useState<string>(() => (location.state as { guide?: { prompt?: string } } | null)?.guide?.prompt ?? "");
  const [step, setStep] = useState<0 | 3 | 4>(prompt ? 3 : 0);
  useEffect(() => {
    if (prompt) navigate(location.pathname, { replace: true, state: null });
  }, [prompt, navigate, location.pathname]);
  return { prompt, step, setStep };
}

/** A number that grows each time a turn ends (the agent may have changed files). */
function useTurnCounter(state: Chat["state"]): number {
  const [count, setCount] = useState(0);
  const last = useRef(state);
  useEffect(() => {
    if ((last.current === "running" || last.current === "waiting") && state === "idle") setCount((n) => n + 1);
    last.current = state;
  }, [state]);
  return count;
}

export function ChatView({ chat }: { chat: Chat }) {
  const data = useStore((s) => s.chatData[chat.id]);
  const { openChat, closeChat, send, upload, answer, cancel, updateChat, clearChat, setError, searchFiles } = useStore();

  useEffect(() => {
    openChat(chat.id);
    return () => closeChat(chat.id);
  }, [chat.id, openChat, closeChat]);

  const transcript = useMemo(() => buildTranscript(data?.items ?? [], data?.live), [data?.items, data?.live]);
  const commands = useMemo(() => chatCommands(data?.items ?? []), [data?.items]);
  const running = chat.state === "running" || chat.state === "waiting";
  const project = useStore((s) => s.projects.find((p) => p.id === chat.project_id));
  const codeAccess = project?.code_access !== false; // W57: no file names for @-mentions without it
  const findFiles = useCallback(
    (query: string) => (codeAccess ? searchFiles(chat.project_id, query) : Promise.resolve([])),
    [searchFiles, chat.project_id, codeAccess],
  );
  const modelChoice = useStore((s) => s.user?.model_choice !== false);
  // Forge Web's product flow (W55): the agent plans and steers; people answer, stop and undo.
  const agentSteers = useStore((s) => s.user?.product_flow === true);
  const [panel, setPanel] = useState(storedPanel);
  const guide = useGuide();
  const turns = useTurnCounter(chat.state);
  const preview = usePreviewPane(chat.project_id, running, turns);
  const showPreview = preview.open && !panel; // one pane next to the chat at a time
  const togglePanel = (open: boolean) => {
    if (open) preview.setOpen(false);
    setPanel(open);
    try {
      window.localStorage.setItem(PANEL_KEY, open ? "open" : "closed");
    } catch {
      // private mode: the panel just forgets
    }
  };
  const guard = async (work: () => Promise<unknown>) => {
    try {
      await work();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="flex h-full min-w-0 flex-1">
      <div className="flex h-full min-w-0 flex-1 flex-col">
        <header className="flex min-h-14 flex-wrap items-center gap-2 border-b border-line px-3 py-2 sm:px-5">
          <h1 className="min-w-[8rem] flex-1 truncate text-[23px] leading-tight font-bold" data-testid="chat-title">
            {chat.title === "New chat" ? (project?.name ?? chat.title) : chat.title}
          </h1>
          {running && (
            <span className="flex items-center gap-1 text-xs text-muted">
              <Loader2 className="size-3 animate-spin" />
              {chat.state === "waiting" ? t("waiting") : t("working")}
            </span>
          )}
          {!agentSteers && (
            <Select
              compact
              ariaLabel={t("mode")}
              className="min-w-0"
              value={chat.mode}
              disabled={!chat.can_write}
              onChange={(mode) => void guard(() => updateChat(chat, { mode }))}
              options={MODES}
            />
          )}
          {modelChoice && <ModelPicker chat={chat} onChange={(model) => void guard(() => updateChat(chat, { model }))} />}
          {!agentSteers && (
            <Select
              compact
              ariaLabel={t("effort")}
              className="min-w-0"
              value={chat.effort}
              disabled={!chat.can_write}
              onChange={(effort) => void guard(() => updateChat(chat, { effort }))}
              options={EFFORTS}
            />
          )}
          {project?.kind === "app" && (
            <Link to={`/p/${chat.project_id}/live`} title={t("goLiveTitle")} className="rounded-md p-1.5 text-muted hover:bg-panel hover:text-accent">
              <Globe className="size-4" />
            </Link>
          )}
          <button
            type="button"
            title={t("preview")}
            aria-label={t("preview")}
            aria-pressed={showPreview}
            className={`rounded-md p-1.5 ${showPreview ? "bg-accent-soft text-accent" : "text-muted hover:bg-panel hover:text-fg"} ${guide.step === 4 ? "ring-2 ring-accent ring-offset-2 ring-offset-bg" : ""}`}
            onClick={() => {
              if (!showPreview) togglePanel(false);
              preview.setOpen(!showPreview);
            }}
          >
            <MonitorPlay className="size-4" />
          </button>
          <button
            type="button"
            title={t("showPanel")}
            aria-pressed={panel}
            className={`rounded-md p-1.5 ${panel ? "bg-accent-soft text-accent" : "text-muted hover:bg-panel hover:text-fg"}`}
            onClick={() => togglePanel(!panel)}
          >
            <PanelRight className="size-4" />
          </button>
          {chat.can_write && (
            <button
              type="button"
              title={t("clearChat")}
              aria-label={t("clearChat")}
              className="rounded-md p-1.5 text-muted hover:bg-panel hover:text-bad"
              onClick={() => {
                if (window.confirm(t("confirmClear"))) void guard(() => clearChat(chat));
              }}
            >
              <Eraser className="size-4" />
            </button>
          )}
        </header>
        <main className="flex-1 overflow-y-auto">
          {transcript.entries.length === 0 ? (
            <div className="flex min-h-full flex-col items-center justify-center p-8">
              <div className="ember-glow-center flex flex-col items-center gap-4 px-16 py-12 text-center">
                <LogoMark size={56} className="text-line-strong" />
                <p className="max-w-[34ch] text-muted">{t("authTagline")}</p>
              </div>
            </div>
          ) : (
            <Transcript
              entries={transcript.entries}
              onAnswer={chat.can_write ? (id, value) => answer(chat.id, id, value) : undefined}
              approvalPage={project?.kind === "apple" ? `/p/${chat.project_id}/apple` : undefined}
              projectId={chat.project_id}
            />
          )}
        </main>
        <TodoPanel todos={transcript.todos} />
        {guide.step > 0 && <GuideBar step={guide.step === 3 ? 3 : 4} onNext={() => guide.setStep(4)} onClose={() => guide.setStep(0)} />}
        {chat.can_write && (
          <Composer
            running={running}
            commands={commands}
            searchFiles={findFiles}
            onSend={(text, files) => guard(() => send(chat.id, text, files)).then(() => undefined)}
            upload={(file) => upload(chat.id, file)}
            initialText={guide.prompt}
            onStop={() => guard(() => cancel(chat.id)).then(() => undefined)}
          />
        )}
      </div>
      {showPreview && (
        <>
          <SplitHandle onResize={preview.resize} />
          <div
            className="fixed inset-0 z-30 lg:static lg:z-auto lg:w-[var(--preview-width)] lg:shrink-0"
            style={{ "--preview-width": `${preview.width}px` } as CSSProperties}
          >
            <BrowserPreview
              projectId={chat.project_id}
              canEdit={project?.role !== "viewer"}
              turns={turns}
              port={preview.port}
              onClose={() => preview.setOpen(false)}
              onError={setError}
            />
          </div>
        </>
      )}
      {panel && (
        <div className="fixed inset-0 z-30 border-line lg:static lg:z-auto lg:w-[clamp(22rem,42vw,46rem)] lg:border-l">
          <ProjectPanel
            projectId={chat.project_id}
            canEdit={project?.role !== "viewer"}
            apple={project?.kind === "apple"}
            codeAccess={project?.code_access !== false}
            refreshKey={turns}
            onClose={() => togglePanel(false)}
            onError={setError}
          />
        </div>
      )}
    </div>
  );
}
