import { ArrowUp, FileText, Slash, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import {
  applyCompletion,
  completionAt,
  matchCommands,
  type Completion,
  type SlashCommand,
} from "../lib/completion";
import { t } from "../lib/i18n";

interface Props {
  running: boolean;
  onSend: (text: string) => Promise<void>;
  onStop: () => Promise<void>;
  commands: SlashCommand[];
  searchFiles?: (query: string) => Promise<string[]>;
}

interface Suggestion {
  value: string; // what is filled in
  label: string;
  hint: string;
}

const FILE_DELAY_MS = 150;

/** Suggestions for what the caret completes: matching commands, or files from the project. */
function useSuggestions(completion: Completion | null, commands: SlashCommand[], searchFiles?: Props["searchFiles"]) {
  const [files, setFiles] = useState<{ query: string; found: string[] } | null>(null);
  const fileQuery = completion?.kind === "file" ? completion.query : null;
  useEffect(() => {
    if (fileQuery === null || !searchFiles) return;
    let current = true;
    const timer = setTimeout(() => {
      searchFiles(fileQuery)
        .then((found) => current && setFiles({ query: fileQuery, found }))
        .catch(() => current && setFiles({ query: fileQuery, found: [] }));
    }, FILE_DELAY_MS);
    return () => {
      current = false;
      clearTimeout(timer);
    };
  }, [fileQuery, searchFiles]);
  if (completion?.kind === "command") {
    return matchCommands(commands, completion.query).map((c) => ({ value: c.name, label: c.usage, hint: c.help }));
  }
  if (completion?.kind === "file" && files && files.query === completion.query) {
    return files.found.map((path) => ({ value: path, label: path, hint: "" }));
  }
  return [];
}

export function Composer({ running, onSend, onStop, commands, searchFiles }: Props) {
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [sending, setSending] = useState(false);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState<string | null>(null); // Esc closes for this text
  const box = useRef<HTMLTextAreaElement>(null);
  const completion = dismissed === text ? null : completionAt(text, caret);
  const suggestions: Suggestion[] = useSuggestions(completion, commands, searchFiles);
  const open = completion !== null && suggestions.length > 0;
  const chosen = Math.min(active, Math.max(suggestions.length - 1, 0));

  const edit = (value: string, at: number) => {
    setText(value);
    setCaret(at);
    setActive(0);
  };
  const accept = (suggestion: Suggestion) => {
    if (!completion) return;
    const next = applyCompletion(text, completion, suggestion.value);
    edit(next.text, next.caret);
    requestAnimationFrame(() => box.current?.setSelectionRange(next.caret, next.caret));
  };
  const send = async () => {
    const message = text.trim();
    if (!message || running || sending) return;
    setSending(true);
    try {
      await onSend(message);
      edit("", 0);
    } finally {
      setSending(false);
    }
  };
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return;
    if (open && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      const step = e.key === "ArrowDown" ? 1 : -1;
      setActive((chosen + step + suggestions.length) % suggestions.length);
    } else if (open && (e.key === "Enter" || e.key === "Tab") && !e.shiftKey) {
      e.preventDefault();
      accept(suggestions[chosen]);
    } else if (open && e.key === "Escape") {
      e.preventDefault();
      setDismissed(text);
    } else if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  return (
    <div className="mx-auto w-full max-w-3xl px-3 pb-3 sm:px-4 sm:pb-4">
      <div className="relative flex items-end gap-2 rounded-2xl border border-line bg-card p-2 shadow-sm">
        {open && (
          <ul
            role="listbox"
            aria-label={completion?.kind === "command" ? t("commandsHint") : t("filesHint")}
            className="absolute right-0 bottom-full left-0 mb-2 max-h-64 overflow-y-auto rounded-xl border border-line bg-card p-1 shadow-lg"
          >
            {suggestions.map((s, index) => (
              <li
                key={s.value}
                role="option"
                aria-selected={index === chosen}
                className={`flex cursor-pointer items-center gap-2 rounded-lg px-2 py-1.5 text-sm ${index === chosen ? "bg-panel" : ""}`}
                onMouseDown={(e) => {
                  e.preventDefault(); // keep the focus in the text box
                  accept(s);
                }}
              >
                {completion?.kind === "command" ? (
                  <Slash className="size-3.5 shrink-0 text-muted" />
                ) : (
                  <FileText className="size-3.5 shrink-0 text-muted" />
                )}
                <span className="font-mono">{s.label}</span>
                {s.hint && <span className="truncate text-xs text-muted">{s.hint}</span>}
              </li>
            ))}
          </ul>
        )}
        <textarea
          ref={box}
          aria-label={t("placeholder")}
          className="max-h-60 min-h-[2.5rem] flex-1 resize-none bg-transparent px-2 py-2 outline-none"
          placeholder={t("placeholder")}
          rows={1}
          value={text}
          onChange={(e) => edit(e.target.value, e.target.selectionStart ?? e.target.value.length)}
          onSelect={(e) => setCaret(e.currentTarget.selectionStart ?? 0)}
          onKeyDown={onKeyDown}
        />
        {running ? (
          <button type="button" title={t("stop")} className="rounded-full bg-fg p-2 text-bg" onClick={() => void onStop()}>
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
