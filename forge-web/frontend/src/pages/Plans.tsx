// Choosing a plan, as on Claude's own plan page (W43): the plans side by side with a
// monthly/yearly switch, each with its price, what it includes and one button; the extras (an
// app's one-time price, hosting, add-ons) in a short list below. Ordering opens a dialog with the
// price, the waiver of the right of withdrawal (§356 (5) BGB) and the button that says the order
// costs money (§312j BGB); the card goes to Stripe's own page, never to this server.

import { Check, X } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { billing, type Interval, type Limits, type MyBilling, type Offer } from "../api/billing";
import { Segmented } from "../components/Segmented";
import { Select } from "../components/Select";
import { language, t } from "../lib/i18n";
import { Button, useAction } from "./parts";

type Period = "month" | "year";
export interface Order {
  offer: Offer;
  interval: Interval;
}

/** An amount in cents, as people write it. */
export function price(cents: number, currency: string): string {
  return new Intl.NumberFormat(language === "de" ? "de-DE" : "en-US", { style: "currency", currency: currency.toUpperCase() }).format(cents / 100);
}

/** What a plan includes, one line each (a limit the plan leaves out is no limit). */
export function features(given: Partial<Limits>): string[] {
  const limit = (key: keyof Omit<Limits, "store_publishing">) => given[key] ?? null;
  const count = (value: number | null, some: "featureProjects" | "featureApps" | "featureDomains", all: "featureProjectsAll" | "featureAppsAll" | "featureDomainsAll") =>
    value === null ? t(all) : t(some, { n: String(value) });
  const lines = [count(limit("projects"), "featureProjects", "featureProjectsAll"), count(limit("hosted_apps"), "featureApps", "featureAppsAll")];
  const ai = limit("ai_budget_month_eur");
  if (ai !== null) lines.push(t("featureAi", { amount: price(ai * 100, "eur") }));
  const storage = limit("storage_gb");
  if (storage !== null) lines.push(t("featureStorage", { n: String(storage) }));
  if (limit("custom_domains") !== 0) lines.push(count(limit("custom_domains"), "featureDomains", "featureDomainsAll"));
  if (limit("mac_minutes")) lines.push(t("featureMac", { n: String(limit("mac_minutes")) }));
  if (given.store_publishing) lines.push(t("featureStores"));
  return lines;
}

/** The largest saving of paying yearly, in percent (0 when no plan has a yearly price). */
function saving(plans: Offer[]): number {
  const cuts = plans.map((p) => (p.amounts.month && p.amounts.year ? 1 - p.amounts.year / (12 * p.amounts.month) : 0));
  return Math.round(Math.max(0, ...cuts) * 100);
}

export function PlanPicker({ plans, current, onOrder }: { plans: Offer[]; current: string; onOrder: (order: Order) => void }) {
  const [period, setPeriod] = useState<Period>("month");
  const save = saving(plans);
  return (
    <section className="space-y-4" data-testid="plans">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">{t("plansTitle")}</h2>
        {save > 0 && (
          <div className="flex items-center gap-2">
            <Segmented ariaLabel={t("plansTitle")} value={period} onChange={setPeriod}
              options={[{ value: "month", label: t("billMonthly") }, { value: "year", label: t("billYearly") }]} />
            <span className="text-xs text-ok">{t("savePercent", { percent: String(save) })}</span>
          </div>
        )}
      </div>
      <div className={`grid gap-4 ${plans.length > 1 ? "md:grid-cols-2" : ""} ${plans.length > 2 ? "xl:grid-cols-3" : ""}`}>
        {plans.map((plan) => (
          <PlanCard key={plan.id} plan={plan} period={period} current={plan.id === current} onOrder={onOrder} />
        ))}
      </div>
    </section>
  );
}

