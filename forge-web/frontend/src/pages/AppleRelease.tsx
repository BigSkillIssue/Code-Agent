// "Release": the approved commit on its way to TestFlight, step by step. A release starts only
// on the user's click, from the newest approval, with the user's own App Store Connect key.

import { Check, CircleDashed, CircleX, Rocket } from "lucide-react";
import { useCallback, useEffect, useMemo } from "react";
import { Link, useParams } from "react-router-dom";
import { type AppleRelease, appleReleases, appleReview, appStoreKey, type ReleaseStep } from "../api/apple";
import { t, type TextKey } from "../lib/i18n";
import { useStore } from "../state/store";
import { Button, date, Section, useAction, useLoaded } from "./parts";

const STEP_TEXT: Record<ReleaseStep, TextKey> = {
  archive: "stepArchive",
  identify: "stepIdentify",
  export: "stepExport",
  upload: "stepUpload",
  process: "stepProcess",
  testflight: "stepTestflight",
  done: "stepDone",
};
const STATUS_TEXT: Record<AppleRelease["status"], TextKey> = {
  running: "releaseRunning",
  done: "releaseDone",
  failed: "releaseFailed",
};
const POLL_MS = 3000;

export function AppleReleasePage() {
  const { projectId = "" } = useParams();
  const project = useStore((s) => s.projects.find((p) => p.id === projectId));
  const api = useMemo(() => appleReleases(projectId), [projectId]);
  const [review] = useLoaded(useCallback(() => appleReview(projectId), [projectId]));
  const [key] = useLoaded(useCallback(() => appStoreKey.get(), []));
  const [releases, reload] = useLoaded(api.list);
  const running = (releases ?? []).some((r) => r.status === "running");
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => void reload(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [running, reload]);
  const act = useAction();
  const approval = review?.approvals[0];
  const start = async (platforms: AppleRelease["platform"][]) => {
    if (await act(() => api.start(platforms))) await reload();
  };
  const retry = async (releaseId: string) => {
    if (await act(() => api.retry(releaseId))) await reload();
  };
  const ready = Boolean(approval?.commit && approval.clean && key);
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-4 p-4 md:p-8" data-testid="apple-release">
        <h1 className="flex items-center gap-2 text-xl font-semibold">
          <Rocket className="size-5" />
          {t("releaseTitle")}
          {project && <span className="font-normal text-muted">— {project.name}</span>}
        </h1>
        <div className="space-y-3 rounded-lg border border-line bg-card p-4 text-sm">
          <p className="text-muted">{t("releaseHint")}</p>
          {review && !approval && (
            <p>
              {t("releaseNeedsApproval")}{" "}
              <Link to={`/p/${projectId}/apple`} className="text-accent underline">
                {t("readyForApproval")}
              </Link>
            </p>
          )}
          {approval && <p>{t("releaseApproved", { commit: approval.commit.slice(0, 10) })}</p>}
          {review && key === null && (
            <p>
              {t("releaseNeedsKey")}{" "}
              <Link to="/settings" className="text-accent underline">
                {t("releaseKeyLink")}
              </Link>
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <Button kind="primary" disabled={!ready || running} onClick={() => void start(["ios"])}>
              {t("releaseIos")}
            </Button>
            <Button disabled={!ready || running} onClick={() => void start(["macos"])}>
              {t("releaseMac")}
            </Button>
            <Button disabled={!ready || running} onClick={() => void start(["ios", "macos"])}>
              {t("releaseBoth")}
            </Button>
          </div>
        </div>
        <Section title={t("releases")}>
          {releases?.length === 0 && <p className="text-muted">{t("noReleases")}</p>}
          <ul className="space-y-3">
            {(releases ?? []).map((release) => (
              <ReleaseCard key={release.id} release={release} onRetry={() => void retry(release.id)} />
            ))}
          </ul>
        </Section>
      </div>
    </div>
  );
}

function ReleaseCard({ release, onRetry }: { release: AppleRelease; onRetry: () => void }) {
  const at = release.steps.indexOf(release.step);
  return (
    <li className="rounded-lg border border-line bg-card p-3 text-sm" data-testid="release">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium">{t(release.platform === "macos" ? "platformMacos" : "platformIos")}</span>
        <span className={release.status === "failed" ? "text-bad" : "text-muted"}>{t(STATUS_TEXT[release.status])}</span>
        {release.version && (
          <span className="text-muted">{t("releaseBuild", { version: release.version, build: String(release.build_number) })}</span>
        )}
        <span className="ml-auto text-xs text-muted">{date(release.created_at)}</span>
      </div>
      <ol className="mt-2 space-y-1">
        {release.steps.map((step, index) => (
          <li key={step} className="flex items-center gap-2">
            {stepIcon(index, at, release.status)}
            <span className={index > at ? "text-muted" : ""}>{t(STEP_TEXT[step])}</span>
          </li>
        ))}
      </ol>
      {release.status === "failed" && (
        <div className="mt-2 space-y-2" data-testid="release-problem">
          <p className="whitespace-pre-wrap text-bad">{release.error}</p>
          {release.hint && <p className="text-muted">{release.hint}</p>}
          <Button onClick={onRetry}>{t("releaseRetry")}</Button>
        </div>
      )}
    </li>
  );
}

function stepIcon(index: number, at: number, status: AppleRelease["status"]) {
  if (index < at || status === "done") return <Check className="size-4 text-ok" aria-hidden />;
  if (index === at && status === "failed") return <CircleX className="size-4 text-bad" aria-hidden />;
  return <CircleDashed className={`size-4 ${index === at ? "animate-spin text-accent" : "text-muted"}`} aria-hidden />;
}
