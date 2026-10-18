import { ArrowUp, FileText, Paperclip, Slash, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { Attachment } from "../api/types";
import {
  applyCompletion,
  completionAt,
  matchCommands,
  type Completion,
  type SlashCommand,
} from "../lib/completion";
import { t } from "../lib/i18n";
import { FileChip, MAX_FILE_MB, MAX_FILES } from "./Attachments";

interface Props {
  running: boolean;
  onSend: (text: string, files: Attachment[]) => Promise<void>;
  onStop: () => Promise<void>;
  commands: SlashCommand[];
  searchFiles?: (query: string) => Promise<string[]>;
  upload?: (file: File) => Promise<Attachment>; // files for the message (W45); none without it
}

interface Pending {
  key: number;
  name: string;
  size: number;
  done?: Attachment;
  problem?: string;
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

/** Files picked, pasted or dropped: uploaded at once, each shown as a chip until the message goes. */
function usePendingFiles(upload: Props["upload"]) {
  const [files, setFiles] = useState<Pending[]>([]);
  const next = useRef(0);
  const change = (key: number, update: Partial<Pending>) =>
    setFiles((all) => all.map((f) => (f.key === key ? { ...f, ...update } : f)));
  const add = (list: FileList | File[]) => {
    if (!upload) return;
    const room = MAX_FILES - files.length;
    for (const [index, file] of Array.from(list).entries()) {
      const key = next.current++;
      if (index >= room) {
        setFiles((all) => [...all, { key, name: file.name, size: file.size, problem: t("tooManyFiles", { n: String(MAX_FILES) }) }]);
        continue;
      }
      if (file.size > MAX_FILE_MB * 1024 * 1024) {
        setFiles((all) => [...all, { key, name: file.name, size: file.size, problem: t("fileTooBig", { mb: String(MAX_FILE_MB) }) }]);
        continue;
      }
      setFiles((all) => [...all, { key, name: file.name || t("pastedPicture"), size: file.size }]);
      upload(file)
        .then((done) => change(key, { done }))
        .catch((err: Error) => change(key, { problem: err.message }));
    }
  };
  const remove = (key: number) => setFiles((all) => all.filter((f) => f.key !== key));
  return { files, add, remove, clear: () => setFiles([]) };
}

export function Composer({ running, onSend, onStop, commands, searchFiles, upload }: Props) {
  const [text, setText] = useState("");
  const [caret, setCaret] = useState(0);
  const [sending, setSending] = useState(false);
  const [active, setActive] = useState(0);
  const [dismissed, setDismissed] = useState<string | null>(null); // Esc closes for this text
  const box = useRef<HTMLTextAreaElement>(null);
  const picker = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);
  const pending = usePendingFiles(upload);
  const uploading = pending.files.some((f) => !f.done && !f.problem);
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
    if (!message || running || sending || uploading) return;
    setSending(true);
    try {
      await onSend(message, pending.files.flatMap((f) => (f.done ? [f.done] : [])));
      edit("", 0);
      pending.clear();
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
      <div
        data-testid="composer"
        className={`relative flex flex-wrap items-end gap-2 rounded-2xl border bg-card p-2 shadow-sm ${dragging ? "border-accent ring-2 ring-accent/30" : "border-line"}`}
        onDragOver={(e) => {
          if (!upload || !Array.from(e.dataTransfer.types).includes("Files")) return;
          e.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          if (!upload) return;
          e.preventDefault();
          setDragging(false);
          pending.add(e.dataTransfer.files);
        }}
      >
        {pending.files.length > 0 && (
          <div className="flex w-full flex-wrap gap-1.5 px-1 pt-1">
            {pending.files.map((f) => (
              <FileChip
                key={f.key}
                name={f.name}
                size={f.size}
                busy={!f.done && !f.problem}
                problem={f.problem}
                onRemove={() => pending.remove(f.key)}
              />
            ))}
          </div>
        )}
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
        {upload && (
          <>
            <button
              type="button"
              title={t("attachFiles")}
              aria-label={t("attachFiles")}
              className="rounded-full p-2 text-muted hover:text-fg"
              onClick={() => picker.current?.click()}
            >
              <Paperclip className="size-4" />
            </button>
            <input
              ref={picker}
              type="file"
              multiple
              hidden
              data-testid="file-picker"
              onChange={(e) => {
                if (e.target.files) pending.add(e.target.files);
                e.target.value = ""; // the same file can be picked again
              }}
            />
          </>
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
          onPaste={(e) => {
            if (!upload || e.clipboardData.files.length === 0) return;
            e.preventDefault(); // a pasted picture becomes a file, not text
            pending.add(e.clipboardData.files);
          }}
        />
        {running ? (
          <button type="button" title={t("stop")} className="rounded-full bg-fg p-2 text-bg" onClick={() => void onStop()}>
            <Square className="size-4" />
          </button>
        ) : (
          <button
            type="button"
            title={t("send")}
            disabled={!text.trim() || sending || uploading}
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
