// The main agent's todo list, above the composer: what is done, what is happening now.

import { CheckCircle2, ChevronDown, ChevronUp, Circle, CircleDot } from "lucide-react";
import { useState } from "react";
import type { Todo } from "../api/types";
import { t } from "../lib/i18n";

export function TodoPanel({ todos }: { todos: Todo[] }) {
  const [open, setOpen] = useState(true);
  if (todos.length === 0) return null;
  const done = todos.filter((todo) => todo.status === "completed").length;
  const current = todos.find((todo) => todo.status === "in_progress");
  return (
    <div className="mx-auto w-full max-w-3xl px-3 sm:px-4" data-testid="todos">
      <div className="rounded-xl border border-line bg-panel px-3 py-2 text-sm">
        <button type="button" className="flex w-full items-center gap-2 text-left" onClick={() => setOpen(!open)}>
          <span className="font-medium">{t("todos")}</span>
          <span className="text-xs text-muted">
            {done}/{todos.length}
          </span>
          {!open && current && <span className="truncate text-xs text-muted">· {current.active_form || current.content}</span>}
          <span className="ml-auto">{open ? <ChevronDown className="size-4" /> : <ChevronUp className="size-4" />}</span>
        </button>
        {open && (
          <ul className="mt-1 space-y-0.5">
            {todos.map((todo, index) => (
              <li key={index} className="flex items-start gap-2">
                {todo.status === "completed" ? (
                  <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-ok" />
                ) : todo.status === "in_progress" ? (
                  <CircleDot className="mt-0.5 size-4 shrink-0 animate-pulse text-accent" />
                ) : (
                  <Circle className="mt-0.5 size-4 shrink-0 text-muted" />
                )}
                <span className={todo.status === "completed" ? "text-muted line-through" : ""}>
                  {todo.status === "in_progress" ? todo.active_form || todo.content : todo.content}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
