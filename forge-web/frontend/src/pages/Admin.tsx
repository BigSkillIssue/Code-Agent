// Administration: accounts and invites here; server keys, usage, server settings and the audit
// log in AdminServer. Only admins see the page; the server refuses everyone else anyway.

import { type FormEvent, useCallback, useState } from "react";
import { type Account, admin, type Grant } from "../api/account";
import { t, type TextKey } from "../lib/i18n";
import { useStore } from "../state/store";
import { AuditTab, ServerKeysTab, ServerSettingsTab, UsageTab } from "./AdminServer";
import { Button, Section, TextInput, useAction, useLoaded } from "./parts";

const TABS = ["users", "invites", "serverKeys", "usage", "serverSettings", "auditLog"] as const;
type Tab = (typeof TABS)[number];
const STATUS: Record<Account["status"], TextKey> = {
  active: "statusActive",
  pending: "statusPending",
  unverified: "statusUnverified",
  disabled: "statusDisabled",
};

export function AdminPage() {
  const user = useStore((s) => s.user);
  const [tab, setTab] = useState<Tab>("users");
  if (user?.role !== "admin") return <p className="p-8 text-muted">{t("adminsOnly")}</p>;
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-5xl space-y-4 p-4 md:p-8" data-testid="admin">
        <h1 className="text-xl font-semibold">{t("administration")}</h1>
        <div className="flex flex-wrap gap-1 border-b border-line" role="tablist">
          {TABS.map((id) => (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={tab === id}
              className={`px-3 py-2 text-sm ${tab === id ? "border-b-2 border-accent font-medium" : "text-muted"}`}
              onClick={() => setTab(id)}
            >
              {t(id)}
            </button>
          ))}
        </div>
        {tab === "users" && <UsersTab />}
        {tab === "invites" && <InvitesTab />}
        {tab === "serverKeys" && <ServerKeysTab />}
        {tab === "usage" && <UsageTab />}
        {tab === "serverSettings" && <ServerSettingsTab />}
        {tab === "auditLog" && <AuditTab />}
      </div>
    </div>
  );
}

