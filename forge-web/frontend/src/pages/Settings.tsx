// The user's own settings: profile and default model, password, two-factor sign-in, linked
// sign-ins; keys, git access, sessions and usage live in SettingsAccess.

import { type FormEvent, useCallback, useState } from "react";
import { account, type Me } from "../api/account";
import { api } from "../api/client";
import { t } from "../lib/i18n";
import { useStore } from "../state/store";
import type { AuthConfig } from "./Auth";
import { Button, Section, TextInput, useAction, useLoaded } from "./parts";
import { GitSection, KeysSection, SessionsSection, UsageSection } from "./SettingsAccess";
import { AppStoreSection } from "./SettingsAppStore";

export function SettingsPage() {
  const [me, reload] = useLoaded(useCallback(() => account.me(), []));
  const [config] = useLoaded(useCallback(() => api.get<AuthConfig>("/api/auth/config"), []));
  if (!me) return null;
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-4 p-4 md:p-8" data-testid="settings">
        <h1 className="text-xl font-semibold">{t("settings")}</h1>
        {me.two_factor_required && (
          <p className="rounded-md border border-warn p-3 text-sm" role="alert">
            {t("twoFactorRequired")}
          </p>
        )}
        <ProfileSection me={me} onSaved={reload} />
        {(config?.passwords ?? true) && <PasswordSection me={me} onSaved={reload} />}
        <TwoFactorSection me={me} onChanged={reload} />
        <IdentitiesSection providers={config?.providers ?? []} />
        <KeysSection />
        <GitSection github={(config?.providers ?? []).some((p) => p.name === "github")} />
        {me.apple_apps && <AppStoreSection />}
        <SessionsSection />
        <UsageSection />
      </div>
    </div>
  );
}

function ProfileSection({ me, onSaved }: { me: Me; onSaved: () => Promise<void> }) {
  const [name, setName] = useState(me.name);
  const [model, setModel] = useState(me.default_model);
  const [models] = useLoaded(useCallback(() => account.models(), []));
  const act = useAction();
  const setUser = useStore((s) => s.setUserName);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => account.update({ name, default_model: model }))) {
      setUser(name);
      await onSaved();
    }
  };
  return (
    <Section title={t("profile")}>
      <form className="space-y-3" onSubmit={save}>
        <TextInput label={t("name")} value={name} onChange={setName} auto="name" />
        <p className="text-xs text-muted">
          {t("email")}: {me.email}
        </p>
        <label className="block">
          <span className="mb-1 block text-xs text-muted">{t("defaultModel")}</span>
          <select
            className="w-full rounded-md border border-line bg-bg px-2 py-1.5"
            value={model}
            onChange={(event) => setModel(event.target.value)}
          >
            <option value="">{t("serverDefault")}</option>
            {model && !models?.some((m) => m.id === model) && <option value={model}>{model}</option>}
            {(models ?? []).map((m) => (
              <option key={m.id} value={m.id}>
                {m.id}
              </option>
            ))}
          </select>
        </label>
        <Button type="submit" kind="primary">
          {t("save")}
        </Button>
      </form>
    </Section>
  );
}

function PasswordSection({ me, onSaved }: { me: Me; onSaved: () => Promise<void> }) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [done, setDone] = useState(false);
  const act = useAction();
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => account.changePassword(current, next))) {
      setCurrent("");
      setNext("");
      setDone(true);
      await onSaved();
    }
  };
  return (
    <Section title={t("password")} hint={t("passwordHint")}>
      <form className="space-y-3" onSubmit={save}>
        {me.has_password && (
          <TextInput label={t("currentPassword")} type="password" value={current} onChange={setCurrent} auto="current-password" />
        )}
        <TextInput label={t("newPassword")} type="password" value={next} onChange={setNext} auto="new-password" />
        {done && <p className="text-ok">{t("passwordChanged")}</p>}
        <Button type="submit" kind="primary" disabled={!next}>
          {me.has_password ? t("changePassword") : t("setPassword")}
        </Button>
      </form>
    </Section>
  );
}

/** A secret in groups of four, easier to type into an app. */
export const grouped = (secret: string) => secret.match(/.{1,4}/g)?.join(" ") ?? secret;

function TwoFactorSection({ me, onChanged }: { me: Me; onChanged: () => Promise<void> }) {
  const [setup, setSetup] = useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const act = useAction();
  const start = () => act(async () => setSetup(await account.totpSetup()));
  const enable = async (event: FormEvent) => {
    event.preventDefault();
    await act(async () => {
      setRecovery((await account.totpEnable(code)).recovery_codes);
      setSetup(null);
      setCode("");
      await onChanged();
    });
  };
  const disable = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => account.totpDisable(code))) {
      setCode("");
      setRecovery(null);
      await onChanged();
    }
  };
  const codeField = <TextInput label={t("authCode")} value={code} onChange={setCode} auto="one-time-code" />;
  return (
    <Section title={t("twoFactor")} hint={t("twoFactorHint")}>
      {recovery && (
        <div className="rounded-md border border-ok p-3" data-testid="recovery-codes">
          <p className="mb-2">{t("recoveryCodesHint")}</p>
          <pre className="grid grid-cols-2 gap-1 font-mono text-sm">{recovery.join("\n")}</pre>
        </div>
      )}
      {me.totp_enabled && (
        <form className="space-y-3" onSubmit={disable}>
          <p className="text-ok">{t("twoFactorOn")}</p>
          {codeField}
          <Button type="submit" kind="danger" disabled={!code}>
            {t("turnOff")}
          </Button>
        </form>
      )}
      {!me.totp_enabled && !setup && (
        <Button kind="primary" onClick={() => void start()}>
          {t("setUpTwoFactor")}
        </Button>
      )}
      {!me.totp_enabled && setup && (
        <form className="space-y-3" onSubmit={enable}>
          <p>{t("twoFactorSteps")}</p>
          <p>
            <span className="text-xs text-muted">{t("secretKey")}: </span>
            <code className="select-all font-mono" data-testid="totp-secret">
              {grouped(setup.secret)}
            </code>
          </p>
          <a className="text-accent underline" href={setup.uri}>
            {t("openInAuthenticator")}
          </a>
          {codeField}
          <Button type="submit" kind="primary" disabled={!code}>
            {t("turnOn")}
          </Button>
        </form>
      )}
    </Section>
  );
}

function IdentitiesSection({ providers }: { providers: { name: string; label: string }[] }) {
  const [linked, reload] = useLoaded(useCallback(() => account.identities(), []));
  const act = useAction();
  if (providers.length === 0 && !linked?.length) return null;
  const unlinked = providers.filter((p) => !linked?.some((l) => l.provider === p.name));
  return (
    <Section title={t("linkedSignIns")}>
      <ul className="space-y-2">
        {(linked ?? []).map((identity) => (
          <li key={identity.provider} className="flex items-center gap-2">
            <span className="flex-1">
              {identity.label} <span className="text-muted">{identity.email || identity.username}</span>
            </span>
            <Button onClick={() => void act(() => account.unlink(identity.provider)).then(reload)}>{t("unlink")}</Button>
          </li>
        ))}
      </ul>
      <div className="flex flex-wrap gap-2">
        {unlinked.map((p) => (
          <a
            key={p.name}
            className="rounded-md border border-line px-3 py-1.5"
            href={`/api/auth/oauth/${encodeURIComponent(p.name)}/start?intent=link&next=/settings`}
          >
            {t("linkProvider", { name: p.label })}
          </a>
        ))}
      </div>
    </Section>
  );
}
