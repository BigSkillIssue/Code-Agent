// Billing administration (W31–W32): the Stripe keys (never shown again, only their mode and last
// characters), the catalog customers buy from (plans with their limits, an app's one-time price,
// hosting, add-ons), coupons, and the sync that brings Stripe in line with the catalog.

import { type FormEvent, useCallback, useState } from "react";
import {
  billingAdmin,
  type CatalogItem,
  type Interval,
  type ItemKind,
  type Limits,
  type SyncReport,
} from "../api/billing";
import { t, type TextKey } from "../lib/i18n";
import { Button, Section, TextInput, useAction, useLoaded } from "./parts";

const KINDS: Record<ItemKind, { text: TextKey; intervals: Interval[] }> = {
  plan: { text: "kindPlan", intervals: ["month", "year"] },
  app_once: { text: "kindAppOnce", intervals: ["once"] },
  hosting: { text: "kindHosting", intervals: ["month"] },
  addon: { text: "kindAddon", intervals: ["month"] },
};
const INTERVAL_TEXT: Record<Interval, TextKey> = { month: "amountMonth", year: "amountYear", once: "amountOnce" };
const LIMITS: [keyof Omit<Limits, "store_publishing">, TextKey][] = [
  ["projects", "limitProjects"],
  ["ai_budget_month_eur", "limitAiMonth"],
  ["ai_budget_per_product_eur", "limitAiProduct"],
  ["mac_minutes", "limitMacMinutes"],
  ["hosted_apps", "limitHostedApps"],
  ["storage_gb", "limitStorage"],
  ["custom_domains", "limitDomains"],
];

const money = (cents: number, currency: string) => `${(cents / 100).toFixed(2)} ${currency.toUpperCase()}`;
const cents = (text: string) => Math.round(Number.parseFloat(text.replace(",", ".")) * 100);
const number = (text: string) => (text.trim() === "" ? null : Number(text.replace(",", ".")));

export function BillingTab() {
  return (
    <div className="space-y-4" data-testid="admin-billing">
      <StripeSection />
      <CatalogSection />
      <CouponsSection />
    </div>
  );
}

function StripeSection() {
  const [setup, reload] = useLoaded(useCallback(() => billingAdmin.stripe(), []));
  const [secret, setSecret] = useState("");
  const [hook, setHook] = useState("");
  const act = useAction();
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => billingAdmin.setKeys(secret.trim(), hook.trim()))) {
      setSecret("");
      setHook("");
      await reload();
    }
  };
  return (
    <Section title={t("stripeTitle")} hint={t("stripeHint")}>
      {setup && !setup.enabled && <p className="text-bad">{t("stripeOff")}</p>}
      <p data-testid="stripe-keys">
        {setup?.configured ? t("stripeKeysSet", { mode: setup.mode, hint: setup.hint }) : t("stripeKeysMissing")}
      </p>
      {setup && <p className="break-all text-muted">{t("stripeWebhook", { url: setup.webhook_url })}</p>}
      <form onSubmit={save} className="grid gap-2 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <TextInput label={t("stripeSecretKey")} type="password" value={secret} onChange={setSecret} />
        <TextInput label={t("stripeWebhookSecret")} type="password" value={hook} onChange={setHook} />
        <Button type="submit" kind="primary" disabled={!secret.trim() || !hook.trim()}>
          {t("stripeSave")}
        </Button>
      </form>
    </Section>
  );
}

