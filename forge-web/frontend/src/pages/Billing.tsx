// A customer's plan and billing (W33a2): what they may use, the plans and extras (chosen as on
// Claude's plan page, `Plans.tsx`, W43), cancelling, the VAT ID and the maker's own Stripe account.
// The legal texts are accepted before an order (W34).

import { useCallback, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { billing, type Entitlements, type MyBilling } from "../api/billing";
import { legal } from "../api/legal";
import { t, type TextKey } from "../lib/i18n";
import { Select } from "../components/Select";
import { LegalConsent, VatSection } from "./Legal";
import { Button, date, Section, useAction, useLoaded } from "./parts";
import { Extras, type Order, OrderDialog, PlanPicker } from "./Plans";

const STATUS: Record<string, TextKey> = {
  active: "subActive",
  trialing: "subTrialing",
  past_due: "subPastDue",
  canceled: "subCanceled",
  incomplete: "subIncomplete",
};
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
  const [ordering, setOrdering] = useState<Order | null>(null);
  const ready = accepted !== null && accepted.missing.length === 0;
  const [query] = useSearchParams();
  const current = mine?.entitlements.source.startsWith("plan:") ? mine.entitlements.source.slice(5) : "";
  const named = (id: string) => offers?.find((o) => o.id === id)?.name ?? id;
  const plan = current ? named(current) : t("myPlanServer");
  const plans = (offers ?? []).filter((o) => o.kind === "plan");
  const extras = (offers ?? []).filter((o) => o.kind !== "plan");
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-6 p-4 md:p-8" data-testid="billing">
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
                {t("subscriptionLine", { item: named(s.item_id), status: t(STATUS[s.status] ?? "subOther") })}
              </p>
            ))}
            {offers?.some((o) => o.kind === "app_once") &&
              mine.apps.map((app) => (
                <p key={app.project_id} className="text-muted" data-testid="app-paid">
                  {t(app.unlocked ? "appUnlocked" : "appLocked", { name: app.name })}
                </p>
              ))}
          </Section>
        )}
        {mine?.enabled && accepted && accepted.missing.length > 0 && (
          <LegalConsent missing={accepted.missing} onAccepted={reloadAccepted} />
        )}
        {mine?.enabled && plans.length > 0 && <PlanPicker plans={plans} current={current} onOrder={setOrdering} />}
        {mine?.enabled && extras.length > 0 && <Extras extras={extras} onOrder={setOrdering} />}
        {mine && ordering && <OrderDialog order={ordering} apps={mine.apps} ready={ready} onClose={() => setOrdering(null)} />}
        {mine?.enabled && mine.subscriptions.length > 0 && <CancelSection mine={mine} named={named} />}
        {mine?.enabled && accepted && <VatSection vat={accepted.vat} />}
        {mine?.enabled && <ConnectSection />}
      </div>
    </div>
  );
}

/** The cancellation button (§312k BGB) in two steps, and Stripe's portal for payments. */
function CancelSection({ mine, named }: { mine: MyBilling; named: (id: string) => string }) {
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
          <Select
            label={t("cancelWhich")}
            className="min-w-48"
            value={contract}
            onChange={setContract}
            options={[{ value: "", label: t("cancelAll") }, ...contracts.map((c) => ({ value: c, label: named(c) }))]}
          />
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
