import { MessageCircleQuestion } from "lucide-react";
import { useState } from "react";
import type { Entry } from "../../state/transcript";
import { t } from "../../lib/i18n";
import type { Answer } from "./ApprovalCard";

type QuestionEntry = Extract<Entry, { kind: "question" }>;

export function QuestionCard({ entry, onAnswer }: { entry: QuestionEntry; onAnswer?: Answer }) {
  const [values, setValues] = useState<string[][]>(entry.questions.map((q) => (q.default ? [q.default] : [])));
  const [busy, setBusy] = useState(false);
  const toggle = (index: number, option: string, multi: boolean) =>
    setValues((all) =>
      all.map((chosen, i) =>
        i !== index ? chosen : multi ? (chosen.includes(option) ? chosen.filter((o) => o !== option) : [...chosen, option]) : [option],
      ),
    );
  const submit = async (dismissed: boolean) => {
    if (!onAnswer) return;
    setBusy(true);
    try {
      await onAnswer(
        entry.id,
        dismissed ? { dismissed: true } : { answers: values.map((v, i) => ({ question_index: i, values: v })) },
      );
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rounded-lg border border-accent/50 bg-card p-3 text-sm" data-testid="question">
      <div className="flex items-center gap-2 font-medium">
        <MessageCircleQuestion className="size-4 text-accent" />
        {t("question")}
      </div>
      {entry.questions.map((question, index) => (
        <div key={index} className="mt-3">
          <div className="font-medium">{question.text}</div>
          {question.why && <div className="text-xs text-muted">{question.why}</div>}
          {entry.resolution ? (
            <div className="mt-1 text-xs text-ok">{(entry.answers?.[index] ?? []).join(", ") || t("answered")}</div>
          ) : question.kind === "choice" || question.kind === "multi" || question.kind === "confirm" ? (
            <div className="mt-2 flex flex-wrap gap-2">
              {(question.kind === "confirm" ? ["yes", "no"] : question.options).map((option) => (
                <button
                  key={option}
                  type="button"
                  className={`rounded-full border px-3 py-1 ${values[index]?.includes(option) ? "border-accent bg-accent text-on-accent" : "border-line"}`}
                  onClick={() => toggle(index, option, question.kind === "multi")}
                >
                  {option}
                </button>
              ))}
            </div>
          ) : (
            <input
              className="mt-2 w-full rounded-md border border-line bg-bg px-2 py-1"
              value={values[index]?.[0] ?? ""}
              onChange={(e) => setValues((all) => all.map((v, i) => (i === index ? [e.target.value] : v)))}
            />
          )}
        </div>
      ))}
      {!entry.resolution && (
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            disabled={busy}
            className="rounded-md bg-accent px-3 py-1 font-medium text-on-accent disabled:opacity-50"
            onClick={() => submit(false)}
          >
            {t("answer")}
          </button>
          <button type="button" disabled={busy} className="rounded-md border border-line px-3 py-1" onClick={() => submit(true)}>
            {t("dismiss")}
          </button>
        </div>
      )}
    </div>
  );
}
