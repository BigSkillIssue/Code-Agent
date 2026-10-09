// Apple apps: the Macs that build them (admins), and the screenshots of a project's devices
// (fetched through projectApi).

import { api } from "./client";

export interface Mac {
  id: string;
  name: string;
  enabled: boolean;
  created_at: number;
  last_seen: number;
  online: boolean;
  version: string;
}

export interface AppleJob {
  id: string;
  kind: "build" | "screenshot";
  params: string; // JSON
  status: "queued" | "running" | "done" | "failed";
  user: string;
  project: string;
  worker_id: string;
  created_at: number;
  seconds: number;
  outcome: string;
}

export interface AppleUser {
  id: string;
  email: string;
  name: string;
  role: string;
  allowed: boolean;
  minutes_this_month: number;
}

export interface DeviceScreen {
  platform: "ios" | "ipados" | "macos" | "watchos";
  dark: boolean;
  url: string; // data:image/png;base64,...
}

const id = encodeURIComponent;

export const appleAdmin = {
  macs: () => api.get<Mac[]>("/api/admin/apple/macs"),
  addMac: (name: string) => api.post<Mac & { token: string }>("/api/admin/apple/macs", { name }),
  setMac: (macId: string, enabled: boolean) => api.patch<Mac>(`/api/admin/apple/macs/${id(macId)}`, { enabled }),
  removeMac: (macId: string) => api.delete(`/api/admin/apple/macs/${id(macId)}`),
  jobs: () => api.get<AppleJob[]>("/api/admin/apple/jobs?limit=100"),
  users: () => api.get<AppleUser[]>("/api/admin/apple/users"),
  grant: (userId: string, allowed: boolean) => api.put(`/api/admin/apple/users/${id(userId)}`, { allowed }),
};
