// The panel next to a chat: the project's files, its changes, (for editors) its terminals, the
// live preview and (for Apple apps) the pictures of its devices.

import { X } from "lucide-react";
import { useMemo, useState } from "react";
import { projectApi, type ProjectApi } from "../api/project";
import { t } from "../lib/i18n";
import { useStore } from "../state/store";
import { AppleScreens } from "./AppleScreens";
import { Changes } from "./Changes";
import { ProductPanel } from "./Product";
import { FileEditor } from "./FileEditor";
import { FileTree } from "./FileTree";
import { PreviewPanel } from "./Preview";
import { TerminalPanel } from "./Terminal";

type Tab = "product" | "files" | "changes" | "terminal" | "preview" | "devices";

interface Props {
  projectId: string;
  canEdit: boolean;
  apple?: boolean; // an Apple app: show its devices
  codeAccess?: boolean; // false: the owner's plan hides the code (W57)
  refreshKey: number;
  onClose: () => void;
  onError: (message: string) => void;
  api?: ProjectApi; // tests pass their own
}

export function ProjectPanel({ projectId, canEdit, apple = false, codeAccess = true, refreshKey, onClose, onError, api }: Props) {
  const sources = useStore((state) => state.user?.project_sources);
  const remotes = sources === undefined || sources.includes("git"); // W48: git turned off
  const client = useMemo(() => api ?? projectApi(projectId), [api, projectId]);
  const [tab, setTab] = useState<Tab>(codeAccess ? "files" : "product");
  const [file, setFile] = useState<string | null>(null);
  // Terminals and the preview stay alive once opened, also while another tab shows.
  const [opened, setOpened] = useState<Set<Tab>>(() => new Set());
  // W57: without the code, the product and its preview only.
  const code = (entries: [Tab, string][]) => (codeAccess ? entries : []);
  const tabs: [Tab, string][] = [
    ["product", t("productTab")],
    ...code([["files", t("files")], ["changes", t("changes")]]),
    ...code(canEdit ? [["terminal", t("terminal")]] : []),
    ["preview", t("preview")],
    ...(apple ? [["devices", t("devices")] as [Tab, string]] : []),
  ];
  const choose = (next: Tab) => {
    setTab(next);
    setOpened((all) => (all.has(next) ? all : new Set(all).add(next)));
  };
  return (
    <aside className="flex h-full min-w-0 flex-col bg-card" aria-label={t("showPanel")}>
      <div className="flex min-h-14 items-end gap-1 border-b border-line px-2" role="tablist">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            className={`-mb-px px-2.5 py-3 text-sm ${tab === id ? "border-b-2 border-accent font-medium" : "text-muted hover:text-fg"}`}
            onClick={() => choose(id)}
          >
            {label}
          </button>
        ))}
        <button type="button" title={t("close")} className="mb-2 ml-auto self-center rounded-md p-1.5 text-muted hover:bg-panel hover:text-fg" onClick={onClose}>
          <X className="size-4" />
        </button>
      </div>
      <div className="min-h-0 flex-1">
        {tab === "files" &&
          (file ? (
            <FileEditor api={client} path={file} canEdit={canEdit} onClose={() => setFile(null)} onError={onError} />
          ) : (
            <FileTree api={client} canEdit={canEdit} refreshKey={refreshKey} onOpen={setFile} onError={onError} />
          ))}
        {tab === "product" && <ProductPanel api={client} refreshKey={refreshKey} onError={onError} />}
        {tab === "changes" && <Changes api={client} canEdit={canEdit} remotes={remotes} refreshKey={refreshKey} onError={onError} />}
        {canEdit && codeAccess && opened.has("terminal") && (
          <div className={tab === "terminal" ? "h-full" : "hidden"}>
            <TerminalPanel api={client} active={tab === "terminal"} onError={onError} />
          </div>
        )}
        {tab === "devices" && (
          <AppleScreens api={client} refreshKey={refreshKey} onError={onError} approvalPage={`/p/${projectId}/apple`} />
        )}
        {opened.has("preview") && (
          <div className={tab === "preview" ? "h-full" : "hidden"}>
            <PreviewPanel api={client} canEdit={canEdit} active={tab === "preview"} onError={onError} />
          </div>
        )}
      </div>
    </aside>
  );
}
