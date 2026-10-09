// Creating a project: empty, cloned from a git URL, unpacked from a ZIP file, (admins) a folder
// on the server, or (when this server builds them) an Apple app from Forge's SwiftUI template.

import { X } from "lucide-react";
import { useState, type FormEvent } from "react";
import { createPortal } from "react-dom";
import { api, ApiError } from "../api/client";
import { projectApi } from "../api/project";
import type { Project } from "../api/types";
import { t, type TextKey } from "../lib/i18n";
import type { NewProject } from "../state/store";

type Source = NonNullable<NewProject["source"]>;

interface Props {
  isAdmin: boolean;
  appleApps?: boolean; // this user may build Apple apps here
  create: (body: NewProject) => Promise<Project>;
  forget: (projectId: string) => void;
  onCreated: (project: Project) => void;
  onClose: () => void;
}

const SOURCES: [Source, TextKey][] = [
  ["empty", "sourceEmpty"],
  ["git", "sourceGit"],
  ["zip", "sourceZip"],
  ["folder", "sourceFolder"],
  ["apple", "sourceApple"],
];

export function NewProjectDialog({ isAdmin, appleApps = false, create, forget, onCreated, onClose }: Props) {
  const [name, setName] = useState("");
  const [source, setSource] = useState<Source>("empty");
  const [url, setUrl] = useState("");
  const [folder, setFolder] = useState("");
  const [zip, setZip] = useState<File | null>(null);
  const [bundleId, setBundleId] = useState("");
  const [busy, setBusy] = useState<TextKey | null>(null);
  const [error, setError] = useState("");

  const ready = name.trim() !== "" && (source !== "git" || url.trim()) && (source !== "zip" || zip) && (source !== "folder" || folder.trim());
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!ready || busy) return;
    setError("");
    setBusy(source === "git" ? "cloning" : source === "zip" ? "unpacking" : "creating");
    let project: Project | null = null;
    try {
      const body: NewProject = { name: name.trim(), source, url: url.trim(), folder: folder.trim() };
      if (source === "apple") body.bundle_id = bundleId.trim();
      project = await create(body);
      if (source === "zip" && zip) await projectApi(project.id).importZip(zip);
      onCreated(project);
    } catch (err) {
      if (project && source === "zip") {
        await api.delete(`/api/projects/${project.id}`).catch(() => undefined);
        forget(project.id);
      }
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  // A portal: the sidebar may be moved with a CSS transform, which would trap a fixed overlay.
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" role="dialog" aria-modal="true" aria-label={t("newProjectTitle")}>
      <form className="w-full max-w-md space-y-3 rounded-xl border border-line bg-card p-5 shadow-lg" onSubmit={submit}>
        <div className="flex items-center">
          <h2 className="flex-1 text-lg font-semibold">{t("newProjectTitle")}</h2>
          <button type="button" title={t("close")} onClick={onClose}>
            <X className="size-4" />
          </button>
        </div>
        <label className="block space-y-1 text-sm">
          <span className="text-muted">{t("projectName")}</span>
          <input autoFocus className="w-full rounded-md border border-line bg-bg px-3 py-2" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <fieldset className="space-y-1 text-sm">
          <legend className="text-muted">{t("source")}</legend>
          <div className="flex flex-wrap gap-2">
            {SOURCES.filter(([id]) => (id !== "folder" || isAdmin) && (id !== "apple" || appleApps)).map(([id, label]) => (
              <label key={id} className={`cursor-pointer rounded-full border px-3 py-1 ${source === id ? "border-accent bg-accent text-on-accent" : "border-line"}`}>
                <input type="radio" name="source" value={id} className="sr-only" checked={source === id} onChange={() => setSource(id)} />
                {t(label)}
              </label>
            ))}
          </div>
        </fieldset>
        {source === "git" && (
          <label className="block space-y-1 text-sm">
            <span className="text-muted">{t("gitUrl")}</span>
            <input className="w-full rounded-md border border-line bg-bg px-3 py-2 font-mono text-xs" placeholder="https://github.com/…" value={url} onChange={(e) => setUrl(e.target.value)} />
            <span className="block text-xs text-muted">{t("privateRepoHint")}</span>
          </label>
        )}
        {source === "zip" && (
          <label className="block space-y-1 text-sm">
            <span className="text-muted">{t("zipFile")}</span>
            <input type="file" accept=".zip,application/zip" className="block w-full text-sm" onChange={(e) => setZip(e.target.files?.[0] ?? null)} />
          </label>
        )}
        {source === "folder" && (
          <label className="block space-y-1 text-sm">
            <span className="text-muted">{t("folderPath")}</span>
            <input className="w-full rounded-md border border-line bg-bg px-3 py-2 font-mono text-xs" placeholder="/srv/projects/…" value={folder} onChange={(e) => setFolder(e.target.value)} />
          </label>
        )}
        {source === "apple" && (
          <div className="space-y-2 text-sm">
            <p className="text-xs text-muted">{t("appleAppHint")}</p>
            <label className="block space-y-1">
              <span className="text-muted">{t("bundleId")}</span>
              <input className="w-full rounded-md border border-line bg-bg px-3 py-2 font-mono text-xs" placeholder="com.example.app" value={bundleId} onChange={(e) => setBundleId(e.target.value)} />
            </label>
          </div>
        )}
        {error && (
          <p className="text-sm text-bad" role="alert">
            {error}
          </p>
        )}
        <div className="flex items-center justify-end gap-2">
          {busy && <span className="mr-auto text-xs text-muted">{t(busy)}</span>}
          <button type="button" className="rounded-md border border-line px-3 py-1.5 text-sm" onClick={onClose}>
            {t("cancel")}
          </button>
          <button type="submit" disabled={!ready || busy !== null} className="rounded-md bg-accent px-3 py-1.5 text-sm font-medium text-on-accent disabled:opacity-40">
            {t("create")}
          </button>
        </div>
      </form>
    </div>,
    document.body,
  );
}
