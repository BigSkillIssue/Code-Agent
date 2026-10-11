// Hosting (W26–W28): a project's deploys to staging and production, its secrets and operator
// data, what the go-live page shows; and the admin's hosting panel (hosts, the approval queue, apps, trust).

import { api } from "./client";

export type Environment = "staging" | "production";
export type DeployStep = "pack" | "check" | "creator" | "admin" | "backup" | "migrate" | "release" | "done";
export type DeployStatus = "running" | "waiting" | "live" | "failed" | "replaced";

export interface HostCheck {
  name: string; // e.g. "api: tests"
  ok: boolean;
  detail: string;
}

export interface Deploy {
  id: string;
  environment: Environment;
  commit: string;
  release: number;
  app: string;
  step: DeployStep;
  status: DeployStatus;
  error: string;
  hint: string;
  steps: DeployStep[];
  checks: HostCheck[];
  checks_from: string; // the staging deploy whose checks a production deploy took
  url: string;
  log_tail: string;
  creator_approved_at: number;
  admin: "" | "approved" | "trusted" | "admin";
  admin_approved_at: number;
  migrated: boolean;
  created_at: number;
  updated_at: number;
}

export interface ReleaseFinding {
  area: string;
  status: "ok" | "concern" | "violation";
  rule: string;
  reason: string;
  fix: string;
}

/** The release reviewer's verdict on the request, the plan or the finished product. */
export interface ReleaseReview {
  stage: "prompt" | "plan" | "product";
  verdict: "ok" | "concern" | "violation";
  summary: string;
  findings: ReleaseFinding[];
  error?: string;
  chat_id?: string;
}

export interface GoLiveApproval {
  commit: string;
  at: number;
  summary: string;
  chat_id: string;
}

/** Who runs the app (its creator), for its Impressum and privacy page. */
export interface Operator {
  name: string;
  representative: string;
  address: string;
  email: string;
  phone: string;
  trade_register: string;
  vat_id: string;
  privacy_email: string;
  privacy_extra: string;
}

export interface HostingOverview {
  enabled: boolean;
  app: string;
  suspended: boolean;
  suspended_reason: string;
  reviews: ReleaseReview[];
  approvals: GoLiveApproval[];
  deploys: Deploy[];
  staging: Deploy | null;
  production: Deploy | null;
  secrets: { environment: Environment; name: string }[];
  operator: (Operator & { updated_at: number }) | null;
}

const id = encodeURIComponent;

export const hosting = (projectId: string) => {
  const base = `/api/projects/${id(projectId)}`;
  return {
    overview: () => api.get<HostingOverview>(`${base}/hosting`),
    start: (environment: Environment, commit = "") => api.post<Deploy>(`${base}/deploys`, { environment, commit }),
    retry: (deployId: string) => api.post<Deploy>(`${base}/deploys/${id(deployId)}/retry`, {}),
    setSecret: (environment: Environment, name: string, value: string) =>
      api.put<{ ok: boolean }>(`${base}/hosting/secrets`, { environment, name, value }),
    setOperator: (operator: Operator) => api.put<Operator>(`${base}/hosting/operator`, operator),
  };
};

export interface Host {
  id: string;
  name: string;
  enabled: boolean;
  created_at: number;
  last_seen: number;
  online: boolean;
  version: string;
  apps: number;
}

export interface QueueItem extends Deploy {
  project_id: string;
  project: string;
  creator: string;
  review_summary: string;
}

export interface HostedApp {
  project_id: string;
  app: string;
  project: string;
  host: string;
  suspended: boolean;
  suspended_reason: string;
  suspended_at: number;
  live: Partial<Record<Environment, { release: number; commit: string; url: string }>>;
}

export interface HostingUser {
  id: string;
  email: string | null;
  name: string;
  role: string;
  trusted: boolean;
}

const admin = "/api/admin/hosting";

export const hostingAdmin = {
  hosts: () => api.get<Host[]>(`${admin}/hosts`),
  addHost: (name: string, publicKey: string) =>
    api.post<Host & { token: string }>(`${admin}/hosts`, { name, public_key: publicKey }),
  setHost: (hostId: string, enabled: boolean) => api.patch<Host>(`${admin}/hosts/${id(hostId)}`, { enabled }),
  queue: () => api.get<QueueItem[]>(`${admin}/queue`),
  approve: (deployId: string) => api.post<Deploy>(`${admin}/deploys/${id(deployId)}/approve`, {}),
  refuse: (deployId: string, reason: string) => api.post<Deploy>(`${admin}/deploys/${id(deployId)}/refuse`, { reason }),
  apps: () => api.get<HostedApp[]>(`${admin}/apps`),
  suspend: (projectId: string, suspended: boolean, reason = "") =>
    api.post<{ suspended: boolean; environments: Environment[] }>(`${admin}/apps/${id(projectId)}/suspend`, { suspended, reason }),
  logs: (projectId: string, environment: Environment) =>
    api.post<{ ok: boolean; log_tail: string; error: string }>(`${admin}/apps/${id(projectId)}/logs`, { environment }),
  users: () => api.get<HostingUser[]>(`${admin}/users`),
  trust: (userId: string, trusted: boolean) => api.put<{ trusted: boolean }>(`${admin}/users/${id(userId)}`, { trusted }),
};
