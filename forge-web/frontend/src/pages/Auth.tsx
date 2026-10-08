// Signing in: password sign-in, the first admin, sign-up (with an invite), resets, email checks.

import { useEffect, useState, type FormEvent, type ReactNode } from "react";
import { Link, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { api, ApiError } from "../api/client";
import { t } from "../lib/i18n";

export interface AuthConfig {
  setup_needed: boolean;
  passwords: boolean;
  signup: "invite" | "approval" | "open";
  providers: { name: string; label: string }[];
  mail: boolean;
  dev: boolean;
}

function hashToken(): string {
  return new URLSearchParams(window.location.hash.slice(1)).get("token") ?? "";
}

function Card({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="flex min-h-full items-center justify-center p-4">
      <div className="w-full max-w-sm space-y-4 rounded-xl border border-line bg-card p-6 shadow-sm">
        <h1 className="text-xl font-semibold">{title}</h1>
        {children}
      </div>
    </div>
  );
}

function Field(props: { label: string; type?: string; value: string; onChange: (v: string) => void; auto?: string }) {
  return (
    <label className="block space-y-1 text-sm">
      <span className="text-muted">{props.label}</span>
      <input
        className="w-full rounded-md border border-line bg-bg px-3 py-2"
        type={props.type ?? "text"}
        autoComplete={props.auto}
        value={props.value}
        onChange={(e) => props.onChange(e.target.value)}
      />
    </label>
  );
}

function useSubmit(work: () => Promise<void>) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };
  return { error, busy, submit };
}

function Submit({ busy, label }: { busy: boolean; label: string }) {
  return (
    <button type="submit" disabled={busy} className="w-full rounded-md bg-accent py-2 font-medium text-on-accent disabled:opacity-50">
      {label}
    </button>
  );
}

function ErrorText({ error }: { error: string }) {
  return error ? <p className="text-sm text-bad" role="alert">{error}</p> : null;
}

const enter = () => window.location.assign("/");
const SECOND_FACTOR = "/login?second_factor=1";

/** Go on after a sign-in step: into the app, or to the code from the authenticator app. */
function useSignedIn() {
  const navigate = useNavigate();
  return (result: { totp_required?: boolean } | undefined) => {
    if (result?.totp_required) navigate(SECOND_FACTOR);
    else enter();
  };
}

function SecondFactorPage() {
  const [code, setCode] = useState("");
  const { error, busy, submit } = useSubmit(async () => {
    await api.post("/api/auth/totp/verify", { code: code.trim() });
    enter();
  });
  return (
    <Card title={t("twoFactor")}>
      <p className="text-sm text-muted">{t("enterCode")}</p>
      <form className="space-y-3" onSubmit={submit}>
        <Field label={t("authCode")} value={code} onChange={setCode} auto="one-time-code" />
        <ErrorText error={error} />
        <Submit busy={busy} label={t("signIn")} />
      </form>
      <Link className="block text-sm text-muted underline" to="/login">
        {t("back")}
      </Link>
    </Card>
  );
}

function providerUrl(name: string, invite: string): string {
  const params = new URLSearchParams();
  if (invite) params.set("invite", invite);
  const query = params.toString();
  return `/api/auth/oauth/${encodeURIComponent(name)}/start${query ? `?${query}` : ""}`;
}

function ProviderButtons({ config, invite = "" }: { config: AuthConfig; invite?: string }) {
  if (config.providers.length === 0) return null;
  return (
    <div className="space-y-2">
      {config.providers.map((provider) => (
        <a
          key={provider.name}
          href={providerUrl(provider.name, invite)}
          className="block w-full rounded-md border border-line py-2 text-center font-medium hover:bg-panel"
        >
          {t("continueWith")} {provider.label}
        </a>
      ))}
      {config.passwords && <div className="text-center text-xs text-muted">{t("or")}</div>}
    </div>
  );
}

/** The error a provider sign-in came back with (`?auth_error=`), shown once. */
function useReturnedError(): string {
  const [error] = useState(() => new URLSearchParams(window.location.search).get("auth_error") ?? "");
  useEffect(() => {
    if (error) window.history.replaceState(null, "", window.location.pathname + window.location.hash);
  }, [error]);
  return error;
}

function LoginPage({ config }: { config: AuthConfig }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [token, setToken] = useState("");
  const [sent, setSent] = useState(false);
  const returned = useReturnedError();
  const signedIn = useSignedIn();
  const { error, busy, submit } = useSubmit(async () => {
    signedIn(await api.post<{ totp_required?: boolean }>("/api/auth/login", { email, password }));
  });
  return (
    <Card title={t("signIn")}>
      <ErrorText error={returned} />
      <ProviderButtons config={config} />
      {config.passwords && (
        <form className="space-y-3" onSubmit={submit}>
          <Field label={t("email")} type="email" auto="email" value={email} onChange={setEmail} />
          <Field label={t("password")} type="password" auto="current-password" value={password} onChange={setPassword} />
          <ErrorText error={error} />
          <Submit busy={busy} label={t("signIn")} />
        </form>
      )}
      {config.mail && (
        <button
          type="button"
          className="text-sm text-muted underline"
          onClick={() => api.post("/api/auth/forgot", { email }).then(() => setSent(true))}
        >
          {t("forgot")}
        </button>
      )}
      {sent && <p className="text-sm text-muted">{t("forgotSent")}</p>}
      {config.signup !== "invite" && config.passwords && (
        <Link className="block text-sm text-accent" to="/signup">
          {t("needAccount")}
        </Link>
      )}
      {config.dev && (
        <form
          className="space-y-2 border-t border-line pt-3"
          onSubmit={(e) => {
            e.preventDefault();
            window.location.href = `/api/auth/dev-login?token=${encodeURIComponent(token.trim())}`;
          }}
        >
          <p className="text-xs text-muted">{t("devSignIn")}</p>
          <Field label={t("token")} value={token} onChange={setToken} />
        </form>
      )}
    </Card>
  );
}

