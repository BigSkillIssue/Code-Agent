// A pill toggle for two to four choices (W43), e.g. paying monthly or yearly: a radio group the
// arrow keys move through, as the browser's radio buttons do.

import { type KeyboardEvent, type ReactNode, useRef } from "react";

export interface Segment<T extends string> {
  value: T;
  label: ReactNode;
}

export function Segmented<T extends string>(props: {
  value: T;
  options: Segment<T>[];
  onChange: (value: T) => void;
  ariaLabel: string;
}) {
  const { value, options, onChange } = props;
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const keys = (event: KeyboardEvent, index: number) => {
    const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[event.key];
    if (!step) return;
    event.preventDefault();
    const next = (index + step + options.length) % options.length;
    onChange(options[next].value);
    buttons.current[next]?.focus();
  };
  return (
    <div role="radiogroup" aria-label={props.ariaLabel} className="inline-flex rounded-full border border-line bg-panel p-0.5">
      {options.map((option, index) => {
        const on = option.value === value;
        return (
          <button
            key={option.value}
            ref={(element) => {
              buttons.current[index] = element;
            }}
            type="button"
            role="radio"
            aria-checked={on}
            tabIndex={on ? 0 : -1}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => keys(event, index)}
            className={`rounded-full px-3.5 py-1 text-sm transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40 ${on ? "bg-card font-medium text-fg shadow-sm" : "text-muted hover:text-fg"}`}
          >
            {option.label}
          </button>
        );
      })}
    </div>
  );
}
