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
  stage: "prompt" | "plan" | "product" | "listing";
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

/** The user's App Store Connect team key as the server shows it (never the key itself). */
export interface AppStoreKeyView {
  key_id: string;
  issuer_id: string;
  team_id: string;
  created_at: number;
  checked_at: number;
  check_ok: boolean;
  check_message: string;
}

export const appStoreKey = {
  get: () => api.get<AppStoreKeyView | null>("/api/me/appstore-key"),
  save: (body: { key_id: string; issuer_id: string; team_id: string; private_key: string }) =>
    api.put<AppStoreKeyView>("/api/me/appstore-key", body),
  check: () => api.post<AppStoreKeyView>("/api/me/appstore-key/check", {}),
  remove: () => api.delete("/api/me/appstore-key"),
};

export type ReleaseStep = "archive" | "identify" | "export" | "upload" | "process" | "testflight" | "done";

/** One approved commit on its way to TestFlight, for one platform. */
export interface AppleRelease {
  id: string;
  platform: "ios" | "macos";
  commit: string;
  step: ReleaseStep;
  status: "running" | "failed" | "done";
  error: string;
  hint: string;
  build_number: number;
  version: string;
  bundle_id: string;
  steps: ReleaseStep[];
  created_at: number;
  updated_at: number;
}

export const appleReleases = (projectId: string) => {
  const base = `/api/projects/${id(projectId)}/apple/releases`;
  return {
    list: () => api.get<AppleRelease[]>(base),
    start: (platforms: AppleRelease["platform"][]) => api.post<AppleRelease[]>(base, { platforms }),
    retry: (releaseId: string) => api.post<AppleRelease>(`${base}/${id(releaseId)}/retry`, {}),
  };
};
