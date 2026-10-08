// Administration of the server itself: its model keys, everyone's usage, settings that change
// at run time, and the audit log.

import { type FormEvent, useCallback, useState } from "react";
import { account, admin, type ServerSettings } from "../api/account";
import { t } from "../lib/i18n";
import { Button, date, money, Section, TextInput, useAction, useLoaded } from "./parts";

export function ServerKeysTab() {
  const [keys, reload] = useLoaded(useCallback(() => admin.keys(), []));
  const [providers] = useLoaded(useCallback(() => account.providers(), []));
  const [provider, setProvider] = useState("anthropic");
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const act = useAction();
  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => admin.addKey(provider, key.trim(), name.trim()))) {
      setKey("");
      setName("");
      await reload();
    }
  };
  return (
    <Section title={t("serverKeys")} hint={t("serverKeysHint")}>
      <ul className="space-y-1">
        {(keys ?? []).map((k) => (
          <li key={k.id} className="flex items-center gap-2">
            <span className="flex-1">
              {k.provider} {k.name && <span>({k.name})</span>} <code className="text-muted">…{k.hint}</code>
              <span className="ml-2 text-xs text-muted">
                {t("lastUsed")}: {date(k.last_used_at)}
              </span>
            </span>
            <Button kind="danger" onClick={() => void act(() => admin.removeKey(k.id)).then(reload)}>
              {t("remove")}
            </Button>
          </li>
        ))}
      </ul>
      <form className="flex flex-wrap items-end gap-2" onSubmit={add}>
        <select aria-label={t("provider")} className="rounded-md border border-line bg-bg px-2 py-1.5" value={provider} onChange={(e) => setProvider(e.target.value)}>
          {(providers ?? [{ name: "anthropic" }]).map((p) => (
            <option key={p.name} value={p.name}>
              {p.name}
            </option>
          ))}
        </select>
        <div className="w-40">
          <TextInput label={t("name")} value={name} onChange={setName} />
        </div>
        <div className="min-w-48 flex-1">
          <TextInput label={t("apiKey")} type="password" value={key} onChange={setKey} />
        </div>
        <Button type="submit" kind="primary" disabled={key.trim().length < 8}>
          {t("save")}
        </Button>
      </form>
    </Section>
  );
}

