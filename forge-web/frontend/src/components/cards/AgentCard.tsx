// A sub-agent: its role and task, and — folded — everything it did.

import { Bot, CheckCircle2, ChevronDown, ChevronRight, Loader2, XCircle } from "lucide-react";
import { useState, type ReactNode } from "react";
import { t } from "../../lib/i18n";
import type { Entry } from "../../state/transcript";

type AgentEntry = Extract<Entry, { kind: "agent" }>;

export function AgentCard({ entry, render }: { entry: AgentEntry; render: (entry: Entry) => ReactNode }) {
  const [open, setOpen] = useState(false);
  const icon =
    entry.status === "running" ? (
      <Loader2 className="size-4 animate-spin text-muted" />
    ) : entry.status === "ok" ? (
      <CheckCircle2 className="size-4 text-ok" />
    ) : (
      <XCircle className="size-4 text-bad" />
    );
  return (
    <div className="rounded-lg border border-line bg-card text-sm" data-testid="agent">
      <button type="button" className="flex w-full items-center gap-2 px-3 py-2 text-left" onClick={() => setOpen(!open)}>
        {icon}
        <Bot className="size-4 shrink-0 text-muted" />
        <span className="font-medium">
          {t("subAgent")}
          {entry.role && <span className="font-normal text-muted"> · {entry.role}</span>}
        </span>
        <span className="min-w-0 flex-1 truncate text-xs text-muted">{entry.task}</span>
        {open ? <ChevronDown className="size-4 shrink-0" /> : <ChevronRight className="size-4 shrink-0" />}
      </button>
      {open && (
        <div className="space-y-3 border-t border-line px-3 py-3">
          {entry.task && <p className="text-xs whitespace-pre-wrap text-muted">{entry.task}</p>}
          <div className="space-y-3 border-l-2 border-line pl-3">{entry.entries.map((e) => <div key={e.key}>{render(e)}</div>)}</div>
          {entry.summary && (
            <pre className="max-h-48 overflow-auto rounded bg-code p-2 text-xs whitespace-pre-wrap">{entry.summary}</pre>
          )}
        </div>
      )}
    </div>
  );
}
