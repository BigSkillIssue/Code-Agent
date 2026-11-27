// Small building blocks shared by the settings and admin pages.

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { language } from "../lib/i18n";
import { useStore } from "../state/store";

export function Section({ title, children, hint }: { title: string; children: ReactNode; hint?: string }) {
  return (
    <section className="border-t border-line py-6 first-of-type:border-t-0 first-of-type:pt-2">
      <h2 className="text-lg font-semibold">{title}</h2>
      {hint && <p className="mt-1 max-w-[70ch] text-sm text-muted">{hint}</p>}
      <div className="mt-4 space-y-3 text-sm">{children}</div>
    </section>
  );
}

export function TextInput(props: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  type?: string;
  placeholder?: string;
  auto?: string;
}) {
  return (
    <label className="block">
      <span className="mb-1.5 block text-sm font-medium">{props.label}</span>
      <input
        className="h-9 w-full rounded-md border border-line-strong bg-card px-2.5 focus:border-accent focus:outline-none"
        type={props.type ?? "text"}
        value={props.value}
        placeholder={props.placeholder}
        autoComplete={props.auto ?? "off"}
        onChange={(event) => props.onChange(event.target.value)}
      />
    </label>
  );
}

export function Button(props: {
  children: ReactNode;
  onClick?: () => void;
  type?: "button" | "submit";
  kind?: "primary" | "plain" | "danger";
  disabled?: boolean;
  title?: string;
}) {
  const look = {
    primary: "bg-accent font-medium text-on-accent hover:opacity-90",
    plain: "border border-line bg-card hover:bg-panel",
    danger: "border border-bad text-bad hover:bg-bad hover:text-bg",
  }[props.kind ?? "plain"];
  return (
    <button
      type={props.type ?? "button"}
      title={props.title}
      disabled={props.disabled}
      onClick={props.onClick}
      className={`h-9 rounded-md px-3.5 text-sm transition-colors disabled:opacity-50 ${look}`}
    >
      {props.children}
    </button>
  );
}

/** Data from the server, a way to load it again, and errors shown in the app's error bar. */
export function useLoaded<T>(load: () => Promise<T>): [T | null, () => Promise<void>] {
  const [data, setData] = useState<T | null>(null);
  const setError = useStore((s) => s.setError);
  const reload = useCallback(async () => {
    try {
      setData(await load());
    } catch (err) {
      setError((err as Error).message);
    }
  }, [load, setError]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return [data, reload];
}

/** Run a change; its error goes to the error bar. True when it worked. */
export function useAction() {
  const setError = useStore((s) => s.setError);
  return useCallback(
    async (work: () => Promise<unknown>): Promise<boolean> => {
      try {
        await work();
        return true;
      } catch (err) {
        setError((err as Error).message);
        return false;
      }
    },
    [setError],
  );
}

export const date = (seconds: number) =>
  seconds ? new Date(seconds * 1000).toLocaleString(language === "de" ? "de-DE" : "en-US") : "–";

export const money = (usd: number) =>
  new Intl.NumberFormat(language === "de" ? "de-DE" : "en-US", { style: "currency", currency: "USD" }).format(usd);
