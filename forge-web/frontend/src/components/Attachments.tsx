// Files that go with a chat message (W45): chips in the composer while they upload, and in the
// transcript a picture as a thumbnail, any other file as a chip. The project's raw downloads are
// octet-stream with nosniff, so a thumbnail is fetched and shown from a blob of its own type.

import { FileText, Loader2, X } from "lucide-react";
import { useEffect, useState } from "react";
import type { Attachment } from "../api/types";
import { t } from "../lib/i18n";

export const MAX_FILES = 10;
export const MAX_FILE_MB = 20;
const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);

/** `412 B`, `4.1 KB`, `2.3 MB`. */
export function fileSize(size: number): string {
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

export function isImage(file: { media_type: string }): boolean {
  return IMAGE_TYPES.has(file.media_type);
}

interface ChipProps {
  name: string;
  size?: number;
  busy?: boolean;
  problem?: string;
  onRemove?: () => void;
}

export function FileChip({ name, size, busy, problem, onRemove }: ChipProps) {
  return (
    <span
      data-testid="file-chip"
      className={`inline-flex max-w-[16rem] items-center gap-1.5 rounded-lg border px-2 py-1 text-xs ${problem ? "border-bad text-bad" : "border-line bg-panel"}`}
      title={problem}
    >
      {busy ? <Loader2 className="size-3.5 shrink-0 animate-spin" aria-label={t("uploading")} /> : <FileText className="size-3.5 shrink-0" />}
      <span className="min-w-0 truncate">{name}</span>
      {size !== undefined && !problem && <span className="shrink-0 text-muted">{fileSize(size)}</span>}
      {onRemove && (
        <button type="button" aria-label={t("removeFile", { name })} className="shrink-0 text-muted hover:text-fg" onClick={onRemove}>
          <X className="size-3.5" />
        </button>
      )}
    </span>
  );
}

/** A picture from the project, shown from its own bytes. */
export function Thumbnail({ projectId, file }: { projectId: string; file: Attachment }) {
  const [url, setUrl] = useState<string | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let made = "";
    let current = true;
    const path = `/api/projects/${projectId}/files/raw?path=${encodeURIComponent(file.path)}`;
    fetch(path, { credentials: "same-origin" })
      .then((response) => (response.ok ? response.arrayBuffer() : Promise.reject(new Error(String(response.status)))))
      .then((bytes) => {
        if (!current) return;
        made = URL.createObjectURL(new Blob([bytes], { type: file.media_type }));
        setUrl(made);
      })
      .catch(() => current && setFailed(true));
    return () => {
      current = false;
      if (made) URL.revokeObjectURL(made);
    };
  }, [projectId, file.path, file.media_type]);
  if (failed) return <FileChip name={file.name} size={file.size} />;
  if (!url) return <span className="inline-block size-24 animate-pulse rounded-lg bg-panel" />;
  return <img src={url} alt={file.name} className="max-h-48 max-w-[16rem] rounded-lg border border-line object-contain" />;
}

/** The files of a sent message. */
export function MessageFiles({ projectId, files }: { projectId?: string; files: Attachment[] }) {
  if (files.length === 0) return null;
  return (
    <div className="mt-1 flex flex-wrap justify-end gap-2" data-testid="message-files">
      {files.map((file) =>
        projectId && isImage(file) ? (
          <Thumbnail key={file.path} projectId={projectId} file={file} />
        ) : (
          <FileChip key={file.path} name={file.name} size={file.size} />
        ),
      )}
    </div>
  );
}
