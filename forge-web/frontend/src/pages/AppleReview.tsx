// "Ready for approval": everything about an Apple app in one place — the pictures of every
// device, the guideline reviews of the request, the plan and the app, the builds on the Macs —
// and the user's answer to Forge's approval question. Only that answer approves the app.

import { ShieldCheck } from "lucide-react";
import { useCallback, useMemo } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { type AppleBuild, appleReview, type AppleReviewData, decideApproval } from "../api/apple";
import { projectApi } from "../api/project";
import { GuidelineCard } from "../components/cards/GuidelineCard";
import { t, type TextKey } from "../lib/i18n";
import { AppleScreens } from "../panels/AppleScreens";
import { useStore } from "../state/store";
import { Button, date, Section, useAction, useLoaded } from "./parts";

const JOB_STATUS: Record<AppleBuild["status"], TextKey> = {
  queued: "jobQueued",
  running: "jobRunning",
  done: "jobDone",
  failed: "jobFailed",
};

export function AppleReviewPage() {
  const { projectId = "" } = useParams();
  const project = useStore((s) => s.projects.find((p) => p.id === projectId));
  const setError = useStore((s) => s.setError);
  const [data, reload] = useLoaded(useCallback(() => appleReview(projectId), [projectId]));
  const files = useMemo(() => projectApi(projectId), [projectId]);
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-4 md:p-8" data-testid="apple-review">
        <h1 className="flex items-center gap-2 text-xl font-semibold">
          <ShieldCheck className="size-5" />
          {t("readyForApproval")}
          {project && <span className="font-normal text-muted">— {project.name}</span>}
        </h1>
        {data && <Decision data={data} onDecided={reload} />}
        <section className="rounded-lg border border-line bg-card">
          <AppleScreens api={files} refreshKey={0} onError={setError} />
        </section>
        <Section title={t("guidelineReviews")}>
          {data?.reviews.length === 0 && <p className="text-muted">{t("noReviews")}</p>}
          {(data?.reviews ?? []).map((review) => (
            <GuidelineCard key={review.stage} review={review} />
          ))}
        </Section>
        <Builds builds={data?.builds ?? null} />
        <Approvals data={data} />
      </div>
    </div>
  );
}

function Decision({ data, onDecided }: { data: AppleReviewData; onDecided: () => Promise<void> }) {
  const navigate = useNavigate();
  const act = useAction();
  const pending = data.pending;
  if (!pending) {
    const last = data.approvals[0];
    return <p className="rounded-lg border border-line bg-card p-4 text-sm">{last ? approvedText(last) : t("approvalNotAsked")}</p>;
  }
  const decide = async (choice: string, toChat: boolean) => {
    const done = await act(async () => {
      const { accepted } = await decideApproval(pending.chat_id, pending.request_id, choice);
      if (!accepted) throw new Error(t("approvalGone"));
    });
    if (done && toChat) navigate(`/c/${pending.chat_id}`);
    else await onDecided();
  };
  return (
    <div className="space-y-3 rounded-lg border border-accent bg-card p-4 text-sm" data-testid="approval-decision">
      <p className="font-medium">{t("approvalWaiting")}</p>
      <p className="whitespace-pre-wrap text-muted">{pending.text}</p>
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="primary" onClick={() => void decide(data.choices.approve, false)}>
          {t("approveApp")}
        </Button>
        <Button onClick={() => void decide(data.choices.send_back, true)}>{t("sendBack")}</Button>
        <Link to={`/c/${pending.chat_id}`} className="ml-auto text-accent underline">
          {t("openChat")}
        </Link>
      </div>
    </div>
  );
}

function approvedText(approval: AppleReviewData["approvals"][number]): string {
  const parts = [t("approvedBy", { who: approval.user, when: date(approval.at) })];
  if (approval.commit) parts.push(t("atCommit", { commit: approval.commit.slice(0, 10) }));
  if (!approval.clean) parts.push(t("uncommitted"));
  return parts.join(" · ");
}

function Builds({ builds }: { builds: AppleBuild[] | null }) {
  return (
    <Section title={t("macBuilds")}>
      {builds?.length === 0 && <p className="text-muted">{t("noBuilds")}</p>}
      <table className="w-full text-left text-xs">
        <tbody>
          {(builds ?? []).map((build) => (
            <tr key={build.id} className="border-t border-line align-top">
              <td className="py-1 whitespace-nowrap">{date(build.created_at)}</td>
              <td>
                {build.params.action ?? build.kind} · {build.params.platform ?? "–"}
              </td>
              <td className={build.status === "failed" ? "text-bad" : ""}>{t(JOB_STATUS[build.status])}</td>
              <td className="text-muted">{build.outcome}</td>
              <td className="text-right">{build.seconds ? `${Math.round(build.seconds)} s` : "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function Approvals({ data }: { data: AppleReviewData | null }) {
  if (!data?.approvals.length) return null;
  return (
    <Section title={t("approvals")}>
      <ul className="space-y-1">
        {data.approvals.map((approval) => (
          <li key={approval.id}>
            {approvedText(approval)}
            {approval.summary && <div className="text-xs text-muted">{approval.summary}</div>}
          </li>
        ))}
      </ul>
    </Section>
  );
}
