// Administration of Apple builds: whether and for whom they run, the Macs that do them (with the
// one-time token a new Mac connects with), who may build, and the latest jobs.

import { type FormEvent, useCallback, useState } from "react";
import { admin, type ServerSettings } from "../api/account";
import { type AppleJob, appleAdmin, type AppleUser, type Mac } from "../api/apple";
import { t, type TextKey } from "../lib/i18n";
import { changedSettings } from "./AdminServer";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

const JOB_STATUS: Record<AppleJob["status"], TextKey> = {
  queued: "jobQueued",
  running: "jobRunning",
  done: "jobDone",
  failed: "jobFailed",
};

export function AppleTab() {
  return (
    <div className="space-y-4" data-testid="admin-apple">
      <AppleSettingsForm />
      <MacsSection />
      <AppleUsersSection />
      <AppleJobsSection />
    </div>
  );
}

function AppleSettingsForm() {
  const [saved, reload] = useLoaded(useCallback(() => admin.settings(), []));
  if (!saved) return null;
  return <SettingsFields key={JSON.stringify(saved)} saved={saved} onSaved={reload} />;
}

function SettingsFields({ saved, onSaved }: { saved: ServerSettings; onSaved: () => Promise<void> }) {
  const [edited, setEdited] = useState(saved);
  const act = useAction();
  const set = <K extends keyof ServerSettings>(key: K, value: ServerSettings[K]) => setEdited((s) => ({ ...s, [key]: value }));
  const save = async (event: FormEvent) => {
    event.preventDefault();
    const changes = changedSettings(saved, { ...edited, apple_reviewer_model: edited.apple_reviewer_model.trim() });
    if (Object.keys(changes).length && (await act(() => admin.saveSettings(changes)))) await onSaved();
  };
  return (
    <form onSubmit={save}>
      <Section title={t("appleBuilds")} hint={t("appleSettingsHint")}>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={edited.apple_enabled} onChange={(e) => set("apple_enabled", e.target.checked)} />
          {t("appleEnabled")}
        </label>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">{t("appleAllowed")}</span>
          <select className="rounded-md border border-line bg-bg px-2 py-1.5" value={edited.apple_allowed} onChange={(e) => set("apple_allowed", e.target.value as ServerSettings["apple_allowed"])}>
            <option value="admins">{t("forAdmins")}</option>
            <option value="granted">{t("forGranted")}</option>
            <option value="everyone">{t("forEveryone")}</option>
          </select>
        </label>
        <div className="grid gap-3 sm:grid-cols-2">
          <TextInput label={t("appleMinutes")} value={String(edited.apple_minutes_per_month)} onChange={(v) => set("apple_minutes_per_month", Number(v))} />
          <TextInput label={t("appleReviewerModel")} value={edited.apple_reviewer_model} placeholder="openai/gpt-5" onChange={(v) => set("apple_reviewer_model", v)} />
        </div>
        <Button type="submit" kind="primary">
          {t("save")}
        </Button>
      </Section>
    </form>
  );
}

function MacsSection() {
  const [macs, reload] = useLoaded(useCallback(() => appleAdmin.macs(), []));
  const [name, setName] = useState("");
  const [token, setToken] = useState("");
  const act = useAction();
  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(async () => setToken((await appleAdmin.addMac(name.trim())).token))) {
      setName("");
      await reload();
    }
  };
  return (
    <Section title={t("macs")} hint={t("macsHint")}>
      {token && <NewMacToken token={token} />}
      {macs?.length === 0 && <p className="text-muted">{t("noMacs")}</p>}
      <ul className="space-y-1">
        {(macs ?? []).map((mac) => (
          <MacRow key={mac.id} mac={mac} onChange={(work) => void act(work).then(reload)} />
        ))}
      </ul>
      <form className="flex flex-wrap items-end gap-2" onSubmit={add}>
        <div className="min-w-48 flex-1">
          <TextInput label={t("name")} value={name} placeholder="mac-mini-1" onChange={setName} />
        </div>
        <Button type="submit" kind="primary" disabled={!name.trim()}>
          {t("addMac")}
        </Button>
      </form>
    </Section>
  );
}

