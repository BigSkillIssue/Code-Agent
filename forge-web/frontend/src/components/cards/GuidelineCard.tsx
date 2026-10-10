// The Apple reviewer's verdict on the request, the plan, the finished app or its store texts,
// with every finding.
// The review comes from the project's sandbox: links are only made for Apple's own pages.

import { CheckCircle2, ShieldCheck, TriangleAlert, XCircle } from "lucide-react";
import type { GuidelineFinding, GuidelineReview, Verdict } from "../../api/apple";
import { t, type TextKey } from "../../lib/i18n";

const STAGE: Record<GuidelineReview["stage"], TextKey> = {
  prompt: "reviewOfPrompt",
  plan: "reviewOfPlan",
  product: "reviewOfProduct",
  listing: "reviewOfListing",
};

const VERDICT: Record<Verdict, { label: TextKey; tone: string; Icon: typeof CheckCircle2 }> = {
  ok: { label: "verdictOk", tone: "text-ok", Icon: CheckCircle2 },
  concern: { label: "verdictConcern", tone: "text-warn", Icon: TriangleAlert },
  violation: { label: "verdictViolation", tone: "text-bad", Icon: XCircle },
};

const AREA: Record<GuidelineFinding["area"], TextKey> = {
  safety: "areaSafety",
  performance: "areaPerformance",
  business: "areaBusiness",
  design: "areaDesign",
  legal: "areaLegal",
  hig: "areaHig",
};

const APPLE_PAGE = /^https:\/\/(developer|www)\.apple\.com\//;

export function GuidelineCard({ review }: { review: GuidelineReview }) {
  const verdict = VERDICT[review.verdict] ?? VERDICT.concern;
  const problems = review.findings.filter((f) => f.status !== "ok");
  const fine = review.findings.filter((f) => f.status === "ok");
  return (
    <div className="rounded-lg border border-line bg-card p-3 text-sm" data-testid="guideline-review">
      <div className="flex flex-wrap items-center gap-2 font-medium">
        <ShieldCheck className="size-4" />
        {t(STAGE[review.stage] ?? "reviewOfProduct")}
        <span className={`ml-auto flex items-center gap-1 text-xs ${verdict.tone}`}>
          <verdict.Icon className="size-4" />
          {t(verdict.label)}
        </span>
      </div>
      <p className="mt-1">{review.summary}</p>
      {review.error && <p className="mt-1 text-xs text-bad">{review.error}</p>}
      {problems.length > 0 && (
        <ul className="mt-2 space-y-2">
          {problems.map((finding, index) => (
            <Finding key={index} finding={finding} />
          ))}
        </ul>
      )}
      {fine.length > 0 && (
        <details className="mt-2">
          <summary className="cursor-pointer text-xs text-muted">{t("findingsOk", { n: String(fine.length) })}</summary>
          <ul className="mt-1 space-y-2">
            {fine.map((finding, index) => (
              <Finding key={index} finding={finding} />
            ))}
          </ul>
        </details>
      )}
      {review.sources.length > 0 && <Sources sources={review.sources} />}
    </div>
  );
}

function Finding({ finding }: { finding: GuidelineFinding }) {
  const look = VERDICT[finding.status] ?? VERDICT.concern;
  return (
    <li className="flex items-start gap-2">
      <look.Icon className={`mt-0.5 size-4 shrink-0 ${look.tone}`} />
      <div>
        <div className="font-medium">
          {t(AREA[finding.area] ?? "areaDesign")}
          {finding.guideline && <span className="font-normal text-muted"> · {finding.guideline}</span>}
        </div>
        <div>{finding.reason}</div>
        {finding.fix && (
          <div className="text-muted">
            {t("fixHint")}: {finding.fix}
          </div>
        )}
      </div>
    </li>
  );
}

function Sources({ sources }: { sources: string[] }) {
  return (
    <div className="mt-2 text-xs text-muted">
      {t("guidelinesRead")}:{" "}
      {sources.map((source, index) => (
        <span key={source} className="break-all">
          {index > 0 && ", "}
          {APPLE_PAGE.test(source) ? (
            <a href={source} target="_blank" rel="noreferrer noopener" className="underline">
              {source}
            </a>
          ) : (
            source
          )}
        </span>
      ))}
    </div>
  );
}
