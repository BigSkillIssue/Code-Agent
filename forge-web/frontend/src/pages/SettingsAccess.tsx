// Settings that give Forge access to things: model API keys, git hosts; and where one is
// signed in, and what this month cost.

import { type FormEvent, useCallback, useState } from "react";
import { account } from "../api/account";
import { t } from "../lib/i18n";
import { Button, date, money, Section, TextInput, useAction, useLoaded } from "./parts";

export function KeysSection() {
  const [keys, reload] = useLoaded(useCallback(() => account.keys(), []));
  const [providers] = useLoaded(useCallback(() => account.providers(), []));
  const [provider, setProvider] = useState("anthropic");
  const [key, setKey] = useState("");
  const act = useAction();
  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => account.addKey(provider, key.trim()))) {
      setKey("");
      await reload();
    }
  };
  return (
    <Section title={t("apiKeys")} hint={t("apiKeysHint")}>
      <ul className="space-y-1">
        {(keys ?? []).map((k) => (
          <li key={k.id} className="flex items-center gap-2">
            <span className="flex-1">
              {k.provider} <code className="text-muted">…{k.hint}</code>
            </span>
            <Button kind="danger" onClick={() => void act(() => account.removeKey(k.id)).then(reload)}>
              {t("remove")}
            </Button>
          </li>
        ))}
      </ul>
      <form className="flex flex-wrap items-end gap-2" onSubmit={add}>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">{t("provider")}</span>
          <select
            className="rounded-md border border-line bg-bg px-2 py-1.5"
            value={provider}
            onChange={(event) => setProvider(event.target.value)}
          >
            {(providers ?? [{ name: "anthropic" }]).map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
              </option>
            ))}
          </select>
        </label>
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

export function GitSection({ github }: { github: boolean }) {
  const [credentials, reload] = useLoaded(useCallback(() => account.gitCredentials(), []));
  const [host, setHost] = useState("github.com");
  const [token, setToken] = useState("");
  const act = useAction();
  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => account.addGitCredential(host.trim(), token.trim()))) {
      setToken("");
      await reload();
    }
  };
  return (
    <Section title={t("gitAccess")} hint={t("gitAccessHint")}>
      <ul className="space-y-1">
        {(credentials ?? []).map((c) => (
          <li key={c.id} className="flex items-center gap-2">
            <span className="flex-1">
              {c.host} <span className="text-muted">{c.username}</span> <code className="text-muted">…{c.hint}</code>
            </span>
            <Button kind="danger" onClick={() => void act(() => account.removeGitCredential(c.id)).then(reload)}>
              {t("remove")}
            </Button>
          </li>
        ))}
      </ul>
      {github && (
        <a className="inline-block rounded-md border border-line px-3 py-1.5" href="/api/auth/oauth/github/start?intent=repos&next=/settings">
          {t("connectGithub")}
        </a>
      )}
      <form className="flex flex-wrap items-end gap-2" onSubmit={add}>
        <div className="w-40">
          <TextInput label={t("gitHost")} value={host} onChange={setHost} />
        </div>
        <div className="min-w-48 flex-1">
          <TextInput label={t("accessToken")} type="password" value={token} onChange={setToken} />
        </div>
        <Button type="submit" kind="primary" disabled={token.trim().length < 8}>
          {t("save")}
        </Button>
      </form>
    </Section>
  );
}

export function SessionsSection() {
  const [sessions, reload] = useLoaded(useCallback(() => account.sessions(), []));
  const act = useAction();
  return (
    <Section title={t("sessions")}>
      <ul className="space-y-1">
        {(sessions ?? []).map((s) => (
          <li key={s.id} className="flex items-center gap-2">
            <span className="min-w-0 flex-1 truncate" title={s.user_agent}>
              {s.current ? <strong>{t("thisBrowser")}</strong> : s.user_agent || "?"}{" "}
              <span className="text-muted">
                {s.ip} · {date(s.last_seen_at)}
              </span>
            </span>
            {!s.current && (
              <Button onClick={() => void act(() => account.endSession(s.id)).then(reload)}>{t("signOut")}</Button>
            )}
          </li>
        ))}
      </ul>
    </Section>
  );
}

export function UsageSection() {
  const [usage] = useLoaded(useCallback(() => account.usage(), []));
  if (!usage) return null;
  return (
    <Section title={t("usageThisMonth")}>
      <p>
        {t("onServerKeys")}: <strong>{money(usage.server_keys_usd)}</strong>
        {usage.limit_usd !== null && (
          <span className="text-muted">
            {" "}
            / {money(usage.limit_usd)} {t("limit")}
          </span>
        )}
      </p>
      {usage.by_model.length > 0 && (
        <table className="w-full text-left">
          <thead className="text-xs text-muted">
            <tr>
              <th>{t("model")}</th>
              <th>{t("key")}</th>
              <th className="text-right">{t("calls")}</th>
              <th className="text-right">{t("cost")}</th>
            </tr>
          </thead>
          <tbody>
            {usage.by_model.map((row) => (
              <tr key={`${row.provider}/${row.model}/${row.key_kind}`}>
                <td>
                  {row.provider}/{row.model}
                </td>
                <td>{row.key_kind}</td>
                <td className="text-right">{row.calls}</td>
                <td className="text-right">{money(row.cost_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Section>
  );
}
