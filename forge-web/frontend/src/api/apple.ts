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

export type Verdict = "ok" | "concern" | "violation";

export interface GuidelineFinding {
  area: "safety" | "performance" | "business" | "design" | "legal" | "hig";
  status: Verdict;
  guideline: string; // e.g. "5.1.1" or "HIG Accessibility"
  reason: string;
  fix: string;
}

/** The Apple reviewer's verdict on the request ("prompt"), the plan or the finished app. */
export interface GuidelineReview {
  stage: "prompt" | "plan" | "product";
  verdict: Verdict;
  summary: string;
  findings: GuidelineFinding[];
  sources: string[];
  error: string;
  chat_id?: string;
}

export interface AppleBuild {
  id: string;
  kind: "build" | "screenshot";
  params: { platform?: string; action?: string; dark?: boolean };
  status: AppleJob["status"];
  outcome: string;
  seconds: number;
  created_at: number;
}

export interface AppApproval {
  id: string;
  chat_id: string;
  user: string;
  at: number;
  commit: string;
  clean: boolean;
  summary: string;
}

/** What the approval page shows besides the pictures. */
export interface AppleReviewData {
  reviews: GuidelineReview[];
  pending: { chat_id: string; request_id: string; text: string } | null;
  choices: { approve: string; send_back: string; not_yet: string };
  builds: AppleBuild[];
  approvals: AppApproval[];
}

export const appleReview = (projectId: string) =>
  api.get<AppleReviewData>(`/api/projects/${id(projectId)}/apple/review`);

/** Answer Forge's approval question with one of its choices. */
export const decideApproval = (chatId: string, requestId: string, choice: string) =>
  api.post<{ accepted: boolean }>(`/api/chats/${id(chatId)}/answer`, {
    request_id: requestId,
    answer: { answers: [{ question_index: 0, values: [choice] }] },
  });
