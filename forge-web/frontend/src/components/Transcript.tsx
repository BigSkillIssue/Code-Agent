import { useEffect, useRef } from "react";
import type { Entry } from "../state/transcript";
import { AgentCard } from "./cards/AgentCard";
import { ApprovalCard, type Answer } from "./cards/ApprovalCard";
import { Notice, PlanCard, StructuredCard, TurnCard } from "./cards/Cards";
import { GuidelineCard } from "./cards/GuidelineCard";
import { QuestionCard } from "./cards/QuestionCard";
import { ToolCard } from "./cards/ToolCard";
import { Markdown } from "./Markdown";

interface ViewProps {
  entry: Entry;
  onAnswer?: Answer;
  approvalPage?: string; // where an Apple app's approval page is
}

function EntryView({ entry, onAnswer, approvalPage }: ViewProps) {
  switch (entry.kind) {
    case "user":
      return (
        <div className="flex justify-end">
          <div className="max-w-[85%] rounded-2xl bg-panel px-4 py-2 whitespace-pre-wrap" data-testid="user-message">
            {entry.text}
          </div>
        </div>
      );
    case "assistant":
      return (
        <div data-testid="assistant-message">
          {entry.agent !== "main" && <div className="text-xs text-muted">{entry.agent}</div>}
          <Markdown text={entry.text} />
          {entry.streaming && <span className="inline-block h-4 w-2 animate-pulse bg-muted align-middle" />}
        </div>
      );
    case "structured":
      return <StructuredCard entry={entry} />;
    case "tool":
      return <ToolCard entry={entry} />;
    case "agent":
      return <AgentCard entry={entry} render={(inner) => <EntryView entry={inner} onAnswer={onAnswer} approvalPage={approvalPage} />} />;
    case "approval":
      return <ApprovalCard entry={entry} onAnswer={onAnswer} />;
    case "question":
      return <QuestionCard entry={entry} onAnswer={onAnswer} approvalPage={approvalPage} />;
    case "guideline":
      return <GuidelineCard review={entry.review} />;
    case "plan":
      return <PlanCard entry={entry} />;
    case "turn":
      return <TurnCard entry={entry} />;
    case "notice":
      return <Notice entry={entry} />;
  }
}

export function Transcript({ entries, onAnswer, approvalPage }: { entries: Entry[]; onAnswer?: Answer; approvalPage?: string }) {
  const end = useRef<HTMLDivElement>(null);
  const last = entries[entries.length - 1];
  const lastLength = last?.kind === "assistant" ? last.text.length : 0;
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [entries.length, lastLength]);
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-3 py-4 sm:px-4 sm:py-6">
      {entries.map((entry) => (
        <EntryView key={entry.key} entry={entry} onAnswer={onAnswer} approvalPage={approvalPage} />
      ))}
      <div ref={end} />
    </div>
  );
}
