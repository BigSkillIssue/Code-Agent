// The project's changes: what is staged and what is not, diffs, commit, branches, remote, push
// and pull. Viewers see everything but cannot change anything.

import { GitBranch, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import type { Commit, GitFile, GitStatus, ProjectApi } from "../api/project";
import { PatchView } from "../components/cards/DiffView";
import { t } from "../lib/i18n";

interface Props {
  api: ProjectApi;
  canEdit: boolean;
  refreshKey: number;
  onError: (message: string) => void;
}

const isStaged = (f: GitFile) => f.index !== " " && f.index !== "?";
const isUnstaged = (f: GitFile) => f.worktree !== " ";

function FileRow(props: { file: GitFile; code: string; selected: boolean; onSelect: () => void; children?: React.ReactNode }) {
  return (
    <li className={`group flex items-center gap-2 rounded px-1 py-0.5 ${props.selected ? "bg-panel" : "hover:bg-panel"}`}>
      <span className="w-4 shrink-0 text-center font-mono text-xs text-muted">{props.code === "?" ? "U" : props.code}</span>
      <button type="button" className="min-w-0 flex-1 truncate text-left font-mono text-xs" onClick={props.onSelect}>
        {props.file.path}
      </button>
      {props.children}
    </li>
  );
}

export function Changes({ api, canEdit, refreshKey, onError }: Props) {
  const [status, setStatus] = useState<GitStatus | null>(null);
  const [branches, setBranches] = useState<string[]>([]);
  const [commits, setCommits] = useState<Commit[]>([]);
  const [remote, setRemote] = useState("");
  const [diff, setDiff] = useState<{ path: string; staged: boolean; text: string } | null>(null);
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const fail = useCallback((err: unknown) => onError(err instanceof Error ? err.message : String(err)), [onError]);

  const refresh = useCallback(async () => {
    try {
      const found = await api.status();
      setStatus(found);
      if (!found.repo) return;
      const [b, log, r] = await Promise.all([api.branches(), api.log(), api.remote()]);
      setBranches(b.branches.map((x) => x.name));
      setCommits(log.commits);
      setRemote(r.url ?? "");
    } catch (err) {
      fail(err);
    }
  }, [api, fail]);
  useEffect(() => {
    void refresh();
  }, [refresh, refreshKey]);

  const act = async (work: () => Promise<unknown>, after?: string) => {
    setBusy(true);
    setNote("");
    try {
      await work();
      if (after) setNote(after);
      await refresh();
    } catch (err) {
      fail(err);
    } finally {
      setBusy(false);
    }
  };
  const show = async (file: GitFile, staged: boolean) => {
    try {
      const found = await api.diff(file.path, staged);
      setDiff({ path: file.path, staged, text: found.diff || "" });
    } catch (err) {
      fail(err);
    }
  };

  if (status && !status.repo) return <p className="p-3 text-sm text-muted">{t("noRepo")}</p>;
  const files = status?.files ?? [];
  const staged = files.filter(isStaged);
  const unstaged = files.filter(isUnstaged);
  const selected = (f: GitFile, s: boolean) => diff?.path === f.path && diff.staged === s;

  return (
    <div className="flex h-full flex-col overflow-y-auto text-sm" data-testid="changes">
      <div className="flex items-center gap-2 border-b border-line px-2 py-1.5">
        <GitBranch className="size-4 text-muted" />
        <select
          aria-label={t("branch")}
          className="min-w-0 flex-1 rounded border border-line bg-card px-1 py-0.5 text-sm"
          value={status?.branch ?? ""}
          disabled={!canEdit || busy}
          onChange={(e) => {
            const value = e.target.value;
            if (value === "\u0000new") {
              const name = window.prompt(t("branchPrompt"))?.trim();
              if (name) void act(() => api.switchBranch(name, true));
            } else void act(() => api.switchBranch(value, false));
          }}
        >
          {branches.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
          {status?.branch && !branches.includes(status.branch) && <option value={status.branch}>{status.branch}</option>}
          {canEdit && <option value={"\u0000new"}>{t("newBranch")}</option>}
        </select>
        {status && (status.ahead > 0 || status.behind > 0) && (
          <span className="text-xs text-muted">
            {status.ahead} {t("ahead")} · {status.behind} {t("behind")}
          </span>
        )}
        <button type="button" title={t("refresh")} onClick={() => void refresh()}>
          <RefreshCw className="size-4 text-muted" />
        </button>
      </div>

      <div className="space-y-2 border-b border-line px-2 py-2">
        <form
          className="flex gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            void act(() => api.setRemote(remote.trim()));
          }}
        >
          <input
            aria-label={t("remoteUrl")}
            placeholder={t("remoteUrl")}
            className="min-w-0 flex-1 rounded border border-line bg-bg px-2 py-0.5 font-mono text-xs"
            value={remote}
            readOnly={!canEdit}
            onChange={(e) => setRemote(e.target.value)}
          />
          {canEdit && (
            <button type="submit" disabled={busy} className="rounded border border-line px-2 text-xs">
              {t("setRemote")}
            </button>
          )}
        </form>
        {canEdit && (
          <div className="flex gap-2">
            <button
              type="button"
              disabled={busy || !remote}
              className="rounded border border-line px-3 py-0.5 disabled:opacity-40"
              onClick={() => void act(async () => setNote((await api.pull()).reason ?? t("pulled")))}
            >
              {t("pull")}
            </button>
            <button
              type="button"
              disabled={busy || !remote}
              className="rounded border border-line px-3 py-0.5 disabled:opacity-40"
              onClick={() => void act(() => api.push(), t("pushed"))}
            >
              {t("push")}
            </button>
          </div>
        )}
        {note && (
          <p className="text-xs text-muted" role="status">
            {note}
          </p>
        )}
      </div>

      <section className="px-2 py-2">
        <h3 className="mb-1 text-xs font-medium tracking-wide text-muted uppercase">{t("staged")}</h3>
        <ul>
          {staged.map((file) => (
            <FileRow key={`s-${file.path}`} file={file} code={file.index} selected={selected(file, true)} onSelect={() => void show(file, true)}>
              {canEdit && (
                <button type="button" className="text-xs text-muted hover:text-fg" disabled={busy} onClick={() => void act(() => api.unstage([file.path]))}>
                  {t("unstage")}
                </button>
              )}
            </FileRow>
          ))}
        </ul>
        <div className="mt-3 mb-1 flex items-center">
          <h3 className="flex-1 text-xs font-medium tracking-wide text-muted uppercase">{t("unstagedChanges")}</h3>
          {canEdit && unstaged.length > 0 && (
            <button type="button" className="text-xs text-accent" disabled={busy} onClick={() => void act(() => api.stage(["."]))}>
              {t("stageAll")}
            </button>
          )}
        </div>
        {files.length === 0 && status && <p className="text-xs text-muted">{t("noChanges")}</p>}
        <ul>
          {unstaged.map((file) => (
            <FileRow key={`u-${file.path}`} file={file} code={file.worktree} selected={selected(file, false)} onSelect={() => void show(file, false)}>
              {canEdit && (
                <>
                  <button type="button" className="text-xs text-muted hover:text-fg" disabled={busy} onClick={() => void act(() => api.stage([file.path]))}>
                    {t("stage")}
                  </button>
                  <button
                    type="button"
                    className="text-xs text-muted hover:text-bad"
                    disabled={busy}
                    onClick={() => {
                      if (window.confirm(t("confirmDiscard", { name: file.path }))) void act(() => api.discard([file.path]));
                    }}
                  >
                    {t("discard")}
                  </button>
                </>
              )}
            </FileRow>
          ))}
        </ul>
      </section>

      {diff && (
        <section className="px-2 pb-2">
          <div className="mb-1 font-mono text-xs text-muted">{diff.path}</div>
          <PatchView patch={diff.text || t("noChanges")} />
        </section>
      )}

      {canEdit && (
        <form
          className="space-y-1 border-t border-line px-2 py-2"
          onSubmit={(e) => {
            e.preventDefault();
            void act(async () => {
              await api.commit(message.trim());
              setMessage("");
            });
          }}
        >
          <textarea
            aria-label={t("commitMessage")}
            placeholder={t("commitMessage")}
            className="w-full resize-y rounded border border-line bg-bg px-2 py-1 text-sm"
            rows={2}
            value={message}
            onChange={(e) => setMessage(e.target.value)}
          />
          <button
            type="submit"
            disabled={busy || !message.trim() || staged.length === 0}
            className="rounded bg-accent px-3 py-1 text-sm font-medium text-on-accent disabled:opacity-40"
          >
            {t("commit")}
          </button>
        </form>
      )}

      <details className="border-t border-line px-2 py-2">
        <summary className="cursor-pointer text-xs font-medium tracking-wide text-muted uppercase">{t("history")}</summary>
        <ul className="mt-1 space-y-1">
          {commits.map((c) => (
            <li key={c.commit} className="text-xs">
              <span className="font-mono text-muted">{c.commit.slice(0, 7)}</span> {c.subject}{" "}
              <span className="text-muted">— {c.author}</span>
            </li>
          ))}
        </ul>
      </details>
    </div>
  );
}
