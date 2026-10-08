import { ArrowUp, Square } from "lucide-react";
import { useState } from "react";
import { t } from "../lib/i18n";

interface Props {
  running: boolean;
  onSend: (text: string) => Promise<void>;
  onStop: () => Promise<void>;
}

export function Composer({ running, onSend, onStop }: Props) {
  const [text, setText] = useState("");
  const [sending, setSending] = useState(false);
  const send = async () => {
    const message = text.trim();
    if (!message || running || sending) return;
    setSending(true);
    try {
      await onSend(message);
      setText("");
    } finally {
      setSending(false);
    }
  };
  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-4">
      <div className="flex items-end gap-2 rounded-2xl border border-line bg-card p-2 shadow-sm">
        <textarea
          aria-label={t("placeholder")}
          className="max-h-60 min-h-[2.5rem] flex-1 resize-none bg-transparent px-2 py-2 outline-none"
          placeholder={t("placeholder")}
          rows={1}
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {running ? (
          <button
            type="button"
            title={t("stop")}
            className="rounded-full bg-fg p-2 text-bg"
            onClick={() => void onStop()}
          >
            <Square className="size-4" />
          </button>
        ) : (
          <button
            type="button"
            title={t("send")}
            disabled={!text.trim() || sending}
            className="rounded-full bg-accent p-2 text-on-accent disabled:opacity-40"
            onClick={() => void send()}
          >
            <ArrowUp className="size-4" />
          </button>
        )}
      </div>
    </div>
  );
}