function CatalogSection() {
  const [catalog, reload] = useLoaded(useCallback(() => billingAdmin.catalog(), []));
  const [editing, setEditing] = useState<CatalogItem | null>(null);
  const [report, setReport] = useState<SyncReport | null>(null);
  const act = useAction();
  const sync = async () => {
    if (await act(async () => setReport(await billingAdmin.sync()))) await reload();
  };
  const remove = async (itemId: string) => {
    if (await act(() => billingAdmin.removeItem(itemId))) await reload();
  };
  const items = catalog?.items ?? [];
  return (
    <Section title={t("catalogTitle")} hint={t("catalogHint")}>
      {catalog && items.length === 0 && <p className="text-muted">{t("catalogEmpty")}</p>}
      <ul className="space-y-1" data-testid="catalog">
        {items.map((item) => (
          <li key={item.id} className="flex flex-wrap items-center gap-2" data-testid="catalog-item">
            <button type="button" className="font-medium underline" onClick={() => setEditing(item)}>
              {item.name}
            </button>
            <span className="text-muted">{t(KINDS[item.kind].text)}</span>
            {Object.entries(item.amounts).map(([interval, amount]) => (
              <span key={interval}>
                {t(INTERVAL_TEXT[interval as Interval])}: {money(amount ?? 0, item.currency)}
              </span>
            ))}
            <span className={item.archived_at ? "text-muted" : item.synced ? "text-ok" : "text-bad"}>
              {t(item.archived_at ? "itemArchived" : item.synced ? "itemSynced" : "itemNotSynced")}
            </span>
            {!item.archived_at && (
              <span className="ml-auto">
                <Button kind="danger" onClick={() => void remove(item.id)}>
                  {t("itemRemove")}
                </Button>
              </span>
            )}
          </li>
        ))}
      </ul>
      <ItemForm key={editing?.id ?? "new"} item={editing} onSaved={reload} />
      <div className="flex flex-wrap items-center gap-2">
        <Button kind="primary" onClick={() => void sync()}>
          {t("catalogSync")}
        </Button>
        {report && (
          <span data-testid="sync-report">
            {t("catalogSynced", {
              created: String(report.created.length),
              updated: String(report.updated.length),
              archived: String(report.archived.length),
              unchanged: String(report.unchanged),
            })}
          </span>
        )}
      </div>
    </Section>
  );
}

function ItemForm({ item, onSaved }: { item: CatalogItem | null; onSaved: () => Promise<void> }) {
  const [itemId, setItemId] = useState(item?.id ?? "");
  const [kind, setKind] = useState<ItemKind>(item?.kind ?? "plan");
  const [name, setName] = useState(item?.name ?? "");
  const [amounts, setAmounts] = useState<Partial<Record<Interval, string>>>(() =>
    Object.fromEntries(Object.entries(item?.amounts ?? {}).map(([k, v]) => [k, String((v ?? 0) / 100)])),
  );
  const [trial, setTrial] = useState(String(item?.trial_days ?? 0));
  const [limits, setLimits] = useState<Record<string, string>>(() =>
    Object.fromEntries(LIMITS.map(([key]) => [key, item?.limits[key] == null ? "" : String(item.limits[key])])),
  );
  const [store, setStore] = useState(item?.limits.store_publishing ?? false);
  const act = useAction();
  const intervals = KINDS[kind].intervals;
  const save = async (event: FormEvent) => {
    event.preventDefault();
    const chosen = Object.fromEntries(
      intervals.filter((i) => amounts[i]?.trim()).map((i) => [i, cents(amounts[i] ?? "")]),
    );
    const planLimits =
      kind === "plan"
        ? { ...Object.fromEntries(LIMITS.map(([key]) => [key, number(limits[key] ?? "")])), store_publishing: store }
        : {};
    const body = {
      kind,
      name: name.trim(),
      description: item?.description ?? "",
      currency: item?.currency ?? "eur",
      amounts: chosen,
      trial_days: kind === "plan" ? Number(trial) || 0 : 0,
      limits: planLimits,
    };
    if (await act(() => billingAdmin.putItem(itemId.trim(), body))) await onSaved();
  };
  return (
    <form onSubmit={save} className="space-y-2 rounded-md border border-line p-3" data-testid="item-form">
      <div className="grid gap-2 sm:grid-cols-3">
        <TextInput label={t("itemId")} value={itemId} onChange={setItemId} />
        <TextInput label={t("itemName")} value={name} onChange={setName} />
        <label className="block">
          <span className="mb-1 block text-xs text-muted">{t("itemKind")}</span>
          <select className="w-full rounded-md border border-line bg-bg px-2 py-1.5" value={kind}
            disabled={Boolean(item)} onChange={(e) => setKind(e.target.value as ItemKind)}>
            {(Object.keys(KINDS) as ItemKind[]).map((k) => (
              <option key={k} value={k}>{t(KINDS[k].text)}</option>
            ))}
          </select>
        </label>
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        {intervals.map((interval) => (
          <TextInput key={interval} label={t(INTERVAL_TEXT[interval])} value={amounts[interval] ?? ""} placeholder="29.00"
            onChange={(value) => setAmounts({ ...amounts, [interval]: value })} />
        ))}
        {kind === "plan" && <TextInput label={t("trialDays")} value={trial} onChange={setTrial} />}
      </div>
      {kind === "plan" && (
        <div className="grid gap-2 sm:grid-cols-4" data-testid="plan-limits">
          {LIMITS.map(([key, text]) => (
            <TextInput key={key} label={t(text)} value={limits[key] ?? ""} placeholder="∞"
              onChange={(value) => setLimits({ ...limits, [key]: value })} />
          ))}
          <label className="flex items-center gap-2">
            <input type="checkbox" checked={store} onChange={(e) => setStore(e.target.checked)} />
            {t("limitStore")}
          </label>
        </div>
      )}
      {kind === "plan" && <p className="text-xs text-muted">{t("planLimitsHint")}</p>}
      <Button type="submit" disabled={!itemId.trim() || !name.trim()}>
        {t("itemSave")}
      </Button>
    </form>
  );
}

