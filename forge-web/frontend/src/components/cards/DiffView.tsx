// A line diff of an edit (old text -> new text), or the content of a new file.

import { diffLines } from "diff";

export function DiffView({ before, after }: { before: string; after: string }) {
  const parts = diffLines(before, after);
  return (
    <pre className="text-xs font-mono bg-code rounded-md p-2 overflow-x-auto max-h-96">
      {parts.map((part, index) => {
        const sign = part.added ? "+" : part.removed ? "-" : " ";
        const tone = part.added ? "text-ok" : part.removed ? "text-bad" : "text-muted";
        const lines = part.value.replace(/\n$/, "").split("\n");
        return lines.map((line, lineIndex) => (
          <div key={`${index}-${lineIndex}`} className={tone}>
            {sign} {line}
          </div>
        ));
      })}
    </pre>
  );
}
