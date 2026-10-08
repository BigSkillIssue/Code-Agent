// The panel next to a chat: the project's files and its changes.

import { X } from "lucide-react";
import { useMemo, useState } from "react";
import { projectApi, type ProjectApi } from "../api/project";
import { t } from "../lib/i18n";
import { Changes } from "./Changes";
import { FileEditor } from "./FileEditor";
import { FileTree } from "./FileTree";

type Tab = "files" | "changes";

interface Props {
  projectId: string;
  canEdit: boolean;
  refreshKey: number;
  onClose: () => void;
  onError: (message: string) => void;
  api?: ProjectApi; // tests pass their own
}

export function ProjectPanel({ projectId, canEdit, refreshKey, onClose, onError, api }: Props) {
  const client = useMemo(() => api ?? projectApi(projectId), [api, projectId]);
  const [tab, setTab] = useState<Tab>("files");
  const [file, setFile] = useState<string | null>(null);
  const tabs: [Tab, string][] = [
    ["files", t("files")],
    ["changes", t("changes")],
  ];
  return (
    <aside className="flex h-full min-w-0 flex-col bg-card" aria-label={t("showPanel")}>
      <div className="flex items-center gap-1 border-b border-line px-2" role="tablist">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            className={`px-2 py-2 text-sm ${tab === id ? "border-b-2 border-accent font-medium" : "text-muted"}`}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
        <button type="button" title={t("close")} className="ml-auto p-1 text-muted" onClick={onClose}>
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
        {tab === "changes" && <Changes api={client} canEdit={canEdit} refreshKey={refreshKey} onError={onError} />}
      </div>
    </aside>
  );
}
