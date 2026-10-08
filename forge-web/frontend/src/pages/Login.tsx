import { useState } from "react";
import { t } from "../lib/i18n";

export function Login() {
  const [token, setToken] = useState("");
  return (
    <div className="flex h-full items-center justify-center p-4">
      <form
        className="w-full max-w-sm space-y-4 rounded-xl border border-line bg-card p-6"
        onSubmit={(e) => {
          e.preventDefault();
          window.location.href = `/api/auth/dev-login?token=${encodeURIComponent(token.trim())}`;
        }}
      >
        <h1 className="text-xl font-semibold">{t("signIn")}</h1>
        <p className="text-sm text-muted">{t("devSignIn")}</p>
        <input
          className="w-full rounded-md border border-line bg-bg px-3 py-2"
          placeholder={t("token")}
          value={token}
          onChange={(e) => setToken(e.target.value)}
        />
        <button type="submit" className="w-full rounded-md bg-accent py-2 font-medium text-on-accent">
          {t("signIn")}
        </button>
      </form>
    </div>
  );
}
