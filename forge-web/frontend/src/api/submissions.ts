// The App Store step of an Apple project: prepare a release from TestFlight for App Review,
// submit it (with a confirmation naming the version) and release it once Apple approved.

import { api } from "./client";

export type SubmissionStep = "screenshots" | "version" | "texts" | "terms" | "contact" | "placements";

export interface AppleSubmission {
  id: string;
  release_id: string;
  platform: "ios" | "macos";
  version: string;
  status: "preparing" | "ready" | "failed" | "submitted" | "released";
  step: SubmissionStep;
  steps: SubmissionStep[];
  error: string;
  hint: string;
  review_state: string;
  version_state: string;
  screenshots: number;
  created_at: number;
  updated_at: number;
}

export interface ReviewContact {
  first_name: string;
  last_name: string;
  phone: string;
  email: string;
  notes: string;
}

export const appleSubmissions = (projectId: string) => {
  const base = `/api/projects/${encodeURIComponent(projectId)}/apple/submissions`;
  const one = (submissionId: string) => `${base}/${encodeURIComponent(submissionId)}`;
  return {
    list: () => api.get<AppleSubmission[]>(base),
    prepare: (releaseId: string, contact: ReviewContact) => api.post<AppleSubmission>(base, { release_id: releaseId, contact }),
    submit: (submissionId: string, version: string) => api.post<AppleSubmission>(`${one(submissionId)}/submit`, { confirm: true, version }),
    release: (submissionId: string) => api.post<AppleSubmission>(`${one(submissionId)}/release`, {}),
  };
};