function UsersTab() {
  const [users, reload] = useLoaded(useCallback(() => admin.users(), []));
  const [grants, reloadGrants] = useLoaded(useCallback(() => admin.grants(), []));
  const [link, setLink] = useState("");
  const act = useAction();
  const change = (work: () => Promise<unknown>) => void act(work).then(reload);
  return (
    <Section title={t("users")}>
      {link && (
        <p className="break-all rounded-md border border-line p-2" data-testid="reset-link">
          {t("resetLinkHint")} <code className="select-all">{link}</code>
        </p>
      )}
      <table className="w-full text-left">
        <thead className="text-xs text-muted">
          <tr>
            <th>{t("name")}</th>
            <th>{t("role")}</th>
            <th>{t("status")}</th>
            <th>{t("serverKeys")}</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {(users ?? []).map((u) => (
            <tr key={u.id} className="border-t border-line align-top">
              <td className="py-2">
                {u.name || "–"}
                <div className="text-xs text-muted">{u.email}</div>
                {u.totp_enabled && <div className="text-xs text-ok">{t("twoFactorOn")}</div>}
                {u.email && !u.email_verified && u.status !== "unverified" && (
                  <div className="text-xs text-warn">{t("statusUnverified")}</div>
                )}
              </td>
              <td className="py-2">
                <select
                  aria-label={t("role")}
                  className="rounded border border-line bg-bg px-1"
                  value={u.role}
                  onChange={(event) => change(() => admin.updateUser(u.id, { role: event.target.value }))}
                >
                  <option value="member">{t("roleMember")}</option>
                  <option value="admin">{t("roleAdmin")}</option>
                </select>
              </td>
              <td className="py-2">{t(STATUS[u.status])}</td>
              <td className="py-2">
                <GrantCell user={u} grant={grants?.find((g) => g.user_id === u.id)} onSaved={reloadGrants} />
              </td>
              <td className="space-x-1 space-y-1 py-2 text-right">
                <UserActions user={u} onChange={change} onLink={setLink} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function UserActions(props: {
  user: Account;
  onChange: (work: () => Promise<unknown>) => void;
  onLink: (link: string) => void;
}) {
  const { user, onChange, onLink } = props;
  const act = useAction();
  return (
    <>
      {user.status === "pending" && <Button onClick={() => onChange(() => admin.approve(user.id))}>{t("approve")}</Button>}
      {user.status === "active" && (
        <Button kind="danger" onClick={() => onChange(() => admin.updateUser(user.id, { status: "disabled" }))}>
          {t("disable")}
        </Button>
      )}
      {user.status === "disabled" && (
        <Button onClick={() => onChange(() => admin.updateUser(user.id, { status: "active" }))}>{t("enable")}</Button>
      )}
      <Button onClick={() => void act(async () => onLink((await admin.resetLink(user.id)).link))}>{t("resetLink")}</Button>
      {user.totp_enabled && (
        <Button
          onClick={() => {
            if (window.confirm(t("confirmResetTwoFactor", { name: user.name || user.email || "" }))) {
              onChange(() => admin.resetTwoFactor(user.id));
            }
          }}
        >
          {t("resetTwoFactor")}
        </Button>
      )}
    </>
  );
}

function GrantCell({ user, grant, onSaved }: { user: Account; grant?: Grant; onSaved: () => Promise<void> }) {
  const [limit, setLimit] = useState(grant?.monthly_limit_usd?.toString() ?? "");
  const act = useAction();
  const allowed = grant?.allowed ?? false;
  const save = (nextAllowed: boolean, nextLimit: string) =>
    void act(() => admin.setGrant(user.id, nextAllowed, nextLimit.trim() ? Number(nextLimit) : null)).then(onSaved);
  return (
    <div className="flex items-center gap-1">
      <input
        type="checkbox"
        aria-label={t("mayUseServerKeys")}
        checked={allowed}
        onChange={(event) => save(event.target.checked, limit)}
      />
      <input
        aria-label={t("monthlyLimit")}
        className="w-16 rounded border border-line bg-bg px-1"
        placeholder="$"
        inputMode="decimal"
        value={limit}
        onChange={(event) => setLimit(event.target.value)}
        onBlur={() => limit !== (grant?.monthly_limit_usd?.toString() ?? "") && save(allowed, limit)}
      />
    </div>
  );
}

function InvitesTab() {
  const [invites, reload] = useLoaded(useCallback(() => admin.invites(), []));
  const [email, setEmail] = useState("");
  const [role, setRole] = useState("member");
  const [link, setLink] = useState("");
  const act = useAction();
  const create = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(async () => setLink((await admin.invite(email.trim(), role)).link))) {
      setEmail("");
      await reload();
    }
  };
  return (
    <Section title={t("invites")} hint={t("invitesHint")}>
      <form className="flex flex-wrap items-end gap-2" onSubmit={create}>
        <div className="min-w-48 flex-1">
          <TextInput label={t("emailOptional")} value={email} onChange={setEmail} />
        </div>
        <select aria-label={t("role")} className="rounded-md border border-line bg-bg px-2 py-1.5" value={role} onChange={(e) => setRole(e.target.value)}>
          <option value="member">{t("roleMember")}</option>
          <option value="admin">{t("roleAdmin")}</option>
        </select>
        <Button type="submit" kind="primary">
          {t("createInvite")}
        </Button>
      </form>
      {link && (
        <p className="break-all rounded-md border border-ok p-2" data-testid="invite-link">
          {t("inviteLinkHint")} <code className="select-all">{link}</code>
        </p>
      )}
      <ul className="space-y-1">
        {(invites ?? []).map((invite) => (
          <li key={invite.id} className="flex items-center gap-2">
            <span className="flex-1">
              {invite.email || t("anyone")} <span className="text-muted">({invite.role})</span>
            </span>
            <Button kind="danger" onClick={() => void act(() => admin.withdraw(invite.id)).then(reload)}>
              {t("withdraw")}
            </Button>
          </li>
        ))}
      </ul>
    </Section>
  );
}
