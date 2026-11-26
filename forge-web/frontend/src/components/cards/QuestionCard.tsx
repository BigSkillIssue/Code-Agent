import { MessageCircleQuestion, ShieldCheck } from "lucide-react";
import { useState } from "react";
import { Link } from "react-router-dom";
import type { Entry } from "../../state/transcript";
import { t } from "../../lib/i18n";
import type { Answer } from "./ApprovalCard";

type QuestionEntry = Extract<Entry, { kind: "question" }>;

interface Props {
  entry: QuestionEntry;
  onAnswer?: Answer;
  approvalPage?: string; // Forge's approval of an Apple app links to the page with everything to check
}

export function QuestionCard({ entry, onAnswer, approvalPage }: Props) {
  const [values, setValues] = useState<string[][]>(entry.questions.map((q) => (q.default ? [q.default] : [])));
  const [busy, setBusy] = useState(false);
  const toggle = (index: number, option: string, multi: boolean) =>
    setValues((all) =>
      all.map((chosen, i) =>
        i !== index ? chosen : multi ? (chosen.includes(option) ? chosen.filter((o) => o !== option) : [...chosen, option]) : [option],
      ),
    );
  // "Other": the person's own answer next to the options (replaces the choice, or adds to a multi).
  const setOther = (index: number, text: string, multi: boolean) =>
    setValues((all) =>
      all.map((chosen, i) => {
        if (i !== index) return chosen;
        const options = entry.questions[index].options;
        const kept = multi ? chosen.filter((v) => options.includes(v)) : [];
        return text.trim() ? [...kept, text] : kept;
      }),
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
    <div className="rounded-lg border border-accent/60 bg-card p-4 text-sm shadow-[inset_3px_0_0_var(--accent)]" data-testid="question">
      <div className="flex items-center gap-2 font-medium">
        <MessageCircleQuestion className="size-4 text-accent" />
        {t("question")}
      </div>
      {entry.appleApproval && approvalPage && !entry.resolution && (
        <Link to={approvalPage} className="mt-2 inline-flex items-center gap-1 rounded-md border border-accent px-3 py-1 text-accent">
          <ShieldCheck className="size-4" />
          {t("openApproval")}
        </Link>
      )}
      {entry.questions.map((question, index) => (
        <div key={index} className="mt-3">
          <div className="font-medium">{question.text}</div>
          {question.why && <div className="text-xs text-muted">{question.why}</div>}
          {entry.resolution ? (
            <div className="mt-1 text-xs text-ok">{(entry.answers?.[index] ?? []).join(", ") || t("answered")}</div>
          ) : question.kind === "choice" || question.kind === "multi" || question.kind === "confirm" ? (
            <Choices
              options={question.kind === "confirm" ? ["yes", "no"] : question.options}
              confirm={question.kind === "confirm"}
              multi={question.kind === "multi"}
              recommended={question.default ?? ""}
              chosen={values[index] ?? []}
              onToggle={(option) => toggle(index, option, question.kind === "multi")}
              onOther={(text) => setOther(index, text, question.kind === "multi")}
            />
          ) : (
            <input
              className="mt-2 h-9 w-full rounded-md border border-line bg-card px-2.5 focus:border-accent focus:outline-none"
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
            className="h-9 rounded-md bg-accent px-3.5 font-medium text-on-accent hover:opacity-90 disabled:opacity-50"
            onClick={() => submit(false)}
          >
            {t("answer")}
          </button>
          <button type="button" disabled={busy} className="h-9 rounded-md border border-line bg-card px-3.5 hover:bg-panel" onClick={() => submit(true)}>
            {t("dismiss")}
          </button>
        </div>
      )}
    </div>
  );
}

/** "Label — what it means" (or "Label: …") as its two parts. */
export function splitOption(option: string): [string, string] {
  const found = option.match(/^(.{1,40}?)\s+(?:—|–|-|:)\s+(.+)$/);
  return found ? [found[1], found[2]] : [option, ""];
}

function Choices(props: {
  options: string[];
  confirm: boolean;
  multi: boolean;
  recommended: string;
  chosen: string[];
  onToggle: (option: string) => void;
  onOther: (text: string) => void;
}) {
  const [other, setOther] = useState<string | null>(null);
  const described = props.options.some((o) => splitOption(o)[1]);
  const label = (option: string) => (props.confirm ? t(option === "yes" ? "answerYes" : "answerNo") : splitOption(option)[0]);
  return (
    <div className={`mt-2 ${described ? "space-y-1.5" : "flex flex-wrap gap-2"}`} role="group">
      {props.options.map((option) => {
        const on = props.chosen.includes(option);
        const [, description] = splitOption(option);
        return (
          <button
            key={option}
            type="button"
            aria-pressed={on}
            className={
              described
                ? `flex w-full items-start gap-2 rounded-md border px-3 py-2 text-left ${on ? "border-accent bg-accent-soft" : "border-line hover:bg-panel"}`
                : `rounded-full border px-3 py-1 ${on ? "border-accent bg-accent text-on-accent" : "border-line hover:bg-panel"}`
            }
            onClick={() => props.onToggle(option)}
          >
            <span className="min-w-0 flex-1">
              <span className="font-medium">{label(option)}</span>
              {option === props.recommended && <span className="ml-2 text-xs text-accent">{t("recommended")}</span>}
              {description && <span className="block text-xs text-muted">{description}</span>}
            </span>
          </button>
        );
      })}
      {!props.confirm &&
        (other === null ? (
          <button type="button" className={described ? "text-left text-xs text-muted underline" : "rounded-full border border-dashed border-line px-3 py-1 text-muted"} onClick={() => setOther("")}>
            {t("otherAnswer")}
          </button>
        ) : (
          <input
            autoFocus
            aria-label={t("otherAnswer")}
            placeholder={t("otherAnswer")}
            className="h-9 w-full rounded-md border border-line bg-card px-2.5 focus:border-accent focus:outline-none"
            value={other}
            onChange={(e) => {
              setOther(e.target.value);
              props.onOther(e.target.value);
            }}
          />
        ))}
    </div>
  );
}
