// Forge's own choice field instead of the browser's <select> (W43): a button that opens a list
// of options, each with an optional icon and description, the chosen one ticked. It follows the
// ARIA "select-only combobox": focus stays on the button, the arrow keys move through the list,
// Enter or Space picks, Escape closes, typing jumps to an option; a click outside closes it.

import { Check, ChevronDown } from "lucide-react";
import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from "react";

export interface Choice<T extends string = string> {
  value: T;
  label: string;
  description?: string;
  icon?: ReactNode;
}

export interface SelectProps<T extends string> {
  value: T;
  options: Choice<T>[];
  onChange: (value: T) => void;
  label?: string; // shown above the field
  ariaLabel?: string; // when there is no visible label
  disabled?: boolean;
  compact?: boolean; // the small kind for headers, tables and toolbars
  className?: string;
  footer?: ReactNode; // shown under the options, e.g. a field for a new entry
}

const ROOM = 280; // pixels the list wants below the field before it opens upwards

export function Select<T extends string>(props: SelectProps<T>) {
  const { value, options, onChange, compact } = props;
  const id = useId();
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [upwards, setUpwards] = useState(false);
  const [rightAligned, setRightAligned] = useState(false);
  const typed = useRef({ text: "", at: 0 });
  const box = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);
  const chosen = options.find((o) => o.value === value);

  useEffect(() => {
    if (!open) return;
    const away = (event: MouseEvent) => {
      if (box.current && !box.current.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", away);
    return () => document.removeEventListener("mousedown", away);
  }, [open]);

  useEffect(() => {
    if (open) document.getElementById(`${id}-${active}`)?.scrollIntoView?.({ block: "nearest" });
  }, [open, active, id]);

  const show = () => {
    if (props.disabled) return;
    const rect = button.current?.getBoundingClientRect();
    setUpwards(Boolean(rect && window.innerHeight - rect.bottom < ROOM && rect.top > window.innerHeight - rect.bottom));
    setRightAligned(Boolean(rect && rect.left > window.innerWidth / 2)); // keeps the list on the screen
    setActive(Math.max(0, options.findIndex((o) => o.value === value)));
    setOpen(true);
  };
  const pick = (index: number) => {
    const option = options[index];
    setOpen(false);
    button.current?.focus();
    if (option && option.value !== value) onChange(option.value);
  };
  const jump = (key: string) => {
    const now = Date.now();
    typed.current = { text: (now - typed.current.at < 700 ? typed.current.text : "") + key.toLowerCase(), at: now };
    const found = options.findIndex((o) => o.label.toLowerCase().startsWith(typed.current.text));
    if (found >= 0) setActive(found);
  };
  const keys = (event: KeyboardEvent) => {
    if (!open) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
        event.preventDefault();
        show();
      }
      return;
    }
    const last = options.length - 1;
    const moves: Record<string, () => void> = {
      ArrowDown: () => setActive((i) => Math.min(last, i + 1)),
      ArrowUp: () => setActive((i) => Math.max(0, i - 1)),
      Home: () => setActive(0),
      End: () => setActive(last),
      Enter: () => pick(active),
      " ": () => pick(active),
      Escape: () => setOpen(false),
      Tab: () => setOpen(false),
    };
    const move = moves[event.key];
    if (move) {
      if (event.key !== "Tab") event.preventDefault();
      move();
    } else if (event.key.length === 1) jump(event.key);
  };

  const size = compact ? "px-2 py-1 text-sm" : "px-2.5 py-1.5";
  return (
    <div ref={box} className={`relative ${props.label ? "block" : "inline-block"} ${props.className ?? ""}`}>
      {props.label && (
        <span id={`${id}-label`} className="mb-1 block text-xs text-muted">
          {props.label}
        </span>
      )}
      <button
        ref={button}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={`${id}-list`}
        aria-activedescendant={open ? `${id}-${active}` : undefined}
        aria-labelledby={props.label ? `${id}-label` : undefined}
        aria-label={props.label ? undefined : props.ariaLabel}
        disabled={props.disabled}
        onClick={() => (open ? setOpen(false) : show())}
        onKeyDown={keys}
        className={`flex w-full min-w-0 items-center gap-2 rounded-lg border border-line bg-card text-left shadow-sm transition-colors hover:border-muted/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 disabled:cursor-not-allowed disabled:opacity-60 ${size}`}
      >
        {chosen?.icon && <span className="shrink-0 text-muted">{chosen.icon}</span>}
        <span className="min-w-0 flex-1 truncate">{chosen?.label ?? value}</span>
        <ChevronDown className={`size-4 shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
      </button>
      {open && (
        <div
          className={`absolute z-50 w-max min-w-full max-w-[min(22rem,90vw)] overflow-hidden rounded-xl border border-line bg-card shadow-lg ${rightAligned ? "right-0" : "left-0"} ${upwards ? "bottom-full mb-1" : "top-full mt-1"}`}
        >
          <ul id={`${id}-list`} role="listbox" aria-labelledby={props.label ? `${id}-label` : undefined} aria-label={props.label ? undefined : props.ariaLabel} className="max-h-72 overflow-y-auto p-1">
            {options.map((option, index) => (
              <li
                key={option.value}
                id={`${id}-${index}`}
                role="option"
                aria-selected={option.value === value}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setActive(index)}
                onClick={() => pick(index)}
                className={`flex cursor-pointer items-start gap-2 rounded-lg px-2.5 py-2 text-sm ${index === active ? "bg-panel" : ""}`}
              >
                {option.icon && <span className="mt-0.5 shrink-0 text-muted">{option.icon}</span>}
                <span className="min-w-0 flex-1">
                  <span className="block truncate">{option.label}</span>
                  {option.description && <span className="block text-xs text-muted">{option.description}</span>}
                </span>
                <Check className={`mt-0.5 size-4 shrink-0 text-accent ${option.value === value ? "" : "invisible"}`} aria-hidden />
              </li>
            ))}
          </ul>
          {props.footer && <div className="border-t border-line p-1">{props.footer}</div>}
        </div>
      )}
    </div>
  );
}
