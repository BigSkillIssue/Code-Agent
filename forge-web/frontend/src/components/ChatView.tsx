import { Loader2, Trash2 } from "lucide-react";
import { useEffect, useMemo } from "react";
import { useNavigate } from "react-router-dom";
import type { Chat, ChatMode } from "../api/types";
import { t } from "../lib/i18n";
import { useStore } from "../state/store";
import { buildTranscript } from "../state/transcript";
import { Composer } from "./Composer";
import { Transcript } from "./Transcript";

const MODES: { value: ChatMode; label: string }[] = [
  { value: "ask", label: t("modeAsk") },
  { value: "edits", label: t("modeEdits") },
  { value: "auto", label: t("modeAuto") },
];

export function ChatView({ chat }: { chat: Chat }) {
  const data = useStore((s) => s.chatData[chat.id]);
  const { openChat, closeChat, send, answer, cancel, updateChat, deleteChat, setError } = useStore();
  const navigate = useNavigate();

  useEffect(() => {
    openChat(chat.id);
    return () => closeChat(chat.id);
  }, [chat.id, openChat, closeChat]);

  const transcript = useMemo(
    () => buildTranscript(data?.items ?? [], data?.live),
    [data?.items, data?.live],
  );
  const running = chat.state === "running" || chat.state === "waiting";
  const guard = async (work: () => Promise<unknown>) => {
    try {
      await work();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="flex h-full min-w-0 flex-1 flex-col">
      <header className="flex items-center gap-3 border-b border-line px-4 py-2">
        <h1 className="truncate font-medium" data-testid="chat-title">
          {chat.title}
        </h1>
        {running && (
          <span className="flex items-center gap-1 text-xs text-muted">
            <Loader2 className="size-3 animate-spin" />
            {chat.state === "waiting" ? t("waiting") : t("working")}
          </span>
        )}
        <select
          className="ml-auto rounded-md border border-line bg-card px-2 py-1 text-sm"
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
        <Transcript
          entries={transcript.entries}
          onAnswer={chat.mine ? (id, value) => answer(chat.id, id, value) : undefined}
        />
      </main>
      {chat.mine && (
        <Composer
          running={running}
          onSend={(text) => guard(() => send(chat.id, text)).then(() => undefined)}
          onStop={() => guard(() => cancel(chat.id)).then(() => undefined)}
        />
      )}
    </div>
  );
}
