// The project's backups (W66): after every run Forge keeps the project's state on a storage server
// (or on the Forge server while there is none). Owners and editors see them and bring one back as
// a new branch; their own branch and files stay as they are.

import { HardDrive, History, RotateCcw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { ProjectApi, Snapshot, SnapshotList } from "../api/project";
import { language, t } from "../lib/i18n";

interface Props {
  api: ProjectApi;
  refreshKey: number;
  onError: (message: string) => void;
}

const when = (seconds: number) =>
  new Date(seconds * 1000).toLocaleString(language === "de" ? "de-DE" : "en-GB", { dateStyle: "medium", timeStyle: "short" });

function size(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function SnapshotsPanel({ api, refreshKey, onError }: Props) {
  const [list, setList] = useState<SnapshotList | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [restored, setRestored] = useState<string | null>(null);
  const load = useCallback(() => {
    api
      .snapshots()
      .then(setList)
      .catch((err: unknown) => onError(err instanceof Error ? err.message : String(err)));
  }, [api, onError]);
  useEffect(load, [load, refreshKey]);

  const restore = async (snapshot: Snapshot) => {
    if (!window.confirm(t("snapshotConfirm", { time: when(snapshot.created_at) }))) return;
    setBusy(snapshot.id);
    try {
      const made = await api.restoreSnapshot(snapshot.id);
      setRestored(made.branch);
    } catch (err) {
      onError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  };

  if (!list) return null;
  return (
    <div className="h-full space-y-3 overflow-y-auto p-4 text-sm" data-testid="snapshots">
      <p className="text-muted">{list.enabled ? t("snapshotsHint") : t("snapshotsOff")}</p>
      <p className={`flex items-center gap-2 rounded-md border border-line px-3 py-2 ${list.storage === "local" ? "bg-panel" : ""}`}>
        <HardDrive className="size-4 shrink-0 text-muted" />
        {list.storage === "local" ? t("snapshotsLocal") : t("snapshotsNode")}
      </p>
      {restored && <p className="text-ok">{t("snapshotRestored", { branch: restored })}</p>}
      {list.snapshots.length === 0 ? (
        <p className="text-muted">{t("snapshotsNone")}</p>
      ) : (
        <ul className="divide-y divide-line">
          {list.snapshots.map((s) => (
            <li key={s.id} className="flex items-center gap-3 py-2">
              <History className="size-4 shrink-0 text-muted" />
              <div className="min-w-0 flex-1">
                <div className="flex flex-wrap items-baseline gap-x-2">
                  <span>{when(s.created_at)}</span>
                  {s.commit && <span className="font-mono text-xs text-muted">{s.commit}</span>}
                  {s.state === "failed" && <span className="text-bad">{t("snapshotFailed")}</span>}
                </div>
                <div className="truncate text-xs text-muted" title={s.error || undefined}>
                  {[s.chat, s.state === "failed" ? s.error : size(s.size), s.state === "failed" ? "" : t(s.where === "node" ? "snapshotWhereNode" : "snapshotWhereLocal")]
                    .filter(Boolean)
                    .join(" · ")}
                </div>
              </div>
              {s.state !== "failed" && (
                <button
                  type="button"
                  disabled={busy !== null}
                  className="flex shrink-0 items-center gap-1 rounded-md border border-line px-2 py-1 text-xs hover:bg-panel disabled:opacity-50"
                  onClick={() => void restore(s)}
                >
                  <RotateCcw className="size-3.5" />
                  {t("snapshotRestore")}
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
