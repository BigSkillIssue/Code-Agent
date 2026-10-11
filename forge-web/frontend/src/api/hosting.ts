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
  /** The app's deletion began (W29c); deleting_error says why it is not done yet. */
  deleting: boolean;
  deleting_error: string;
  /** The kept backups, newest first: each is an export of its environment to download. */
  backups: { id: string; environment: Environment; created_at: number; size: number }[];
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
    exportData: (environment: Environment) => api.post<{ started: boolean }>(`${base}/hosting/export`, { environment }),
    exportUrl: (backupId: string) => `${base}/hosting/backups/${id(backupId)}`,
    deleteApp: () => api.delete(`${base}/hosting`),
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

/** A notice of illegal content in a hosted app (DSA Art. 16), and what an admin decided. */
export interface AbuseNotice {
  id: string;
  project_id: string;
  app: string;
  project: string;
  location: string;
  category: "hate" | "child_abuse" | "terror" | "fraud" | "copyright" | "privacy" | "other";
  reason: string;
  name: string;
  email: string;
  status: "open" | "dismissed" | "actioned";
  decision: string;
  created_at: number;
  decided_at: number;
}

/** Something wrong in hosting that the admins were mailed about once; closed_at is 0 while it lasts. */
export interface Incident {
  id: string;
  key: string;
  subject: string;
  detail: string;
  opened_at: number;
  closed_at: number;
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
  notices: () => api.get<AbuseNotice[]>(`${admin}/notices`),
  decide: (noticeId: string, action: "dismiss" | "suspend", reason: string) =>
    api.post<AbuseNotice>(`${admin}/notices/${id(noticeId)}/decide`, { action, reason }),
  incidents: () => api.get<Incident[]>(`${admin}/incidents`),
  users: () => api.get<HostingUser[]>(`${admin}/users`),
  trust: (userId: string, trusted: boolean) => api.put<{ trusted: boolean }>(`${admin}/users/${id(userId)}`, { trusted }),
};
