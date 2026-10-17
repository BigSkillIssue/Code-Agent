// Every customer's billing for the admin (W40c): the cancellations that wait for an admin, every
// account with what it pays for, and one customer opened: their limits, a plan given for free,
// subscriptions ended at Stripe, orders, apps unlocked or taken back, and their dunning case
// held, resumed or closed. The server audits each change.

import { type FormEvent, useCallback, useState } from "react";
import { billingAdmin } from "../api/billing";
import { type CustomerDetail, customersAdmin } from "../api/customers";
import { t, type TextKey } from "../lib/i18n";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

const STAGES: Record<string, TextKey> = {
  grace: "stageGrace",
  suspended: "stageSuspended",
  noticed: "stageNoticed",
  deleted: "stageDeleted",
};
/** A dunning case's stage for people. */
const stage = (name: string) => (STAGES[name] ? t(STAGES[name]) : name);

export function CustomersTab() {
  const [query, setQuery] = useState("");
  const [rows, reload] = useLoaded(useCallback(() => customersAdmin.list(query.trim()), [query]));
  const [opened, open] = useState("");
  return (
    <div className="space-y-4" data-testid="admin-customers">
      <WaitingCancellations />
      <Section title={t("customersTitle")} hint={t("customersHint")}>
        <TextInput label={t("customerSearch")} value={query} onChange={setQuery} />
        <table className="w-full text-left">
          <thead className="text-xs text-muted">
            <tr>
              <th>{t("name")}</th>
              <th>{t("customerPaying")}</th>
              <th>{t("customerGift")}</th>
              <th>{t("customerDunning")}</th>
              <th />
            </tr>
          </thead>
          <tbody>
            {rows?.map((row) => (
              <tr key={row.id} className="border-t border-line" data-testid="customer">
                <td>
                  {row.name || row.email} <span className="text-muted">{row.email}</span>
                </td>
                <td>{row.paying.join(", ") || "–"}</td>
                <td>{row.gift || "–"}</td>
                <td>{row.dunning ? `${stage(row.dunning)}${row.held ? ` (${t("customerHeld")})` : ""}` : "–"}</td>
                <td>
                  <Button onClick={() => open(row.id)}>{t("customerOpen")}</Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>
      {opened && <Customer key={opened} userId={opened} onChanged={reload} />}
    </div>
  );
}

function WaitingCancellations() {
  const [lists, reload] = useLoaded(useCallback(() => customersAdmin.cancellations(), []));
  const act = useAction();
  const carry = (id: string, action: "period_end" | "now" | "nothing") =>
    void act(() => customersAdmin.carryOut(id, action)).then(reload);
  return (
    <Section title={t("cancellationsTitle")}>
      {lists?.waiting.length === 0 && <p className="text-muted">{t("cancellationsEmpty")}</p>}
      {lists?.waiting.map((c) => (
        <div key={c.id} className="rounded-md border border-line p-2" data-testid="waiting-cancellation">
          <p>
            {t("cancellationLine", { name: c.name, email: c.email, received: date(c.received_at), contract: c.contract || t("cancelAll") })}
            {c.extraordinary && <span className="text-bad"> · {t("cancellationForCause")}</span>}
          </p>
          {c.reason && <p className="text-muted">{c.reason}</p>}
          <div className="mt-1 flex flex-wrap gap-2">
            <Button onClick={() => carry(c.id, "period_end")}>{t("carryPeriodEnd")}</Button>
            <Button kind="danger" onClick={() => carry(c.id, "now")}>
              {t("carryNow")}
            </Button>
            <Button onClick={() => carry(c.id, "nothing")}>{t("carryNothing")}</Button>
          </div>
        </div>
      ))}
    </Section>
  );
}

function Customer({ userId, onChanged }: { userId: string; onChanged: () => Promise<void> }) {
  const [shown, reload] = useLoaded(useCallback(() => customersAdmin.customer(userId), [userId]));
  const act = useAction();
  const change = (work: () => Promise<unknown>) =>
    void act(work).then(async (worked) => {
      if (worked) await Promise.all([reload(), onChanged()]);
    });
  if (!shown) return null;
  return (
    <Section title={shown.name || shown.email} hint={shown.stripe_customer ? t("customerStripe", { id: shown.stripe_customer }) : undefined}>
      <div data-testid="customer-detail" className="space-y-3">
        <p>{t("customerLimits", { source: shown.entitlements.source })}</p>
        <GiftForm shown={shown} change={change} />
        <Subscriptions shown={shown} change={change} />
        <Apps shown={shown} change={change} />
        <Dunning shown={shown} change={change} />
        <Orders shown={shown} />
      </div>
    </Section>
  );
}

type Change = (work: () => Promise<unknown>) => void;

function GiftForm({ shown, change }: { shown: CustomerDetail; change: Change }) {
  const [catalog] = useLoaded(useCallback(() => billingAdmin.catalog(), []));
  const plans = catalog?.items.filter((i) => i.kind === "plan" && !i.archived_at) ?? [];
  const [plan, setPlan] = useState("");
  const [until, setUntil] = useState("");
  const [note, setNote] = useState("");
  const chosen = plan || plans[0]?.id || "";
  const give = (event: FormEvent) => {
    event.preventDefault();
    const end = until ? new Date(`${until}T23:59:59`).getTime() / 1000 : 0;
    change(() => customersAdmin.give(shown.id, chosen, end, note.trim()));
  };
  const gift = shown.gift;
  return (
    <form onSubmit={give} className="space-y-2" data-testid="gift">
      <h3 className="font-medium">{t("giftTitle")}</h3>
      {gift && (
        <p>
          {t("giftShown", { plan: gift.item_id, until: gift.until ? date(gift.until) : t("giftForGood") })}{" "}
          {gift.note && <span className="text-muted">({gift.note})</span>}
        </p>
      )}
      <div className="flex flex-wrap items-end gap-2">
        <label className="block">
          <span className="mb-1 block text-xs text-muted">{t("giftPlan")}</span>
          <select className="rounded-md border border-line bg-bg px-2 py-1.5" value={chosen} onChange={(e) => setPlan(e.target.value)}>
            {plans.map((p) => (
              <option key={p.id} value={p.id}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
        <TextInput label={t("giftUntil")} type="date" value={until} onChange={setUntil} />
        <TextInput label={t("giftNote")} value={note} onChange={setNote} />
        <Button type="submit" kind="primary" disabled={!chosen}>
          {t("giftSave")}
        </Button>
        {gift && <Button onClick={() => change(() => customersAdmin.takeBack(shown.id))}>{t("giftRemove")}</Button>}
      </div>
    </form>
  );
}

function Subscriptions({ shown, change }: { shown: CustomerDetail; change: Change }) {
  const running = shown.subscriptions.filter((s) => s.status !== "canceled");
  const ids = [...new Set(running.map((s) => s.stripe_subscription).filter(Boolean))];
  return (
    <div>
      <h3 className="font-medium">{t("subsTitle")}</h3>
      {shown.subscriptions.length === 0 && <p className="text-muted">{t("subsEmpty")}</p>}
      {shown.subscriptions.map((s, index) => (
        <p key={`${s.stripe_subscription}-${s.item_id}-${index}`} className="text-muted">
          {s.item_id} · {s.status} · {s.interval} · {date(s.current_period_end)}
        </p>
      ))}
      {ids.map((sub) => (
        <div key={sub} className="mt-1 flex flex-wrap items-center gap-2" data-testid="subscription">
          <code>{sub}</code>
          <Button onClick={() => change(() => customersAdmin.end(shown.id, sub, false))}>{t("subEndAtPeriod")}</Button>
          <Button kind="danger" onClick={() => change(() => customersAdmin.end(shown.id, sub, true))}>
            {t("subEndNow")}
          </Button>
        </div>
      ))}
    </div>
  );
}

function Apps({ shown, change }: { shown: CustomerDetail; change: Change }) {
  return (
    <div>
      <h3 className="font-medium">{t("customerAppsTitle")}</h3>
      {shown.apps.length === 0 && <p className="text-muted">{t("customerAppsEmpty")}</p>}
      {shown.apps.map((app) => (
        <div key={app.project_id} className="flex flex-wrap items-center gap-2" data-testid="customer-app">
          <span>
            {app.name}: {t(app.by === "paid" ? "appByPaid" : app.by === "admin" ? "appByAdmin" : "appNotYet")}
          </span>
          {app.unlocked ? (
            <Button kind="danger" onClick={() => change(() => customersAdmin.app(shown.id, app.project_id, "lock"))}>
              {t("appLock")}
            </Button>
          ) : (
            <Button onClick={() => change(() => customersAdmin.app(shown.id, app.project_id, "unlock"))}>{t("appUnlock")}</Button>
          )}
        </div>
      ))}
    </div>
  );
}

function Dunning({ shown, change }: { shown: CustomerDetail; change: Change }) {
  const dunning = shown.dunning;
  const step = (action: "hold" | "resume" | "close") => change(() => customersAdmin.dunning(shown.id, action));
  return (
    <div data-testid="dunning">
      <h3 className="font-medium">{t("dunningTitle")}</h3>
      {!dunning && <p className="text-muted">{t("dunningNone")}</p>}
      {dunning && (
        <>
          <p>{t("dunningLine", { stage: stage(dunning.stage), since: date(dunning.stage_at), opened: date(dunning.opened_at) })}</p>
          {dunning.closed_at > 0 && <p className="text-muted">{t("dunningClosed", { closed: date(dunning.closed_at) })}</p>}
          {dunning.held_at > 0 && <p className="text-muted">{t("dunningHeldSince", { held: date(dunning.held_at) })}</p>}
          {!dunning.closed_at && (
            <div className="mt-1 flex flex-wrap gap-2">
              {dunning.held_at ? (
                <Button onClick={() => step("resume")}>{t("dunningResume")}</Button>
              ) : (
                <Button onClick={() => step("hold")}>{t("dunningHold")}</Button>
              )}
              <Button kind="danger" onClick={() => step("close")}>
                {t("dunningClose")}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function Orders({ shown }: { shown: CustomerDetail }) {
  return (
    <div>
      <h3 className="font-medium">{t("ordersTitle")}</h3>
      {shown.orders.length === 0 && <p className="text-muted">{t("ordersEmpty")}</p>}
      {shown.orders.map((o) => (
        <p key={o.id} className="text-muted">
          {date(o.created_at)} · {o.item_id} · {o.interval} · {o.status}
        </p>
      ))}
      {shown.cancellations.length > 0 && <h3 className="mt-2 font-medium">{t("customerCancellations")}</h3>}
      {shown.cancellations.map((c) => (
        <p key={c.id} className="text-muted">
          {date(c.received_at)} · {c.contract || "–"} · {c.status} · {date(c.effective_at)}
        </p>
      ))}
    </div>
  );
}
