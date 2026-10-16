// A customer's plan and billing (W33a2): what they may use, the offers, and the order. The order
// is placed here, with the price, the interval and the trial in view, the waiver of the right of
// withdrawal (§356 (5) BGB) and a button that says the order costs money (§312j BGB); the card
// goes to Stripe's own page, never to this server. The legal texts are accepted first (W34).

import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { billing, type Entitlements, type Interval, type MyBilling, type Offer } from "../api/billing";
import { legal } from "../api/legal";
import { t, type TextKey } from "../lib/i18n";
import { LegalConsent, VatSection } from "./Legal";
import { Button, date, Section, useAction, useLoaded } from "./parts";

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
  const [accepted, reloadAccepted] = useLoaded(useCallback(() => legal.me(), []));
  const ready = accepted !== null && accepted.missing.length === 0;
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
        {mine?.enabled && mine.subscriptions.length > 0 && <CancelSection mine={mine} />}
        {mine?.enabled && accepted && accepted.missing.length > 0 && (
          <LegalConsent missing={accepted.missing} onAccepted={reloadAccepted} />
        )}
        {mine?.enabled && offers && offers.length > 0 && (
          <Section title={t("offersTitle")}>
            <ul className="space-y-3">
              {offers.map((offer) => (
                <OfferCard key={offer.id} offer={offer} ready={ready} />
              ))}
            </ul>
          </Section>
        )}
        {mine?.enabled && accepted && <VatSection vat={accepted.vat} />}
        {mine?.enabled && <ConnectSection />}
      </div>
    </div>
  );
}

function OfferCard({ offer, ready }: { offer: Offer; ready: boolean }) {
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
        <Button kind="primary" disabled={!waiver || !ready} onClick={() => void order()}>
          {t("orderButton")}
        </Button>
      </div>
    </li>
  );
}

/** The cancellation button (§312k BGB) in two steps, and Stripe's portal for payments. */
function CancelSection({ mine }: { mine: MyBilling }) {
  const [asked, setAsked] = useState(false);
  const [contract, setContract] = useState("");
  const [done, setDone] = useState("");
  const act = useAction();
  const contracts = [...new Set(mine.subscriptions.filter((s) => s.status !== "canceled").map((s) => s.item_id))];
  const cancelNow = () =>
    act(async () => {
      const row = await billing.cancel(contract);
      const received = date(row.received_at);
      setDone(row.effective_at ? t("cancelReceived", { received, effective: date(row.effective_at) }) : t("cancelChecked", { received }));
    });
  const portal = async () => {
    const stripe = { url: "" };
    if (await act(async () => void (stripe.url = (await billing.portal()).url))) window.location.assign(stripe.url);
  };
  return (
    <Section title={t("cancelTitle")}>
      <Button onClick={() => void portal()}>{t("managePayment")}</Button>
      {done ? (
        <p data-testid="cancelled">{done}</p>
      ) : !asked ? (
        <div>
          <Button kind="danger" onClick={() => setAsked(true)}>
            {t("cancelContracts")}
          </Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-end gap-2">
          <label className="block">
            <span className="mb-1 block text-xs text-muted">{t("cancelWhich")}</span>
            <select className="rounded-md border border-line bg-bg px-2 py-1.5" value={contract} onChange={(e) => setContract(e.target.value)}>
              <option value="">{t("cancelAll")}</option>
              {contracts.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <Button kind="danger" onClick={() => void cancelNow()}>
            {t("cancelNow")}
          </Button>
        </div>
      )}
    </Section>
  );
}

/** A maker's own Stripe account, through which their apps take their customers' money. */
function ConnectSection() {
  const [state] = useLoaded(useCallback(() => billing.connect(), []));
  const act = useAction();
  const open = async (link: () => Promise<{ url: string }>) => {
    const stripe = { url: "" };
    if (await act(async () => void (stripe.url = (await link()).url))) window.location.assign(stripe.url);
  };
  if (!state) return null;
  const status = state.charges_enabled ? "connectReady" : state.connected ? "connectPending" : "connectNone";
  return (
    <Section title={t("connectTitle")} hint={t("connectHint", { fee: String(state.fee_percent) })}>
      <p data-testid="connect-state" className={state.charges_enabled ? "text-ok" : "text-muted"}>
        {t(status)}
      </p>
      {state.charges_enabled ? (
        <Button onClick={() => void open(billing.connectDashboard)}>{t("connectDashboard")}</Button>
      ) : (
        <Button kind="primary" onClick={() => void open(billing.startConnect)}>
          {t(state.connected ? "connectContinue" : "connectStart")}
        </Button>
      )}
    </Section>
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
