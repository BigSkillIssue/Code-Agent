// Administration of hosting: the app servers (with the one-time token a new one connects with),
// the production deploys waiting for an admin (exactly the commit the server checked, with its
// checks), the hosted apps (suspend, resume, logs) and who is trusted.

import { type FormEvent, useCallback, useState } from "react";
import { type HostedApp, hostingAdmin, type QueueItem } from "../api/hosting";
import { t } from "../lib/i18n";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

export function HostingTab() {
  return (
    <div className="space-y-4" data-testid="admin-hosting">
      <QueueSection />
      <AppsSection />
      <HostsSection />
      <TrustSection />
    </div>
  );
}

function HostsSection() {
  const [hosts, reload] = useLoaded(useCallback(() => hostingAdmin.hosts(), []));
  const [name, setName] = useState("");
  const [key, setKey] = useState("");
  const [token, setToken] = useState("");
  const act = useAction();
  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(async () => setToken((await hostingAdmin.addHost(name.trim(), key.trim())).token))) {
      setName("");
      setKey("");
      await reload();
    }
  };
  const toggle = async (hostId: string, enabled: boolean) => {
    if (await act(() => hostingAdmin.setHost(hostId, enabled))) await reload();
  };
  return (
    <Section title={t("hostsTitle")} hint={t("hostsHint")}>
      <ul className="space-y-1" data-testid="hosts">
        {(hosts ?? []).map((host) => (
          <li key={host.id} className="flex flex-wrap items-center gap-2">
            <span className="font-medium">{host.name}</span>
            <span className={host.online ? "text-ok" : "text-muted"}>{t(host.online ? "hostOnline" : "hostOffline")}</span>
            <span className="text-muted">{t("hostApps", { count: String(host.apps) })}</span>
            {host.version && <span className="text-muted">v{host.version}</span>}
            <span className="ml-auto">
              <Button kind={host.enabled ? "danger" : "plain"} onClick={() => void toggle(host.id, !host.enabled)}>
                {t(host.enabled ? "hostTurnOff" : "hostTurnOn")}
              </Button>
            </span>
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="grid gap-3 sm:grid-cols-[1fr_2fr_auto] sm:items-end">
        <TextInput label={t("hostName")} value={name} placeholder="app-server-1" onChange={setName} />
        <TextInput label={t("hostKey")} value={key} onChange={setKey} />
        <Button type="submit" kind="primary" disabled={!name.trim() || !key.trim()}>
          {t("addHost")}
        </Button>
      </form>
      {token && (
        <p className="break-all rounded-md border border-accent p-2" data-testid="host-token">
          {t("hostToken", { token })}
        </p>
      )}
    </Section>
  );
}

function QueueSection() {
  const [queue, reload] = useLoaded(useCallback(() => hostingAdmin.queue(), []));
  return (
    <Section title={t("queueTitle")}>
      {queue?.length === 0 && <p className="text-muted">{t("queueEmpty")}</p>}
      <ul className="space-y-3">
        {(queue ?? []).map((item) => (
          <QueueCard key={item.id} item={item} onDecided={reload} />
        ))}
      </ul>
    </Section>
  );
}

function QueueCard({ item, onDecided }: { item: QueueItem; onDecided: () => Promise<void> }) {
  const [reason, setReason] = useState("");
  const act = useAction();
  const decide = async (work: () => Promise<unknown>) => {
    if (await act(work)) await onDecided();
  };
  return (
    <li className="rounded-lg border border-line p-3" data-testid="queue-item">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium">{t("queueBy", { project: item.project, creator: item.creator })}</span>
        <code className="text-xs" data-testid="queue-commit">
          {item.commit}
        </code>
        <span className="ml-auto text-xs text-muted">{date(item.created_at)}</span>
      </div>
      {item.review_summary && <p className="mt-1 text-muted">{item.review_summary}</p>}
      <ul className="mt-2">
        {item.checks.map((check) => (
          <li key={check.name} className={check.ok ? "text-ok" : "text-bad"}>
            {check.ok ? "✓" : "✗"} {check.name}
          </li>
        ))}
      </ul>
      {item.checks_from && <p className="text-xs text-muted">{t("checkedInStaging")}</p>}
      <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_auto_auto] sm:items-end">
        <TextInput label={t("refuseReason")} value={reason} onChange={setReason} />
        <Button kind="danger" disabled={!reason.trim()} onClick={() => void decide(() => hostingAdmin.refuse(item.id, reason.trim()))}>
          {t("refuseDeploy")}
        </Button>
        <Button kind="primary" onClick={() => void decide(() => hostingAdmin.approve(item.id))}>
          {t("approveDeploy")}
        </Button>
      </div>
    </li>
  );
}

function AppsSection() {
  const [apps, reload] = useLoaded(useCallback(() => hostingAdmin.apps(), []));
  return (
    <Section title={t("appsTitle")}>
      {apps?.length === 0 && <p className="text-muted">{t("appsEmpty")}</p>}
      <ul className="space-y-3">
        {(apps ?? []).map((app) => (
          <AppCard key={app.project_id} app={app} onChanged={reload} />
        ))}
      </ul>
    </Section>
  );
}

function AppCard({ app, onChanged }: { app: HostedApp; onChanged: () => Promise<void> }) {
  const [reason, setReason] = useState("");
  const [logs, setLogs] = useState("");
  const act = useAction();
  const suspend = async (suspended: boolean) => {
    if (await act(() => hostingAdmin.suspend(app.project_id, suspended, reason.trim()))) {
      setReason("");
      await onChanged();
    }
  };
  const environment = app.live.production ? "production" : "staging";
  const showLogs = () => act(async () => setLogs((await hostingAdmin.logs(app.project_id, environment)).log_tail));
  return (
    <li className="rounded-lg border border-line p-3" data-testid="hosted-app">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium">{app.app}</span>
        <span className="text-muted">
          {app.project} · {app.host}
        </span>
        {app.suspended && <span className="text-bad">{t("appSuspended", { reason: app.suspended_reason })}</span>}
      </div>
      <ul className="mt-1">
        {Object.entries(app.live).map(([env, live]) => (
          <li key={env}>
            {t(env === "production" ? "envProduction" : "envStaging")}: #{live?.release}{" "}
            {live?.url && (
              <a href={live.url} target="_blank" rel="noreferrer" className="text-accent underline">
                {live.url}
              </a>
            )}
          </li>
        ))}
      </ul>
      <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_auto_auto] sm:items-end">
        {app.suspended ? (
          <span />
        ) : (
          <TextInput label={t("suspendReason")} value={reason} onChange={setReason} />
        )}
        {app.suspended ? (
          <Button onClick={() => void suspend(false)}>{t("resumeApp")}</Button>
        ) : (
          <Button kind="danger" disabled={!reason.trim()} onClick={() => void suspend(true)}>
            {t("suspendApp")}
          </Button>
        )}
        <Button disabled={!Object.keys(app.live).length} onClick={() => void showLogs()}>
          {t("showLogs")}
        </Button>
      </div>
      {logs && (
        <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs" data-testid="app-logs">
          {logs}
        </pre>
      )}
    </li>
  );
}

function TrustSection() {
  const [users, reload] = useLoaded(useCallback(() => hostingAdmin.users(), []));
  const act = useAction();
  const trust = async (userId: string, trusted: boolean) => {
    if (await act(() => hostingAdmin.trust(userId, trusted))) await reload();
  };
  return (
    <Section title={t("trustTitle")} hint={t("trustHint")}>
      <ul className="space-y-1">
        {(users ?? [])
          .filter((user) => user.role !== "admin")
          .map((user) => (
            <li key={user.id}>
              <label className="flex items-center gap-2">
                <input type="checkbox" checked={user.trusted} onChange={(e) => void trust(user.id, e.target.checked)} />
                <span>{user.name || user.email}</span>
                <span className="text-muted">{user.trusted ? t("trusted") : ""}</span>
              </label>
            </li>
          ))}
      </ul>
    </Section>
  );
}