function NewMacToken({ token }: { token: string }) {
  const command = `forge-mac-worker run --server ${window.location.origin} --token-file ~/.forge-mac-token`;
  return (
    <div className="space-y-1 rounded-md border border-ok p-2" data-testid="mac-token">
      <p>{t("macTokenHint")}</p>
      <code className="block break-all select-all">{token}</code>
      <code className="block break-all select-all text-xs">{command}</code>
    </div>
  );
}

function MacRow({ mac, onChange }: { mac: Mac; onChange: (work: () => Promise<unknown>) => void }) {
  return (
    <li className="flex flex-wrap items-center gap-2">
      <span className={`size-2 rounded-full ${mac.online ? "bg-ok" : "bg-line"}`} />
      <span className="flex-1">
        {mac.name}
        <span className="ml-2 text-xs text-muted">
          {mac.online ? t("online") : t("offline")} · {t("lastSeen")}: {date(mac.last_seen)}
          {mac.version && ` · ${mac.version}`}
        </span>
      </span>
      <Button onClick={() => onChange(() => appleAdmin.setMac(mac.id, !mac.enabled))}>{mac.enabled ? t("disable") : t("enable")}</Button>
      <Button kind="danger" onClick={() => onChange(() => appleAdmin.removeMac(mac.id))}>
        {t("remove")}
      </Button>
    </li>
  );
}

function AppleUsersSection() {
  const [users, reload] = useLoaded(useCallback(() => appleAdmin.users(), []));
  const act = useAction();
  const grant = (user: AppleUser, allowed: boolean) => void act(() => appleAdmin.grant(user.id, allowed)).then(reload);
  return (
    <Section title={t("appleUsers")} hint={t("appleUsersHint")}>
      <table className="w-full text-left">
        <thead className="text-xs text-muted">
          <tr>
            <th>{t("name")}</th>
            <th>{t("mayBuildApple")}</th>
            <th className="text-right">{t("minutesThisMonth")}</th>
          </tr>
        </thead>
        <tbody>
          {(users ?? []).map((u) => (
            <tr key={u.id} className="border-t border-line">
              <td className="py-1">
                {u.name || u.email || u.id}
                <div className="text-xs text-muted">{u.email}</div>
              </td>
              <td>
                <input type="checkbox" aria-label={`${t("mayBuildApple")}: ${u.name || u.email}`} checked={u.allowed} onChange={(e) => grant(u, e.target.checked)} />
              </td>
              <td className="text-right">{Math.round(u.minutes_this_month)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function AppleJobsSection() {
  const [jobs] = useLoaded(useCallback(() => appleAdmin.jobs(), []));
  return (
    <Section title={t("appleJobs")}>
      {jobs?.length === 0 && <p className="text-muted">{t("noAppleJobs")}</p>}
      <table className="w-full text-left text-xs">
        <thead className="text-muted">
          <tr>
            <th>{t("when")}</th>
            <th>{t("project")}</th>
            <th>{t("who")}</th>
            <th>{t("status")}</th>
            <th className="text-right">{t("duration")}</th>
          </tr>
        </thead>
        <tbody>
          {(jobs ?? []).map((job) => (
            <tr key={job.id} className="border-t border-line align-top">
              <td className="py-1 whitespace-nowrap">{date(job.created_at)}</td>
              <td>
                {job.project} <span className="text-muted">({job.kind})</span>
              </td>
              <td>{job.user}</td>
              <td className={job.status === "failed" ? "text-bad" : ""} title={job.outcome}>
                {t(JOB_STATUS[job.status])}
              </td>
              <td className="text-right">{job.seconds ? `${Math.round(job.seconds)} s` : "–"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </Section>
  );
}