function PlanCard({ plan, period, current, onOrder }: { plan: Offer; period: Period; current: boolean; onOrder: (order: Order) => void }) {
  const yearly = period === "year" && Boolean(plan.amounts.year);
  const monthly = yearly ? Math.round((plan.amounts.year ?? 0) / 12) : (plan.amounts.month ?? 0);
  return (
    <article
      data-testid="plan"
      className={`flex flex-col rounded-2xl border bg-card p-5 shadow-sm ${current ? "border-accent ring-1 ring-accent" : "border-line"}`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <h3 className="text-lg font-semibold">{plan.name}</h3>
        {plan.trial_days > 0 && !current && (
          <span className="rounded-full bg-panel px-2 py-0.5 text-xs text-ok">{t("trialFor", { days: String(plan.trial_days) })}</span>
        )}
      </div>
      {plan.description && <p className="mt-1 text-sm text-muted">{plan.description}</p>}
      <p className="mt-4">
        <span className="text-3xl font-semibold tracking-tight">{price(monthly, plan.currency)}</span>
        <span className="text-sm text-muted"> {t("perMonthShort")}</span>
      </p>
      <p className="min-h-5 text-xs text-muted">
        {yearly ? t("billedYearly", { price: price(plan.amounts.year ?? 0, plan.currency) }) : period === "year" ? t("monthlyOnly") : ""}
      </p>
      <div className="mt-4">
        {current ? (
          <p className="rounded-lg border border-line py-2 text-center text-sm text-muted">{t("currentPlan")}</p>
        ) : (
          <button
            type="button"
            className="w-full rounded-lg bg-accent py-2 text-sm font-medium text-on-accent hover:opacity-90"
            onClick={() => onOrder({ offer: plan, interval: yearly ? "year" : "month" })}
          >
            {t("choosePlan", { name: plan.name })}
          </button>
        )}
      </div>
      <ul className="mt-5 space-y-2 text-sm">
        {features(plan.limits).map((line) => (
          <li key={line} className="flex items-start gap-2">
            <Check className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
            <span>{line}</span>
          </li>
        ))}
      </ul>
    </article>
  );
}

export function Extras({ extras, onOrder }: { extras: Offer[]; onOrder: (order: Order) => void }) {
  return (
    <section className="space-y-2" data-testid="extras">
      <h2 className="text-lg font-semibold">{t("extrasTitle")}</h2>
      <ul className="divide-y divide-line rounded-2xl border border-line bg-card">
        {extras.map((extra) => {
          const interval = (Object.keys(extra.amounts) as Interval[]).find((i) => extra.amounts[i]) ?? "month";
          return (
            <li key={extra.id} data-testid="offer" className="flex flex-wrap items-center gap-3 px-4 py-3">
              <div className="min-w-0 flex-1">
                <p className="font-medium">{extra.name}</p>
                {extra.description && <p className="text-sm text-muted">{extra.description}</p>}
              </div>
              <span className="text-sm">{t(interval === "once" ? "once" : "perMonth", { price: price(extra.amounts[interval] ?? 0, extra.currency) })}</span>
              <Button onClick={() => onOrder({ offer: extra, interval })}>{t("orderExtra")}</Button>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

/** The order itself: the price, the app for an app's price, the waiver and the paid button. */
export function OrderDialog({ order, apps, ready, onClose }: { order: Order; apps: MyBilling["apps"]; ready: boolean; onClose: () => void }) {
  const { offer, interval } = order;
  const title = useId();
  const [waiver, setWaiver] = useState(false);
  const open = apps.filter((a) => !a.unlocked);
  const perApp = offer.kind === "app_once";
  const [app, setApp] = useState(open[0]?.project_id ?? "");
  const act = useAction();
  useEffect(() => {
    const escape = (event: KeyboardEvent) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, [onClose]);
  const order_ = async () => {
    const stripe = { url: "" };
    const made = perApp ? billing.checkout(offer.id, interval, waiver, app) : billing.checkout(offer.id, interval, waiver);
    if (await act(async () => void (stripe.url = (await made).url))) window.location.assign(stripe.url); // Stripe's own page takes the card
  };
  const amount = price(offer.amounts[interval] ?? 0, offer.currency);
  const priceText = t(interval === "year" ? "perYear" : interval === "once" ? "once" : "perMonth", { price: amount });
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div role="dialog" aria-modal="true" aria-labelledby={title} className="w-full max-w-md space-y-4 rounded-2xl border border-line bg-card p-6 shadow-xl" data-testid="order">
        <div className="flex items-start justify-between gap-2">
          <h2 id={title} className="text-lg font-semibold">{t("orderTitle", { name: offer.name })}</h2>
          <button type="button" aria-label={t("close")} onClick={onClose} className="text-muted hover:text-fg">
            <X className="size-5" />
          </button>
        </div>
        <p className="text-2xl font-semibold">{priceText}</p>
        {offer.kind === "plan" && offer.trial_days > 0 && <p className="text-sm text-ok">{t("orderTrialNote", { days: String(offer.trial_days) })}</p>}
        {perApp &&
          (open.length > 0 ? (
            <Select label={t("appChoose")} value={app} onChange={setApp} options={open.map((a) => ({ value: a.project_id, label: a.name }))} />
          ) : (
            <p className="text-sm text-muted">{t("appNoneLeft")}</p>
          ))}
        <label className="flex items-start gap-2 text-sm">
          <input type="checkbox" className="mt-0.5" checked={waiver} onChange={(e) => setWaiver(e.target.checked)} />
          <span>{t("waiverBox")}</span>
        </label>
        {!ready && <p className="text-sm text-warn">{t("orderLegalFirst")}</p>}
        <div className="flex justify-end gap-2">
          <Button onClick={onClose}>{t("cancel")}</Button>
          <Button kind="primary" disabled={!waiver || !ready || (perApp && !app)} onClick={() => void order_()}>
            {t("orderButton")}
          </Button>
        </div>
      </div>
    </div>
  );
}
