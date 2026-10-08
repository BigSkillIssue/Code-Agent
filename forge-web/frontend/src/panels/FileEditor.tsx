// One open file: edit and save it (refusing to overwrite changes made meanwhile), or download it.

import { ArrowLeft, Download, Save } from "lucide-react";
import { lazy, Suspense, useCallback, useEffect, useState } from "react";
import { ApiError } from "../api/client";
import type { FileContent, ProjectApi } from "../api/project";
import { t } from "../lib/i18n";

const CodeEditor = lazy(() => import("./CodeEditor"));

interface Props {
  api: ProjectApi;
  path: string;
  canEdit: boolean;
  onClose: () => void;
  onError: (message: string) => void;
}

export function FileEditor({ api, path, canEdit, onClose, onError }: Props) {
  const [file, setFile] = useState<FileContent | null>(null);
  const [text, setText] = useState("");
  const [state, setState] = useState<"clean" | "dirty" | "saving" | "saved" | "conflict">("clean");

  const load = useCallback(async () => {
    try {
      const read = await api.read(path);
      setFile(read);
      setText(read.text ?? "");
      setState("clean");
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    }
  }, [api, path, onError]);
  useEffect(() => {
    void load();
  }, [load]);

  const save = async () => {
    if (!file || !canEdit || state === "saving") return;
    setState("saving");
    try {
      const saved = await api.save(path, text, file.mtime);
      setFile({ ...file, mtime: saved.mtime });
      setState("saved");
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) setState("conflict");
      else {
        setState("dirty");
        onError(err instanceof Error ? err.message : String(err));
      }
    }
  };
  const editable = file !== null && !file.binary && !file.truncated;
  const status = { clean: "", dirty: t("unsaved"), saving: t("saving"), saved: t("saved"), conflict: "" }[state];

  return (
    <div
      className="flex h-full flex-col"
      data-testid="file-editor"
      onKeyDown={(e) => {
        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
          e.preventDefault();
          void save();
        }
      }}
    >
      <div className="flex items-center gap-2 border-b border-line px-2 py-1.5 text-sm">
        <button type="button" title={t("back")} onClick={onClose}>
          <ArrowLeft className="size-4" />
        </button>
        <span className="min-w-0 flex-1 truncate font-mono text-xs">{path}</span>
        <span className="text-xs text-muted">{status}</span>
        <a href={api.downloadUrl(path)} title={t("download")} className="text-muted">
          <Download className="size-4" />
        </a>
        {canEdit && editable && (
          <button
            type="button"
            title={t("save")}
            disabled={state === "clean" || state === "saving"}
            className="rounded bg-accent px-2 py-0.5 text-xs text-on-accent disabled:opacity-40"
            onClick={() => void save()}
          >
            <Save className="inline size-3.5" /> {t("save")}
          </button>
        )}
      </div>
      {state === "conflict" && (
        <div className="flex items-center gap-2 border-b border-warn bg-panel px-2 py-1.5 text-xs text-warn" role="alert">
          <span className="flex-1">{t("conflict")}</span>
          <button type="button" className="underline" onClick={() => void load()}>
            {t("reload")}
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1 overflow-hidden">
        {file === null ? null : editable ? (
          <Suspense fallback={<pre className="p-2 text-xs">{text}</pre>}>
            <CodeEditor
              path={path}
              value={text}
              readOnly={!canEdit}
              onChange={(value) => {
                setText(value);
                setState("dirty");
              }}
            />
          </Suspense>
        ) : (
          <p className="p-3 text-sm text-muted">{file.binary ? t("binaryFile") : t("largeFile")}</p>
        )}
      </div>
    </div>
  );
}
