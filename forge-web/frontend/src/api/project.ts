// A project's files and git repository (the server forwards these to the project's sandbox).

import type { DeviceScreen } from "./apple";
import { api } from "./client";

export interface FileEntry {
  name: string;
  type: "file" | "dir" | "symlink" | "other";
  size: number;
  mtime: number;
}

export interface FileContent {
  path: string;
  size: number;
  mtime: number;
  truncated: boolean;
  binary: boolean;
  text: string | null;
}

export interface GitFile {
  path: string;
  index: string; // staged change: M A D R C ? or " "
  worktree: string; // unstaged change
  from: string | null;
}

export interface GitStatus {
  repo: boolean;
  branch: string | null;
  upstream: string | null;
  ahead: number;
  behind: number;
  files: GitFile[];
}

export interface Commit {
  commit: string;
  author: string;
  email: string;
  time: number;
  subject: string;
}

export interface SyncResult {
  ok: boolean;
  merged?: boolean;
  reason?: string;
  output: string;
}

export interface TerminalInfo {
  id: string;
  cols: number;
  rows: number;
  started_at: number;
  running: boolean;
  exit_code: number | null;
  attached: number;
}

export interface Program {
  id: string;
  name: string;
  argv: string[];
  cwd: string;
  started_at: number;
  running: boolean;
  exit_code: number | null;
  total_lines: number;
}

export interface PreviewOverview {
  enabled: boolean; // false: this server has no preview domain
  suggestions: { label: string; command: string }[];
  programs: Program[];
  ports: { port: number; address: string }[];
}

export interface ProgramOutput {
  lines: string[];
  from: number;
  next: number;
  running: boolean;
}

const q = (params: Record<string, string>) => new URLSearchParams(params).toString();

function socketUrl(path: string): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}${path}`;
}

export function projectApi(projectId: string) {
  const base = `/api/projects/${encodeURIComponent(projectId)}`;
  return {
    list: (path: string) => api.get<{ path: string; entries: FileEntry[] }>(`${base}/files/list?${q({ path })}`),
    read: (path: string) => api.get<FileContent>(`${base}/files/content?${q({ path })}`),
    save: (path: string, text: string, expectedMtime?: number) =>
      api.put<{ path: string; mtime: number }>(`${base}/files/content`, { path, text, expected_mtime: expectedMtime ?? null }),
    mkdir: (path: string) => api.post(`${base}/files/mkdir`, { path }),
    rename: (src: string, dst: string) => api.post(`${base}/files/rename`, { src, dst }),
    remove: (path: string) => api.delete(`${base}/files?${q({ path, recursive: "true" })}`),
    upload: (path: string, file: Blob) => api.putBytes<{ path: string }>(`${base}/files/raw?${q({ path })}`, file),
    downloadUrl: (path: string) => `${base}/files/raw?${q({ path })}`,
    importZip: (file: Blob) => api.putBytes<{ files: number; bytes: number }>(`${base}/import/zip`, file),
    status: () => api.get<GitStatus>(`${base}/git/status`),
    diff: (path: string, staged: boolean) =>
      api.get<{ diff: string; truncated: boolean }>(`${base}/git/diff?${q({ path, staged: String(staged) })}`),
    stage: (paths: string[]) => api.post(`${base}/git/stage`, { paths }),
    unstage: (paths: string[]) => api.post(`${base}/git/unstage`, { paths }),
    discard: (paths: string[]) => api.post(`${base}/git/discard`, { paths }),
    commit: (message: string) => api.post<{ commit: string }>(`${base}/git/commit`, { message }),
    branches: () =>
      api.get<{ current: string | null; branches: { name: string; commit: string; upstream: string | null }[] }>(
        `${base}/git/branches`,
      ),
    switchBranch: (branch: string, create: boolean) => api.post(`${base}/git/switch`, { branch, create }),
    log: () => api.get<{ commits: Commit[] }>(`${base}/git/log?limit=20`),
    remote: () => api.get<{ url: string | null; problem: string | null }>(`${base}/git/remote`),
    setRemote: (url: string) => api.put(`${base}/git/remote`, { url }),
    push: () => api.post<SyncResult>(`${base}/git/push`, {}),
    pull: () => api.post<SyncResult>(`${base}/git/pull`, {}),
    terminals: () => api.get<TerminalInfo[]>(`${base}/terminals`),
    openTerminal: (cols: number, rows: number) => api.post<TerminalInfo>(`${base}/terminals`, { cols, rows }),
    closeTerminal: (id: string) => api.delete(`${base}/terminals/${encodeURIComponent(id)}`),
    terminalUrl: (id: string) => socketUrl(`${base}/terminals/${encodeURIComponent(id)}/ws`),
    preview: () => api.get<PreviewOverview>(`${base}/preview`),
    startProgram: (command: string) => api.post<Program>(`${base}/preview/programs`, { command }),
    stopProgram: (id: string) => api.delete(`${base}/preview/programs/${encodeURIComponent(id)}`),
    programOutput: (id: string, since: number) =>
      api.get<ProgramOutput>(`${base}/preview/programs/${encodeURIComponent(id)}/output?${q({ since: String(since) })}`),
    openPreview: (port: number) => api.post<{ url: string }>(`${base}/preview/${port}/open`, {}),
    screens: () => api.get<DeviceScreen[]>(`${base}/apple/screens`),
  };
}

export type ProjectApi = ReturnType<typeof projectApi>;
