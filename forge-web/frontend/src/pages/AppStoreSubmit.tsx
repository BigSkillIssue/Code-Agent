// The App Store step of the page "Release": prepare a release from TestFlight for App Review,
// submit it only after a confirmation naming the version, follow App Review, and release it.
// Each step is the user's own click.

import { Check, CircleDashed, CircleX, Store } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { AppleRelease } from "../api/apple";
import { type AppleSubmission, appleSubmissions, type ReviewContact, type SubmissionStep } from "../api/submissions";
import { t, type TextKey } from "../lib/i18n";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

const STEP_TEXT: Record<SubmissionStep, TextKey> = {
  screenshots: "stepScreenshots",
  version: "stepVersionStore",
  texts: "stepTexts",
  terms: "stepTerms",
  contact: "stepContact",
  placements: "stepPlacements",
};
const STATUS_TEXT: Record<AppleSubmission["status"], TextKey> = {
  preparing: "submissionPreparing",
  ready: "submissionReady",
  failed: "submissionFailed",
  submitted: "submissionSubmitted",
  released: "submissionReleased",
};
const POLL_MS = 3000;
const EMPTY: ReviewContact = { first_name: "", last_name: "", phone: "", email: "", notes: "" };

export function AppStoreSubmit(props: { projectId: string; appName: string; releases: AppleRelease[] }) {
  const api = useMemo(() => appleSubmissions(props.projectId), [props.projectId]);
  const [submissions, reload] = useLoaded(api.list);
  const [contact, setContact] = useState<ReviewContact>(EMPTY);
  const act = useAction();
  const busy = (submissions ?? []).some((s) => s.status === "preparing" || s.status === "submitted");
  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => void reload(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [busy, reload]);
  const inTestflight = latestDone(props.releases);
  const complete = Boolean(contact.first_name && contact.last_name && contact.phone && contact.email);
  const prepare = async (release: AppleRelease) => {
    if (await act(() => api.prepare(release.id, contact))) await reload();
  };
  const submit = async (submission: AppleSubmission) => {
    if (!window.confirm(t("submitConfirm", { name: props.appName, version: submission.version }))) return;
    if (await act(() => api.submit(submission.id, submission.version))) await reload();
  };
  const release = async (submission: AppleSubmission) => {
    if (!window.confirm(t("releaseConfirm", { version: submission.version }))) return;
    if (await act(() => api.release(submission.id))) await reload();
  };
  const change = useCallback((field: keyof ReviewContact) => (value: string) => setContact((c) => ({ ...c, [field]: value })), []);
  return (
    <Section title={t("appStoreStep")} hint={t("storeStepHint")}>
      <p className="text-xs text-muted">{t("appStoreOnce")}</p>
      {inTestflight.length === 0 && <p className="text-muted">{t("noTestflight")}</p>}
      {inTestflight.length > 0 && (
        <div className="space-y-2" data-testid="review-contact">
          <p className="text-xs font-medium">{t("reviewContact")}</p>
          <div className="grid gap-2 md:grid-cols-2">
            <TextInput label={t("contactFirst")} value={contact.first_name} onChange={change("first_name")} auto="given-name" />
            <TextInput label={t("contactLast")} value={contact.last_name} onChange={change("last_name")} auto="family-name" />
            <TextInput label={t("contactPhone")} value={contact.phone} onChange={change("phone")} type="tel" auto="tel" />
            <TextInput label={t("contactEmail")} value={contact.email} onChange={change("email")} type="email" auto="email" />
          </div>
          <TextInput label={t("contactNotes")} value={contact.notes} onChange={change("notes")} />
          <div className="flex flex-wrap gap-2">
            {inTestflight.map((r) => (
              <Button key={r.id} kind="primary" disabled={!complete || busy} onClick={() => void prepare(r)}>
                {t("prepareForStore")} — {t(r.platform === "macos" ? "platformMacos" : "platformIos")} {r.version}
              </Button>
            ))}
          </div>
        </div>
      )}
      <ul className="space-y-3">
        {(submissions ?? []).map((s) => (
          <SubmissionCard key={s.id} submission={s} onSubmit={() => void submit(s)} onRelease={() => void release(s)} />
        ))}
      </ul>
    </Section>
  );
}

function latestDone(releases: AppleRelease[]): AppleRelease[] {
  const seen = new Set<string>();
  return releases.filter((r) => r.status === "done" && !seen.has(r.platform) && seen.add(r.platform));
}

function SubmissionCard(props: { submission: AppleSubmission; onSubmit: () => void; onRelease: () => void }) {
  const s = props.submission;
  const at = s.status === "preparing" || s.status === "failed" ? s.steps.indexOf(s.step) : s.steps.length;
  const appleState = s.version_state || s.review_state;
  return (
    <li className="rounded-lg border border-line bg-bg p-3" data-testid="submission">
      <div className="flex flex-wrap items-baseline gap-2">
        <Store className="size-4 self-center" aria-hidden />
        <span className="font-medium">
          {t(s.platform === "macos" ? "platformMacos" : "platformIos")} {s.version}
        </span>
        <span className={s.status === "failed" ? "text-bad" : "text-muted"}>{t(STATUS_TEXT[s.status])}</span>
        {appleState && <span className="text-muted">{t("appleSays", { state: appleState.replaceAll("_", " ").toLowerCase() })}</span>}
        {s.screenshots > 0 && <span className="text-muted">{t("shotsUploaded", { n: String(s.screenshots) })}</span>}
        <span className="ml-auto text-xs text-muted">{date(s.created_at)}</span>
      </div>
      <ol className="mt-2 space-y-1">
        {s.steps.map((step, index) => (
          <li key={step} className="flex items-center gap-2 text-xs">
            {index < at ? (
              <Check className="size-4 text-ok" aria-hidden />
            ) : index === at && s.status === "failed" ? (
              <CircleX className="size-4 text-bad" aria-hidden />
            ) : (
              <CircleDashed className={`size-4 ${index === at ? "animate-spin text-accent" : "text-muted"}`} aria-hidden />
            )}
            <span className={index > at ? "text-muted" : ""}>{t(STEP_TEXT[step])}</span>
          </li>
        ))}
      </ol>
      {s.status === "failed" && (
        <div className="mt-2" data-testid="submission-problem">
          <p className="whitespace-pre-wrap text-bad">{s.error}</p>
          {s.hint && <p className="text-muted">{s.hint}</p>}
        </div>
      )}
      <div className="mt-2 flex gap-2">
        {s.status === "ready" && (
          <Button kind="primary" onClick={props.onSubmit}>
            {t("submitToApple")}
          </Button>
        )}
        {s.status === "submitted" && s.version_state === "PENDING_DEVELOPER_RELEASE" && (
          <Button kind="primary" onClick={props.onRelease}>
            {t("releaseOnStore")}
          </Button>
        )}
      </div>
    </li>
  );
}
