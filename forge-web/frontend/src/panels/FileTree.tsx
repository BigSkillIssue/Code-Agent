// The project's folders and files: open folders on click, open files in the editor.

import { ChevronDown, ChevronRight, File, FilePlus, FolderPlus, Pencil, RefreshCw, Trash2, Upload } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import type { FileEntry, ProjectApi } from "../api/project";
import { t } from "../lib/i18n";

interface Props {
  api: ProjectApi;
  canEdit: boolean;
  refreshKey: number;
  onOpen: (path: string) => void;
  onError: (message: string) => void;
}

const join = (dir: string, name: string) => (dir ? `${dir}/${name}` : name);
const HIDDEN = new Set([".git"]);

export function FileTree({ api, canEdit, refreshKey, onOpen, onError }: Props) {
  const [children, setChildren] = useState<Record<string, FileEntry[]>>({});
  const [open, setOpen] = useState<Set<string>>(new Set([""]));
  const upload = useRef<HTMLInputElement>(null);
  const fail = useCallback((err: unknown) => onError(err instanceof Error ? err.message : String(err)), [onError]);

  const load = useCallback(
    async (dir: string) => {
      const listing = await api.list(dir);
      setChildren((all) => ({ ...all, [dir]: listing.entries.filter((e) => !(dir === "" && HIDDEN.has(e.name))) }));
    },
    [api],
  );
  useEffect(() => {
    // Reload the open folders whenever the chat may have changed files (refreshKey).
    for (const dir of open) load(dir).catch(fail);
  }, [refreshKey, load]); // eslint-disable-line react-hooks/exhaustive-deps

  const toggle = (dir: string) => {
    const next = new Set(open);
    if (next.has(dir)) next.delete(dir);
    else {
      next.add(dir);
      load(dir).catch(fail);
    }
    setOpen(next);
  };
  const reloadAll = () => Promise.all([...open].map((dir) => load(dir))).catch(fail);
  const create = async (folder: boolean) => {
    const name = window.prompt(t("namePrompt"))?.trim();
    if (!name) return;
    try {
      if (folder) await api.mkdir(name);
      else await api.save(name, "");
      await reloadAll();
      if (!folder) onOpen(name);
    } catch (err) {
      fail(err);
    }
  };
  const rename = async (path: string) => {
    const target = window.prompt(t("rename"), path)?.trim();
    if (!target || target === path) return;
    await api.rename(path, target).then(reloadAll).catch(fail);
  };
  const remove = async (path: string) => {
    if (!window.confirm(t("confirmRemove", { name: path }))) return;
    await api.remove(path).then(reloadAll).catch(fail);
  };
  const sendFiles = async (files: FileList | null) => {
    for (const file of Array.from(files ?? [])) await api.upload(file.name, file).catch(fail);
    await reloadAll();
  };

  const rows = (dir: string, depth: number): React.ReactNode =>
    (children[dir] ?? []).map((entry) => {
      const path = join(dir, entry.name);
      const isDir = entry.type === "dir";
      return (
        <div key={path}>
          <div className="group flex items-center gap-1 rounded px-1 py-0.5 hover:bg-panel" style={{ paddingLeft: depth * 12 + 4 }}>
            <button
              type="button"
              className="flex min-w-0 flex-1 items-center gap-1 text-left"
              onClick={() => (isDir ? toggle(path) : onOpen(path))}
            >
              {isDir ? (
                open.has(path) ? <ChevronDown className="size-3.5 shrink-0" /> : <ChevronRight className="size-3.5 shrink-0" />
              ) : (
                <File className="size-3.5 shrink-0 text-muted" />
              )}
              <span className={`truncate ${entry.type === "symlink" ? "text-muted italic" : ""}`}>{entry.name}</span>
            </button>
            {canEdit && (
              <span className="hidden gap-1 group-hover:flex">
                <button type="button" title={t("rename")} onClick={() => void rename(path)}>
                  <Pencil className="size-3.5 text-muted" />
                </button>
                <button type="button" title={t("remove")} onClick={() => void remove(path)}>
                  <Trash2 className="size-3.5 text-muted hover:text-bad" />
                </button>
              </span>
            )}
          </div>
          {isDir && open.has(path) && rows(path, depth + 1)}
        </div>
      );
    });

  return (
    <div className="flex h-full flex-col text-sm" data-testid="file-tree">
      <div className="flex items-center gap-2 border-b border-line px-2 py-1.5 text-muted">
        {canEdit && (
          <>
            <button type="button" title={t("newFile")} onClick={() => void create(false)}>
              <FilePlus className="size-4" />
            </button>
            <button type="button" title={t("newFolder")} onClick={() => void create(true)}>
              <FolderPlus className="size-4" />
            </button>
            <button type="button" title={t("upload")} onClick={() => upload.current?.click()}>
              <Upload className="size-4" />
            </button>
            <input ref={upload} type="file" multiple hidden onChange={(e) => void sendFiles(e.target.files)} />
          </>
        )}
        <button type="button" title={t("refresh")} className="ml-auto" onClick={() => void reloadAll()}>
          <RefreshCw className="size-4" />
        </button>
      </div>
      <div className="flex-1 overflow-y-auto p-1">{rows("", 0)}</div>
    </div>
  );
}
