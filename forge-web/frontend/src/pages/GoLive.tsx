// "Go live": an app project's way to its users. The release review and the approved commit,
// then staging (with the app server's own checks and the staging link), then "Go live" for the
// same commit; the app's secrets, which go in here and never come back out; the app's own
// domains; and for the owner, the export of the app's data and its deletion.

import { Check, CircleDashed, CircleX, Globe, PauseCircle } from "lucide-react";
import { type FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { type Deploy, type DeployStep, type Environment, hosting, type ReleaseReview } from "../api/hosting";
import { t, type TextKey } from "../lib/i18n";
import { useStore } from "../state/store";
import { AppData } from "./AppData";
import { Domains } from "./Domains";
import { OperatorForm } from "./OperatorForm";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

const STEP_TEXT: Record<DeployStep, TextKey> = {
  pack: "deployStepPack",
  check: "deployStepCheck",
  creator: "deployStepCreator",
  admin: "deployStepAdmin",
  backup: "deployStepBackup",
  migrate: "deployStepMigrate",
  release: "deployStepRelease",
  done: "deployStepDone",
};
const STATUS_TEXT: Record<Deploy["status"], TextKey> = {
  running: "deployRunning",
  waiting: "deployWaiting",
  live: "deployLive",
  failed: "deployFailed",
  replaced: "deployReplaced",
};
const STAGE_TEXT: Record<ReleaseReview["stage"], TextKey> = {
  prompt: "releaseReviewOfPrompt",
  plan: "releaseReviewOfPlan",
  product: "releaseReviewOfProduct",
};
const VERDICT_TEXT: Record<ReleaseReview["verdict"], TextKey> = {
  ok: "releaseVerdictFine",
  concern: "releaseVerdictConcern",
  violation: "releaseVerdictViolation",
};
const POLL_MS = 3000;

export function GoLivePage() {
  const { projectId = "" } = useParams();
  const project = useStore((s) => s.projects.find((p) => p.id === projectId));
  const api = useMemo(() => hosting(projectId), [projectId]);
  const [data, reload] = useLoaded(api.overview);
  const busy = (data?.deploys ?? []).some((d) => d.status === "running");
  useEffect(() => {
    if (!busy) return;
    const timer = window.setInterval(() => void reload(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [busy, reload]);
  const act = useAction();
  const approved = data?.approvals[0];
  const staged = data?.staging;
  const canEdit = project?.role !== "viewer";
  const open = Boolean(data?.enabled && approved && !data.suspended && canEdit);
  const start = async (environment: Environment) => {
    const commit = environment === "production" ? (staged?.commit ?? "") : (approved?.commit ?? "");
    if (await act(() => api.start(environment, commit))) await reload();
  };
  const retry = async (deployId: string) => {
    if (await act(() => api.retry(deployId))) await reload();
  };
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-4 p-4 md:p-8" data-testid="go-live">
        <h1 className="flex items-center gap-2 text-xl font-semibold">
          <Globe className="size-5" />
          {t("goLiveTitle")}
          {project && <span className="font-normal text-muted">— {project.name}</span>}
        </h1>
        <div className="space-y-3 rounded-lg border border-line bg-card p-4 text-sm">
          <p className="text-muted">{t("goLiveHint")}</p>
          {data && !data.enabled && <p className="text-bad">{t("hostingOff")}</p>}
          {data?.suspended && (
            <p className="flex items-center gap-2 text-bad" data-testid="suspended">
              <PauseCircle className="size-4" />
              {t("goLiveSuspended", { reason: data.suspended_reason })}
            </p>
          )}
          {data && !approved && <p>{t("goLiveNeedsApproval")}</p>}
          {approved && <p>{t("goLiveApproved", { commit: approved.commit.slice(0, 10) })}</p>}
          <LiveLinks staging={staged ?? null} production={data?.production ?? null} />
          <div className="flex flex-wrap items-center gap-2">
            <Button disabled={!open || busy} onClick={() => void start("staging")}>
              {t("toStaging")}
            </Button>
            <Button kind="primary" disabled={!open || busy || !staged || !data?.operator} onClick={() => void start("production")}>
              {t("goLive")}
            </Button>
            {data && !staged && <span className="text-muted">{t("goLiveNeedsStaging")}</span>}
          </div>
          {data && !data.operator && <p className="text-muted">{t("goLiveNeedsOperator")}</p>}
        </div>
        {data && data.reviews.length > 0 && <Reviews reviews={data.reviews} />}
        <Section title={t("deploysTitle")}>
          {data?.deploys.length === 0 && <p className="text-muted">{t("noDeploys")}</p>}
          <ul className="space-y-3">
            {(data?.deploys ?? []).map((deploy) => (
              <DeployCard key={deploy.id} deploy={deploy} onRetry={canEdit ? () => void retry(deploy.id) : undefined} />
            ))}
          </ul>
        </Section>
        {data && (
          <OperatorForm
            key={data.operator?.updated_at ?? 0}
            saved={data.operator}
            canEdit={canEdit}
            onSave={api.setOperator}
            onSaved={reload}
          />
        )}
        {canEdit && <Secrets projectId={projectId} names={data?.secrets ?? []} onSaved={reload} />}
        {data?.app && canEdit && !data.deleting && (
          <Domains api={api} environments={(["staging", "production"] as const).filter((env) => data[env])} />
        )}
        {data?.app && project?.role === "owner" && <AppData api={api} data={data} onChanged={reload} />}
      </div>
    </div>
  );
}

function LiveLinks({ staging, production }: { staging: Deploy | null; production: Deploy | null }) {
  const links = [
    { label: t("stagingAt"), deploy: staging },
    { label: t("liveAt"), deploy: production },
  ].filter((link) => link.deploy?.url);
  if (!links.length) return null;
  return (
    <ul className="space-y-1" data-testid="live-links">
      {links.map(({ label, deploy }) => (
        <li key={label}>
          {label}:{" "}
          <a href={deploy?.url} target="_blank" rel="noreferrer" className="text-accent underline">
            {deploy?.url}
          </a>{" "}
          <span className="text-muted">({deploy?.commit.slice(0, 10)})</span>
        </li>
      ))}
    </ul>
  );
}

function Reviews({ reviews }: { reviews: ReleaseReview[] }) {
  return (
    <Section title={t("releaseReviewTitle")}>
      <ul className="space-y-2" data-testid="release-reviews">
        {reviews.map((review) => (
          <li key={review.stage}>
            <span className="font-medium">{t(STAGE_TEXT[review.stage])}</span>{" "}
            <span className={review.verdict === "ok" ? "text-ok" : "text-bad"}>{t(VERDICT_TEXT[review.verdict])}</span>
            <p className="text-muted">{review.summary}</p>
            {review.findings
              .filter((finding) => finding.status !== "ok")
              .map((finding) => (
                <p key={`${finding.area}-${finding.rule}`} className="text-bad">
                  {finding.rule || finding.area}: {finding.reason}
                  {finding.fix && <span className="text-muted"> — {finding.fix}</span>}
                </p>
              ))}
          </li>
        ))}
      </ul>
    </Section>
  );
}

function DeployCard({ deploy, onRetry }: { deploy: Deploy; onRetry?: () => void }) {
  const at = deploy.steps.indexOf(deploy.step);
  return (
    <li className="rounded-lg border border-line bg-card p-3 text-sm" data-testid="deploy">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium">{t(deploy.environment === "production" ? "envProduction" : "envStaging")}</span>
        <span className={deploy.status === "failed" ? "text-bad" : "text-muted"}>{t(STATUS_TEXT[deploy.status])}</span>
        <span className="text-muted">
          #{deploy.release} · {deploy.commit.slice(0, 10)}
        </span>
        <span className="ml-auto text-xs text-muted">{date(deploy.created_at)}</span>
      </div>
      <ol className="mt-2 space-y-1">
        {deploy.steps.map((step, index) => (
          <li key={step} className="flex items-center gap-2">
            {stepIcon(index, at, deploy.status)}
            <span className={index > at ? "text-muted" : ""}>{t(STEP_TEXT[step])}</span>
          </li>
        ))}
      </ol>
      {deploy.checks.length > 0 && (
        <div className="mt-2" data-testid="host-checks">
          <p className="text-xs text-muted">{t("hostChecks")}</p>
          <ul>
            {deploy.checks.map((check) => (
              <li key={check.name} className={check.ok ? "text-ok" : "text-bad"} title={check.detail}>
                {check.ok ? "✓" : "✗"} {check.name}
              </li>
            ))}
          </ul>
        </div>
      )}
      {deploy.status === "failed" && (
        <div className="mt-2 space-y-2" data-testid="deploy-problem">
          <p className="whitespace-pre-wrap text-bad">{deploy.error}</p>
          {deploy.hint && <p className="text-muted">{deploy.hint}</p>}
          {deploy.log_tail && (
            <details>
              <summary className="cursor-pointer text-muted">{t("deployLog")}</summary>
              <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap text-xs">{deploy.log_tail}</pre>
            </details>
          )}
          {onRetry && <Button onClick={onRetry}>{t("deployRetry")}</Button>}
        </div>
      )}
    </li>
  );
}

function stepIcon(index: number, at: number, status: Deploy["status"]) {
  if (index < at || status === "live" || status === "replaced") return <Check className="size-4 text-ok" aria-hidden />;
  if (index === at && status === "failed") return <CircleX className="size-4 text-bad" aria-hidden />;
  const turning = index === at && status === "running";
  return <CircleDashed className={`size-4 ${turning ? "animate-spin text-accent" : "text-muted"}`} aria-hidden />;
}

function Secrets(props: {
  projectId: string;
  names: { environment: Environment; name: string }[];
  onSaved: () => Promise<void>;
}) {
  const api = useMemo(() => hosting(props.projectId), [props.projectId]);
  const [environment, setEnvironment] = useState<Environment>("staging");
  const [name, setName] = useState("");
  const [value, setValue] = useState("");
  const act = useAction();
  const save = useCallback(
    async (event: FormEvent) => {
      event.preventDefault();
      if (await act(() => api.setSecret(environment, name.trim(), value))) {
        setName("");
        setValue("");
        await props.onSaved();
      }
    },
    [act, api, environment, name, value, props],
  );
  return (
    <form onSubmit={save}>
      <Section title={t("appSecretsTitle")} hint={t("appSecretsHint")}>
        {props.names.length > 0 && (
          <ul className="text-muted" data-testid="secret-names">
            {props.names.map((secret) => (
              <li key={`${secret.environment}-${secret.name}`}>
                {secret.name} · {t(secret.environment === "production" ? "envProduction" : "envStaging")}
              </li>
            ))}
          </ul>
        )}
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="block">
            <span className="mb-1 block text-xs text-muted">{t("secretFor")}</span>
            <select
              className="w-full rounded-md border border-line bg-bg px-2 py-1.5"
              value={environment}
              onChange={(e) => setEnvironment(e.target.value as Environment)}
            >
              <option value="staging">{t("envStaging")}</option>
              <option value="production">{t("envProduction")}</option>
            </select>
          </label>
          <TextInput label={t("secretName")} value={name} placeholder="MAPS_KEY" onChange={setName} />
          <TextInput label={t("secretValue")} value={value} type="password" onChange={setValue} />
        </div>
        <Button type="submit" disabled={!name.trim() || !value}>
          {t("secretSave")}
        </Button>
      </Section>
    </form>
  );
}
