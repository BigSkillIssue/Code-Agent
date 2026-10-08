import { Loader2, PanelRight, Trash2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import type { Chat, ChatMode, StoredItem } from "../api/types";
import { BUILTIN_COMMANDS, type SlashCommand } from "../lib/completion";
import { t } from "../lib/i18n";
import { useStore } from "../state/store";
import { buildTranscript } from "../state/transcript";
import { ProjectPanel } from "../panels/ProjectPanel";
import { Composer } from "./Composer";
import { TodoPanel } from "./TodoPanel";
import { Transcript } from "./Transcript";

const MODES: { value: ChatMode; label: string }[] = [
  { value: "ask", label: t("modeAsk") },
  { value: "edits", label: t("modeEdits") },
  { value: "auto", label: t("modeAuto") },
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
    <select
      aria-label={t("model")}
      className="max-w-[11rem] min-w-0 rounded-md border border-line bg-card px-2 py-1 text-sm"
      value={chat.model}
      disabled={!chat.mine}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{t("defaultModel")}</option>
      {!known && chat.model && <option value={chat.model}>{chat.model}</option>}
      {(models ?? []).map((m) => (
        <option key={m.id} value={m.id}>
          {m.model} ({m.key === "own" ? t("ownKey") : t("serverKey")})
        </option>
      ))}
    </select>
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
  const { openChat, closeChat, send, answer, cancel, updateChat, deleteChat, setError, searchFiles } = useStore();
  const navigate = useNavigate();

  useEffect(() => {
    openChat(chat.id);
    return () => closeChat(chat.id);
  }, [chat.id, openChat, closeChat]);

  const transcript = useMemo(() => buildTranscript(data?.items ?? [], data?.live), [data?.items, data?.live]);
  const commands = useMemo(() => chatCommands(data?.items ?? []), [data?.items]);
  const findFiles = useCallback((query: string) => searchFiles(chat.project_id, query), [searchFiles, chat.project_id]);
  const running = chat.state === "running" || chat.state === "waiting";
  const project = useStore((s) => s.projects.find((p) => p.id === chat.project_id));
  const [panel, setPanel] = useState(storedPanel);
  const turns = useTurnCounter(chat.state);
  const togglePanel = (open: boolean) => {
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
        <header className="flex flex-wrap items-center gap-2 border-b border-line px-3 py-2 sm:px-4">
          <h1 className="min-w-0 flex-1 truncate font-medium" data-testid="chat-title">
            {chat.title}
          </h1>
          {running && (
            <span className="flex items-center gap-1 text-xs text-muted">
              <Loader2 className="size-3 animate-spin" />
              {chat.state === "waiting" ? t("waiting") : t("working")}
            </span>
          )}
          <select
            aria-label={t("mode")}
            className="min-w-0 rounded-md border border-line bg-card px-2 py-1 text-sm"
            value={chat.mode}
            disabled={!chat.mine}
            onChange={(e) => void guard(() => updateChat(chat, { mode: e.target.value as ChatMode }))}
          >
            {MODES.map((mode) => (
              <option key={mode.value} value={mode.value}>
                {mode.label}
              </option>
            ))}
          </select>
          <ModelPicker chat={chat} onChange={(model) => void guard(() => updateChat(chat, { model }))} />
          <button
            type="button"
            title={t("showPanel")}
            aria-pressed={panel}
            className={`rounded p-1 ${panel ? "text-accent" : "text-muted"}`}
            onClick={() => togglePanel(!panel)}
          >
            <PanelRight className="size-4" />
          </button>
          {chat.mine && (
            <button
              type="button"
              title={t("deleteChat")}
              className="rounded p-1 text-muted hover:text-bad"
              onClick={() => {
                if (window.confirm(t("confirmDelete"))) {
                  void guard(async () => {
                    await deleteChat(chat);
                    navigate(`/p/${chat.project_id}`);
                  });
                }
              }}
            >
              <Trash2 className="size-4" />
            </button>
          )}
        </header>
        <main className="flex-1 overflow-y-auto">
          <Transcript entries={transcript.entries} onAnswer={chat.mine ? (id, value) => answer(chat.id, id, value) : undefined} />
        </main>
        <TodoPanel todos={transcript.todos} />
        {chat.mine && (
          <Composer
            running={running}
            commands={commands}
            searchFiles={findFiles}
            onSend={(text) => guard(() => send(chat.id, text)).then(() => undefined)}
            onStop={() => guard(() => cancel(chat.id)).then(() => undefined)}
          />
        )}
      </div>
      {panel && (
        <div className="fixed inset-0 z-30 border-line lg:static lg:z-auto lg:w-[clamp(22rem,42vw,46rem)] lg:border-l">
          <ProjectPanel
            projectId={chat.project_id}
            canEdit={project?.role !== "viewer"}
            refreshKey={turns}
            onClose={() => togglePanel(false)}
            onError={setError}
          />
        </div>
      )}
    </div>
  );
}
