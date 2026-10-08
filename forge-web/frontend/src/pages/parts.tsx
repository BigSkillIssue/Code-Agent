// Small building blocks shared by the settings and admin pages.

import { type ReactNode, useCallback, useEffect, useState } from "react";
import { language } from "../lib/i18n";
import { useStore } from "../state/store";

export function Section({ title, children, hint }: { title: string; children: ReactNode; hint?: string }) {
  return (
    <section className="rounded-lg border border-line bg-card p-4">
      <h2 className="font-semibold">{title}</h2>
      {hint && <p className="mt-0.5 text-sm text-muted">{hint}</p>}
      <div className="mt-3 space-y-3 text-sm">{children}</div>
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
      <span className="mb-1 block text-xs text-muted">{props.label}</span>
      <input
        className="w-full rounded-md border border-line bg-bg px-2 py-1.5"
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
    primary: "bg-accent text-on-accent",
    plain: "border border-line",
    danger: "border border-bad text-bad",
  }[props.kind ?? "plain"];
  return (
    <button
      type={props.type ?? "button"}
      title={props.title}
      disabled={props.disabled}
      onClick={props.onClick}
      className={`rounded-md px-3 py-1.5 text-sm disabled:opacity-50 ${look}`}
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
