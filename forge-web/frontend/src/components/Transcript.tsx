import { useEffect, useRef } from "react";
import type { Entry } from "../state/transcript";
import { ApprovalCard, type Answer } from "./cards/ApprovalCard";
import { Notice, PlanCard, StructuredCard, TurnCard } from "./cards/Cards";
import { QuestionCard } from "./cards/QuestionCard";
import { ToolCard } from "./cards/ToolCard";
import { Markdown } from "./Markdown";

function EntryView({ entry, onAnswer }: { entry: Entry; onAnswer?: Answer }) {
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
    case "approval":
      return <ApprovalCard entry={entry} onAnswer={onAnswer} />;
    case "question":
      return <QuestionCard entry={entry} onAnswer={onAnswer} />;
    case "plan":
      return <PlanCard entry={entry} />;
    case "turn":
      return <TurnCard entry={entry} />;
    case "notice":
      return <Notice entry={entry} />;
  }
}

export function Transcript({ entries, onAnswer }: { entries: Entry[]; onAnswer?: Answer }) {
  const end = useRef<HTMLDivElement>(null);
  const last = entries[entries.length - 1];
  const lastLength = last?.kind === "assistant" ? last.text.length : 0;
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [entries.length, lastLength]);
  return (
    <div className="mx-auto flex w-full max-w-3xl flex-col gap-4 px-4 py-6">
      {entries.map((entry) => (
        <EntryView key={entry.key} entry={entry} onAnswer={onAnswer} />
      ))}
      <div ref={end} />
    </div>
  );
}
