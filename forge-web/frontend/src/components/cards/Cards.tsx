// The smaller cards: the task as Forge understood it, the plan, a turn's report, notices.

import { CheckCircle2, Circle, CircleDot, ClipboardList, Info, TriangleAlert, XCircle } from "lucide-react";
import type { ReactNode } from "react";
import type { Entry } from "../../state/transcript";
import { t } from "../../lib/i18n";
import { Markdown } from "../Markdown";

export function StructuredCard({ entry }: { entry: Extract<Entry, { kind: "structured" }> }) {
  const goal = typeof entry.data.goal === "string" ? entry.data.goal : null;
  const criteria = Array.isArray(entry.data.acceptance_criteria) ? (entry.data.acceptance_criteria as string[]) : [];
  return (
    <details className="rounded-lg border border-line bg-card px-3 py-2 text-sm">
      <summary className="cursor-pointer">
        <span className="font-medium">{goal ? t("taskUnderstood") : "Data"}</span>
        {goal && <span className="text-muted">: {goal}</span>}
      </summary>
      {criteria.length > 0 && (
        <ul className="mt-2 list-disc pl-5 text-muted">
          {criteria.map((c) => (
            <li key={c}>{c}</li>
          ))}
        </ul>
      )}
      <pre className="mt-2 max-h-64 overflow-auto rounded bg-code p-2 text-xs">{JSON.stringify(entry.data, null, 2)}</pre>
    </details>
  );
}

const STEP_ICON: Record<string, ReactNode> = {
  done: <CheckCircle2 className="size-4 text-ok" />,
  doing: <CircleDot className="size-4 animate-pulse text-accent" />,
  failed: <XCircle className="size-4 text-bad" />,
  check_failed: <XCircle className="size-4 text-bad" />,
};

export function PlanCard({ entry }: { entry: Extract<Entry, { kind: "plan" }> }) {
  return (
    <div className="rounded-lg border border-line bg-card px-3 py-2 text-sm">
      <div className="flex items-center gap-2 font-medium">
        <ClipboardList className="size-4" />
        {t("plan")}
        {entry.goal && <span className="truncate font-normal text-muted">— {entry.goal}</span>}
      </div>
      <ol className="mt-2 space-y-1">
        {entry.steps.map((step) => (
          <li key={step.id} className="flex items-start gap-2">
            <span className="mt-0.5">{STEP_ICON[step.status] ?? <Circle className="size-4 text-muted" />}</span>
            <span className={step.status === "done" ? "text-muted line-through" : ""}>{step.title}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

export function TurnCard({ entry }: { entry: Extract<Entry, { kind: "turn" }> }) {
  const tone = entry.cancelled ? "text-warn" : entry.ok ? "text-ok" : "text-bad";
  const label = entry.cancelled ? t("stopped") : entry.ok ? t("done") : t("failed");
  return (
    <div className="rounded-lg border border-line bg-panel px-3 py-2 text-sm" data-testid="turn">
      <div className={`font-medium ${tone}`}>{label}</div>
      {entry.summary && !entry.cancelled && <Markdown text={entry.summary} />}
      {entry.error && <div className="text-bad">{entry.error}</div>}
      {entry.filesChanged.length > 0 && (
        <div className="mt-1 text-xs text-muted">
          {t("filesChanged")}: <span className="font-mono">{entry.filesChanged.join(", ")}</span>
        </div>
      )}
      <div className="mt-1 text-xs text-muted">
        {t("cost")}: ${entry.usage.cost_usd.toFixed(4)} · {entry.usage.input_tokens + entry.usage.output_tokens} tokens ·{" "}
        {entry.seconds.toFixed(1)} s
      </div>
    </div>
  );
}

export function Notice({ entry }: { entry: Extract<Entry, { kind: "notice" }> }) {
  const error = entry.tone === "error";
  return (
    <div className={`flex items-start gap-2 text-sm ${error ? "text-bad" : "text-muted"}`}>
      {error ? <TriangleAlert className="mt-0.5 size-4 shrink-0" /> : <Info className="mt-0.5 size-4 shrink-0" />}
      <pre className="font-sans whitespace-pre-wrap">{entry.text}</pre>
    </div>
  );
}