export function UsageTab() {
  const [usage] = useLoaded(useCallback(() => admin.usage(), []));
  return (
    <Section title={t("usageThisMonth")}>
      <p>
        {t("total")}: <strong data-testid="usage-total">{money(usage?.total_usd ?? 0)}</strong>
      </p>
      <table className="w-full text-left">
        <thead className="text-xs text-muted">
          <tr>
            <th>{t("name")}</th>
            <th className="text-right">{t("onServerKeys")}</th>
            <th className="text-right">{t("onOwnKeys")}</th>
            <th className="text-right">{t("calls")}</th>
          </tr>
        </thead>
        <tbody>
          {(usage?.users ?? []).map((row) => (
            <tr key={row.user_id} className="border-t border-line">
              <td className="py-1">
                {row.name || row.email || row.user_id}
                <div className="text-xs text-muted">{row.email}</div>
              </td>
              <td className="text-right">{money(row.server_usd)}</td>
              <td className="text-right">{money(row.own_usd)}</td>
              <td className="text-right">{row.calls}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

/** The fields that differ from what the server has now. */
export function changedSettings(saved: ServerSettings, edited: ServerSettings): Partial<ServerSettings> {
  const changes: Record<string, unknown> = {};
  for (const key of Object.keys(edited) as (keyof ServerSettings)[]) {
    if (JSON.stringify(edited[key]) !== JSON.stringify(saved[key])) changes[key] = edited[key];
  }
  return changes as Partial<ServerSettings>;
}

export function ServerSettingsTab() {
  const [saved, reload] = useLoaded(useCallback(() => admin.settings(), []));
  if (!saved) return null;
  return <SettingsForm key={JSON.stringify(saved)} saved={saved} onSaved={reload} />;
}

function SettingsForm({ saved, onSaved }: { saved: ServerSettings; onSaved: () => Promise<void> }) {
  const [edited, setEdited] = useState(saved);
  const [domains, setDomains] = useState(saved.allowed_domains.join(", "));
  const act = useAction();
  const set = <K extends keyof ServerSettings>(key: K, value: ServerSettings[K]) => setEdited((s) => ({ ...s, [key]: value }));
  const number = (key: "projects_per_user" | "project_disk_mb" | "sandbox_cpus" | "sandbox_pids" | "sandbox_idle_minutes" | "monthly_limit_usd", label: string) => (
    <TextInput label={label} value={String(edited[key])} onChange={(v) => set(key, Number(v))} />
  );
  const save = async (event: FormEvent) => {
    event.preventDefault();
    const allowed = domains.split(/[\s,]+/).map((d) => d.trim()).filter(Boolean);
    const changes = changedSettings(saved, { ...edited, allowed_domains: allowed });
    if (Object.keys(changes).length && (await act(() => admin.saveSettings(changes)))) await onSaved();
  };
  return (
    <form onSubmit={save} className="space-y-4" data-testid="server-settings">
      <Section title={t("signUpAndSignIn")}>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">{t("signUpMode")}</span>
          <select className="rounded-md border border-line bg-bg px-2 py-1.5" value={edited.signup} onChange={(e) => set("signup", e.target.value as ServerSettings["signup"])}>
            <option value="invite">{t("signUpInvite")}</option>
            <option value="approval">{t("signUpApproval")}</option>
            <option value="open">{t("signUpOpen")}</option>
          </select>
        </label>
        <TextInput label={t("allowedDomains")} value={domains} onChange={setDomains} placeholder="example.com, firma.de" />
        <Check label={t("passwordAccounts")} checked={edited.passwords} onChange={(v) => set("passwords", v)} />
        <Check label={t("adminTwoFactor")} checked={edited.admin_two_factor} onChange={(v) => set("admin_two_factor", v)} />
      </Section>
      <Section title={t("limits")} hint={t("limitsHint")}>
        <div className="grid gap-3 sm:grid-cols-2">
          {number("projects_per_user", t("projectsPerUser"))}
          {number("project_disk_mb", t("projectDiskMb"))}
          {number("sandbox_cpus", t("sandboxCpus"))}
          <TextInput label={t("sandboxMemory")} value={edited.sandbox_memory} onChange={(v) => set("sandbox_memory", v)} />
          {number("sandbox_pids", t("sandboxPids"))}
          {number("sandbox_idle_minutes", t("sandboxIdleMinutes"))}
        </div>
      </Section>
      <Section title={t("serverKeys")}>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">{t("serverKeysFor")}</span>
          <select className="rounded-md border border-line bg-bg px-2 py-1.5" value={edited.server_keys_for} onChange={(e) => set("server_keys_for", e.target.value as ServerSettings["server_keys_for"])}>
            <option value="admins">{t("forAdmins")}</option>
            <option value="granted">{t("forGranted")}</option>
            <option value="everyone">{t("forEveryone")}</option>
          </select>
        </label>
        {number("monthly_limit_usd", t("defaultMonthlyLimit"))}
      </Section>
      <Button type="submit" kind="primary">
        {t("save")}
      </Button>
    </form>
  );
}

function Check({ label, checked, onChange }: { label: string; checked: boolean; onChange: (value: boolean) => void }) {
  return (
    <label className="flex items-center gap-2">
      <input type="checkbox" checked={checked} onChange={(event) => onChange(event.target.checked)} />
      {label}
    </label>
  );
}

export function AuditTab() {
  const [entries] = useLoaded(useCallback(() => admin.audit(), []));
  return (
    <Section title={t("auditLog")}>
      <table className="w-full text-left text-xs">
        <thead className="text-muted">
          <tr>
            <th>{t("when")}</th>
            <th>{t("action")}</th>
            <th>{t("who")}</th>
            <th>{t("details")}</th>
          </tr>
        </thead>
        <tbody>
          {(entries ?? []).map((row, index) => (
            <tr key={`${row.at}-${index}`} className="border-t border-line align-top">
              <td className="py-1 whitespace-nowrap">{date(row.at)}</td>
              <td className="font-mono">{row.action}</td>
              <td className="font-mono">{row.user_id.slice(0, 8)}</td>
              <td className="break-all text-muted">
                {row.target} {row.detail} {row.ip}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}