function CouponsSection() {
  const [catalog, reload] = useLoaded(useCallback(() => billingAdmin.catalog(), []));
  const [code, setCode] = useState("");
  const [name, setName] = useState("");
  const [percent, setPercent] = useState("");
  const [duration, setDuration] = useState<"once" | "repeating" | "forever">("once");
  const [months, setMonths] = useState("3");
  const act = useAction();
  const add = async (event: FormEvent) => {
    event.preventDefault();
    const body = {
      code: code.trim().toUpperCase(),
      name: name.trim(),
      percent_off: Number(percent),
      amount_off: null,
      currency: "eur",
      duration,
      duration_in_months: duration === "repeating" ? Number(months) : null,
    };
    if (await act(() => billingAdmin.putCoupon(body.code.toLowerCase().replaceAll("_", "-"), body))) {
      setCode("");
      setName("");
      setPercent("");
      await reload();
    }
  };
  const remove = async (couponId: string) => {
    if (await act(() => billingAdmin.removeCoupon(couponId))) await reload();
  };
  return (
    <Section title={t("couponsTitle")}>
      <ul className="space-y-1" data-testid="coupons">
        {(catalog?.coupons ?? []).map((coupon) => (
          <li key={coupon.id} className="flex flex-wrap items-center gap-2">
            <code>{coupon.code}</code>
            <span>{coupon.percent_off ? `${coupon.percent_off} %` : money(coupon.amount_off ?? 0, coupon.currency)}</span>
            <span className="text-muted">{coupon.name}</span>
            <span className={coupon.archived_at ? "text-muted" : coupon.synced ? "text-ok" : "text-bad"}>
              {t(coupon.archived_at ? "itemArchived" : coupon.synced ? "itemSynced" : "itemNotSynced")}
            </span>
            {!coupon.archived_at && (
              <span className="ml-auto">
                <Button kind="danger" onClick={() => void remove(coupon.id)}>
                  {t("itemRemove")}
                </Button>
              </span>
            )}
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="grid gap-2 sm:grid-cols-5 sm:items-end">
        <TextInput label={t("couponCode")} value={code} onChange={setCode} />
        <TextInput label={t("couponName")} value={name} onChange={setName} />
        <TextInput label={t("couponPercent")} value={percent} onChange={setPercent} />
        <label className="block">
          <span className="mb-1 block text-xs text-muted">{t("couponDuration")}</span>
          <select className="w-full rounded-md border border-line bg-bg px-2 py-1.5" value={duration}
            onChange={(e) => setDuration(e.target.value as typeof duration)}>
            <option value="once">{t("durationOnce")}</option>
            <option value="repeating">{t("durationRepeating")}</option>
            <option value="forever">{t("durationForever")}</option>
          </select>
        </label>
        {duration === "repeating" && <TextInput label={t("couponMonths")} value={months} onChange={setMonths} />}
        <Button type="submit" disabled={!code.trim() || !name.trim() || !(Number(percent) > 0)}>
          {t("couponSave")}
        </Button>
      </form>
    </Section>
  );
}
