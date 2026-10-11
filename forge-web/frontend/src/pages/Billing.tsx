// A customer's plan and billing (W33a2): what they may use, the offers, and the order. The order
// is placed here, with the price, the interval and the trial in view, the waiver of the right of
// withdrawal (§356 (5) BGB) and a button that says the order costs money (§312j BGB); the card
// goes to Stripe's own page, never to this server.

import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { billing, type Entitlements, type Interval, type Offer } from "../api/billing";
import { t, type TextKey } from "../lib/i18n";
import { Button, Section, useAction, useLoaded } from "./parts";

const money = (cents: number, currency: string) => `${(cents / 100).toFixed(2)} ${currency.toUpperCase()}`;
const PRICE_TEXT: Record<Interval, TextKey> = { month: "perMonth", year: "perYear", once: "once" };
const LIMIT_ROWS: [keyof Entitlements, TextKey][] = [
  ["projects", "limitProjects"],
  ["hosted_apps", "limitHostedApps"],
  ["custom_domains", "limitDomains"],
  ["mac_minutes", "limitMacMinutes"],
  ["storage_mb", "limitStorage"],
];

/** One limit for people: "no limit", or the number (storage in GB). */
function shown(limits: Entitlements, key: keyof Entitlements): string {
  const value = limits[key];
  if (value === null) return t("limitNone");
  return key === "storage_mb" ? String(Number(value) / 1000) : String(value);
}

export function BillingPage() {
  const [mine] = useLoaded(useCallback(() => billing.me(), []));
  const [offers] = useLoaded(useCallback(() => billing.offers(), []));
  const [query] = useSearchParams();
  const plan = mine?.entitlements.source.startsWith("plan:")
    ? (offers?.find((o) => `plan:${o.id}` === mine.entitlements.source)?.name ?? mine.entitlements.source.slice(5))
    : t("myPlanServer");
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4 md:p-8" data-testid="billing">
        <h1 className="text-xl font-semibold">{t("billingTitle")}</h1>
        {query.get("paid") && <p className="rounded-md border border-ok p-3 text-sm">{t("orderPaid")}</p>}
        {query.get("cancelled") && <p className="rounded-md border border-line p-3 text-sm">{t("orderCancelled")}</p>}
        {mine && !mine.enabled && <p className="text-muted">{t("billingOff")}</p>}
        {mine && (
          <Section title={t("myPlan", { plan })}>
            <ul className="grid gap-1 sm:grid-cols-2" data-testid="my-limits">
              {LIMIT_ROWS.map(([key, text]) => (
                <li key={key}>
                  {t(text)}: {shown(mine.entitlements, key)}
                </li>
              ))}
            </ul>
            {mine.subscriptions.map((s) => (
              <p key={`${s.item_id}-${s.status}`} className="text-muted">
                {t("subscriptionLine", { item: s.item_id, status: s.status })}
              </p>
            ))}
          </Section>
        )}
        {mine?.enabled && offers && offers.length > 0 && (
          <Section title={t("offersTitle")}>
            <ul className="space-y-3">
              {offers.map((offer) => (
                <OfferCard key={offer.id} offer={offer} />
              ))}
            </ul>
          </Section>
        )}
      </div>
    </div>
  );
}

function OfferCard({ offer }: { offer: Offer }) {
  const intervals = (Object.keys(offer.amounts) as Interval[]).filter((i) => offer.amounts[i]);
  const [interval, choose] = useState<Interval>(intervals[0] ?? "month");
  const [waiver, setWaiver] = useState(false);
  const act = useAction();
  const order = async () => {
    const stripe = { url: "" };
    const opened = await act(async () => {
      stripe.url = (await billing.checkout(offer.id, interval, waiver)).url;
    });
    if (opened) window.location.assign(stripe.url); // Stripe's own page takes the card
  };
  return (
    <li className="rounded-lg border border-line p-3" data-testid="offer">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium">{offer.name}</span>
        {offer.trial_days > 0 && <span className="text-ok">{t("trialFor", { days: String(offer.trial_days) })}</span>}
      </div>
      {offer.description && <p className="text-muted">{offer.description}</p>}
      <div className="mt-2 flex flex-wrap gap-3">
        {intervals.map((i) => (
          <label key={i} className="flex items-center gap-1">
            {intervals.length > 1 && (
              <input type="radio" name={`interval-${offer.id}`} checked={interval === i} onChange={() => choose(i)} />
            )}
            {t(PRICE_TEXT[i], { price: money(offer.amounts[i] ?? 0, offer.currency) })}
          </label>
        ))}
      </div>
      <label className="mt-2 flex items-start gap-2 text-sm">
        <input type="checkbox" checked={waiver} onChange={(e) => setWaiver(e.target.checked)} />
        <span>{t("waiverBox")}</span>
      </label>
      <div className="mt-2">
        <Button kind="primary" disabled={!waiver} onClick={() => void order()}>
          {t("orderButton")}
        </Button>
      </div>
    </li>
  );
}

/** A link to the billing page, shown only where payments are set up. */
export function BillingLink() {
  const [mine] = useLoaded(useCallback(() => billing.me(), []));
  if (!mine?.enabled) return null;
  return (
    <Section title={t("billingTitle")}>
      <Link to="/billing" className="text-accent underline">
        {t("billingLink")}
      </Link>
    </Section>
  );
}
