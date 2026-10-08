import { ShieldQuestion } from "lucide-react";
import { useState } from "react";
import type { Entry } from "../../state/transcript";
import { t } from "../../lib/i18n";
import { toolLabel, toolSummary } from "../../lib/tools";
import { DiffView } from "./DiffView";

type ApprovalEntry = Extract<Entry, { kind: "approval" }>;
export type Answer = (requestId: string, answer: Record<string, unknown>) => Promise<unknown>;

export function ApprovalCard({ entry, onAnswer }: { entry: ApprovalEntry; onAnswer?: Answer }) {
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const args = entry.call.arguments as Record<string, string>;
  const answer = async (allow: boolean, remember = false) => {
    if (!onAnswer) return;
    setBusy(true);
    try {
      await onAnswer(entry.id, { allow, remember, feedback: allow ? "" : feedback });
    } finally {
      setBusy(false);
    }
  };
  const resolution = entry.resolution;
  return (
    <div className="rounded-lg border border-warn/60 bg-card p-3 text-sm" data-testid="approval">
      <div className="flex items-center gap-2 font-medium">
        <ShieldQuestion className="size-4 text-warn" />
        {entry.call.name === "submit_plan" ? t("plan") : `${t("wantsTo")}: ${toolLabel(entry.call)}`}
      </div>
      <div className="mt-1 font-mono text-xs text-muted break-all">{toolSummary(entry.call)}</div>
      {entry.reason && <div className="mt-1 text-xs whitespace-pre-wrap text-muted">{entry.reason}</div>}
      {entry.call.name === "edit_file" && (
        <div className="mt-2">
          <DiffView before={args.old ?? ""} after={args.new ?? ""} />
        </div>
      )}
      {entry.call.name === "write_file" && (
        <div className="mt-2">
          <DiffView before="" after={args.content ?? ""} />
        </div>
      )}
      {resolution ? (
        <div className={`mt-2 text-xs font-medium ${resolution === "allowed" ? "text-ok" : "text-bad"}`}>
          {resolution === "allowed" ? t("allowed") : resolution === "denied" ? t("denied") : t("withdrawn")}
        </div>
      ) : (
        <div className="mt-3 space-y-2">
          <input
            className="w-full rounded-md border border-line bg-bg px-2 py-1 text-sm"
            placeholder={t("feedback")}
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
          />
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={busy}
              className="rounded-md bg-accent px-3 py-1 font-medium text-on-accent disabled:opacity-50"
              onClick={() => answer(true)}
            >
              {t("allow")}
            </button>
            <button
              type="button"
              disabled={busy}
              className="rounded-md border border-line px-3 py-1 disabled:opacity-50"
              onClick={() => answer(true, true)}
            >
              {t("allowAlways")}
            </button>
            <button
              type="button"
              disabled={busy}
              className="rounded-md border border-line px-3 py-1 text-bad disabled:opacity-50"
              onClick={() => answer(false)}
            >
              {t("deny")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
