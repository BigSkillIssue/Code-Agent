// The signed-in user's own settings and the administration (admins only).

import { api } from "./client";

export interface Me {
  id: string;
  email: string | null;
  name: string;
  role: "admin" | "member";
  default_model: string;
  has_password: boolean;
  totp_enabled: boolean;
  two_factor_required: boolean;
}

export interface Identity {
  provider: string;
  label: string;
  email: string;
  username: string;
}

export interface StoredKey {
  id: string;
  provider: string;
  name: string;
  hint: string;
  created_at: number;
  last_used_at: number;
}

export interface Provider {
  name: string;
  own_key: boolean;
  server_key: boolean;
}

export interface Model {
  id: string;
  provider: string;
  model: string;
  key: "own" | "server";
}

export interface GitCredential {
  id: string;
  host: string;
  username: string;
  hint: string;
  source: string;
}

export interface SessionInfo {
  id: string;
  current: boolean;
  ip: string;
  user_agent: string;
  last_seen_at: number;
}

export interface Usage {
  server_keys_usd: number;
  limit_usd: number | null;
  by_model: { provider: string; model: string; key_kind: string; cost_usd: number; calls: number }[];
}

export interface Account {
  id: string;
  email: string | null;
  name: string;
  role: "admin" | "member";
  status: "active" | "pending" | "unverified" | "disabled";
  created_at: number;
  has_password: boolean;
  totp_enabled: boolean;
  email_verified: boolean;
}

export interface Invite {
  id: string;
  email: string | null;
  role: string;
  expires_at: number;
}

export interface Grant {
  user_id: string;
  allowed: boolean;
  monthly_limit_usd: number | null;
}

export interface UsageRow {
  user_id: string;
  email: string | null;
  name: string;
  server_usd: number;
  own_usd: number;
  calls: number;
}

export interface AuditRow {
  at: number;
  user_id: string;
  action: string;
  target: string;
  detail: string;
  ip: string;
}

export interface ServerSettings {
  signup: "invite" | "approval" | "open";
  allowed_domains: string[];
  passwords: boolean;
  admin_two_factor: boolean;
  projects_per_user: number;
  project_disk_mb: number;
  sandbox_cpus: number;
  sandbox_memory: string;
  sandbox_pids: number;
  sandbox_idle_minutes: number;
  server_keys_for: "admins" | "granted" | "everyone";
  monthly_limit_usd: number;
  apple_enabled: boolean;
  apple_allowed: "admins" | "granted" | "everyone";
  apple_minutes_per_month: number;
  apple_reviewer_model: string;
}

const id = encodeURIComponent;

export const account = {
  me: () => api.get<Me>("/api/me"),
  update: (changes: Partial<Pick<Me, "name" | "default_model">>) => api.patch<Me>("/api/me", changes),
  changePassword: (current: string, next: string) => api.post("/api/me/password", { current, new: next }),
  totpSetup: () => api.post<{ secret: string; uri: string }>("/api/me/totp/setup"),
  totpEnable: (code: string) => api.post<{ recovery_codes: string[] }>("/api/me/totp/enable", { code }),
  totpDisable: (code: string) => api.post("/api/me/totp/disable", { code }),
  identities: () => api.get<Identity[]>("/api/auth/identities"),
  unlink: (provider: string) => api.delete(`/api/auth/identities/${id(provider)}`),
  keys: () => api.get<StoredKey[]>("/api/keys"),
  addKey: (provider: string, key: string) => api.post<StoredKey>("/api/keys", { provider, key }),
  removeKey: (keyId: string) => api.delete(`/api/keys/${id(keyId)}`),
  providers: () => api.get<Provider[]>("/api/providers"),
  models: () => api.get<Model[]>("/api/models"),
  gitCredentials: () => api.get<GitCredential[]>("/api/git/credentials"),
  addGitCredential: (host: string, token: string) => api.post<GitCredential>("/api/git/credentials", { host, token }),
  removeGitCredential: (credentialId: string) => api.delete(`/api/git/credentials/${id(credentialId)}`),
  sessions: () => api.get<SessionInfo[]>("/api/auth/sessions"),
  endSession: (sessionId: string) => api.delete(`/api/auth/sessions/${id(sessionId)}`),
  usage: () => api.get<Usage>("/api/usage"),
};

export const admin = {
  users: () => api.get<Account[]>("/api/admin/users"),
  updateUser: (userId: string, changes: { role?: string; status?: string }) =>
    api.patch<Account>(`/api/admin/users/${id(userId)}`, changes),
  approve: (userId: string) => api.post<Account>(`/api/admin/users/${id(userId)}/approve`),
  resetLink: (userId: string) => api.post<{ link: string }>(`/api/admin/users/${id(userId)}/reset-link`),
  resetTwoFactor: (userId: string) => api.post(`/api/admin/users/${id(userId)}/totp/reset`),
  invites: () => api.get<Invite[]>("/api/admin/invites"),
  invite: (email: string, role: string) => api.post<{ link: string }>("/api/admin/invites", { email, role }),
  withdraw: (inviteId: string) => api.delete(`/api/admin/invites/${id(inviteId)}`),
  keys: () => api.get<StoredKey[]>("/api/admin/keys"),
  addKey: (provider: string, key: string, name: string) => api.post<StoredKey>("/api/admin/keys", { provider, key, name }),
  removeKey: (keyId: string) => api.delete(`/api/admin/keys/${id(keyId)}`),
  grants: () => api.get<Grant[]>("/api/admin/grants"),
  setGrant: (userId: string, allowed: boolean, limit: number | null) =>
    api.put<Grant>(`/api/admin/grants/${id(userId)}`, { allowed, monthly_limit_usd: limit }),
  usage: () => api.get<{ total_usd: number; users: UsageRow[] }>("/api/admin/usage"),
  audit: () => api.get<AuditRow[]>("/api/admin/audit?limit=200"),
  settings: () => api.get<ServerSettings>("/api/admin/settings"),
  saveSettings: (changes: Partial<ServerSettings>) => api.patch<ServerSettings>("/api/admin/settings", changes),
};