function SetupPage() {
  const [form, setForm] = useState({ email: "", name: "", password: "" });
  const { error, busy, submit } = useSubmit(async () => {
    await api.post("/api/auth/setup", { ...form, token: hashToken() });
    enter();
  });
  return (
    <Card title={t("setupTitle")}>
      <p className="text-sm text-muted">{t("setupHint")}</p>
      <form className="space-y-3" onSubmit={submit}>
        <Field label={t("name")} value={form.name} onChange={(name) => setForm({ ...form, name })} />
        <Field label={t("email")} type="email" value={form.email} onChange={(email) => setForm({ ...form, email })} />
        <Field label={t("password")} type="password" auto="new-password" value={form.password} onChange={(password) => setForm({ ...form, password })} />
        <ErrorText error={error} />
        <Submit busy={busy} label={t("create")} />
      </form>
    </Card>
  );
}

function SignupPage({ config }: { config: AuthConfig }) {
  const invite = hashToken();
  const [form, setForm] = useState({ email: "", name: "", password: "" });
  const [status, setStatus] = useState("");
  useEffect(() => {
    if (invite) {
      api
        .get<{ email: string }>(`/api/auth/invite/${invite}`)
        .then((info) => setForm((f) => ({ ...f, email: info.email || f.email })))
        .catch(() => undefined);
    }
  }, [invite]);
  const { error, busy, submit } = useSubmit(async () => {
    const result = await api.post<{ status: string }>("/api/auth/signup", { ...form, invite });
    if (result.status === "active") enter();
    else setStatus(result.status);
  });
  if (status) return <Card title={t("signupTitle")}>{status === "pending" ? t("pending") : t("checkMail")}</Card>;
  return (
    <Card title={t("signupTitle")}>
      <ProviderButtons config={config} invite={invite} />
      <form className="space-y-3" onSubmit={submit}>
        <Field label={t("name")} value={form.name} onChange={(name) => setForm({ ...form, name })} />
        <Field label={t("email")} type="email" value={form.email} onChange={(email) => setForm({ ...form, email })} />
        <Field label={t("password")} type="password" auto="new-password" value={form.password} onChange={(password) => setForm({ ...form, password })} />
        <ErrorText error={error} />
        <Submit busy={busy} label={t("signUp")} />
      </form>
      <Link className="block text-sm text-accent" to="/">
        {t("haveAccount")}
      </Link>
    </Card>
  );
}

function ResetPage() {
  const [password, setPassword] = useState("");
  const signedIn = useSignedIn();
  const { error, busy, submit } = useSubmit(async () => {
    signedIn(await api.post<{ totp_required?: boolean }>("/api/auth/reset", { token: hashToken(), password }));
  });
  return (
    <Card title={t("resetTitle")}>
      <form className="space-y-3" onSubmit={submit}>
        <Field label={t("newPassword")} type="password" auto="new-password" value={password} onChange={setPassword} />
        <ErrorText error={error} />
        <Submit busy={busy} label={t("resetTitle")} />
      </form>
    </Card>
  );
}

function VerifyPage() {
  const [message, setMessage] = useState(t("verifyTitle"));
  const navigate = useNavigate();
  useEffect(() => {
    api
      .post<{ status?: string; totp_required?: boolean }>("/api/auth/verify", { token: hashToken() })
      .then((result) => {
        if (result.totp_required) navigate(SECOND_FACTOR);
        else if (result.status === "active") enter();
        else setMessage(t("pending"));
      })
      .catch((err) => setMessage(String(err instanceof ApiError ? err.message : err)));
  }, [navigate]);
  return <Card title={t("verifyTitle")}>{message}</Card>;
}

export function AuthPages({ config }: { config: AuthConfig }) {
  const location = useLocation();
  if (config.setup_needed && location.pathname !== "/setup") return <SetupPage />;
  if (new URLSearchParams(location.search).has("second_factor")) return <SecondFactorPage />;
  return (
    <Routes>
      <Route path="/setup" element={<SetupPage />} />
      <Route path="/signup" element={<SignupPage config={config} />} />
      <Route path="/reset" element={<ResetPage />} />
      <Route path="/verify" element={<VerifyPage />} />
      <Route path="*" element={<LoginPage config={config} />} />
    </Routes>
  );
}
