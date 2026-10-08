import { CheckCircle2, ChevronDown, ChevronRight, Loader2, XCircle } from "lucide-react";
import { useState } from "react";
import type { Entry } from "../../state/transcript";
import { t } from "../../lib/i18n";
import { isEdit, toolLabel, toolSummary } from "../../lib/tools";
import { DiffView } from "./DiffView";

type ToolEntry = Extract<Entry, { kind: "tool" }>;

export function ToolCard({ entry }: { entry: ToolEntry }) {
  const [open, setOpen] = useState(entry.status === "running" && entry.output.length > 0);
  const { call } = entry;
  const icon =
    entry.status === "running" ? (
      <Loader2 className="size-4 animate-spin text-muted" />
    ) : entry.status === "ok" ? (
      <CheckCircle2 className="size-4 text-ok" />
    ) : (
      <XCircle className="size-4 text-bad" />
    );
  const args = call.arguments as Record<string, string>;
  return (
    <div className="rounded-lg border border-line bg-card text-sm">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left"
        onClick={() => setOpen(!open)}
      >
        {icon}
        <span className="font-medium">{toolLabel(call)}</span>
        <span className="truncate font-mono text-xs text-muted">{toolSummary(call)}</span>
        {entry.agent !== "main" && <span className="ml-auto text-xs text-muted">{entry.agent}</span>}
        <span className={entry.agent !== "main" ? "" : "ml-auto"}>
          {open ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
        </span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-line px-3 py-2">
          {call.name === "edit_file" && <DiffView before={args.old ?? ""} after={args.new ?? ""} />}
          {call.name === "write_file" && <DiffView before="" after={args.content ?? ""} />}
          {!isEdit(call) && (
            <details>
              <summary className="cursor-pointer text-xs text-muted">{t("arguments")}</summary>
              <pre className="mt-1 max-h-64 overflow-auto rounded bg-code p-2 text-xs">
                {JSON.stringify(call.arguments, null, 2)}
              </pre>
            </details>
          )}
          {entry.output.length > 0 && entry.status === "running" && (
            <pre className="max-h-64 overflow-auto rounded bg-code p-2 text-xs">{entry.output.join("\n")}</pre>
          )}
          {entry.result && (
            <pre className="max-h-80 overflow-auto rounded bg-code p-2 text-xs whitespace-pre-wrap">
              {entry.result.text}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
