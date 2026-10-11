// The app's own domains (W30): a domain is named, its TXT record created at the domain's DNS
// provider, then checked; only a checked domain is served (with its own certificate).

import { Check, CircleDashed, Trash2 } from "lucide-react";
import { type FormEvent, useCallback, useState } from "react";
import type { Environment, hosting } from "../api/hosting";
import { t } from "../lib/i18n";
import { Button, Section, TextInput, useAction, useLoaded } from "./parts";

export function Domains({ api, environments }: { api: ReturnType<typeof hosting>; environments: Environment[] }) {
  const [domains, reload] = useLoaded(useCallback(() => api.domains(), [api]));
  const [domain, setDomain] = useState("");
  const environment: Environment = environments.includes("production") ? "production" : "staging";
  const act = useAction();
  const add = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => api.addDomain(domain.trim(), environment))) {
      setDomain("");
      await reload();
    }
  };
  const run = async (work: () => Promise<unknown>) => {
    if (await act(work)) await reload();
  };
  return (
    <Section title={t("domainsTitle")} hint={t("domainsHint")}>
      <ul className="space-y-2" data-testid="domains">
        {(domains ?? []).map((row) => (
          <li key={row.domain} className="rounded-md border border-line p-2" data-testid="domain">
            <div className="flex flex-wrap items-center gap-2">
              {row.verified_at ? <Check className="size-4 text-ok" /> : <CircleDashed className="size-4 text-muted" />}
              <span className="font-medium">{row.domain}</span>
              <span className="text-muted">{t(row.verified_at ? "domainVerified" : "domainWaiting")}</span>
              <span className="ml-auto flex gap-2">
                {!row.verified_at && <Button onClick={() => void run(() => api.checkDomain(row.domain))}>{t("domainCheck")}</Button>}
                <Button kind="danger" onClick={() => void run(() => api.removeDomain(row.domain))}>
                  <Trash2 className="size-4" />
                </Button>
              </span>
            </div>
            {!row.verified_at && (
              <p className="mt-1 text-xs text-muted" data-testid="domain-dns">
                {t("domainTxt", { name: row.txt_name, value: row.txt_value })}
                <br />
                {t("domainPoint", { target: row.points_to })}
              </p>
            )}
            {row.error && <p className="mt-1 text-xs text-bad">{row.error}</p>}
          </li>
        ))}
      </ul>
      <form onSubmit={add} className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
        <TextInput label={t("domainName")} value={domain} placeholder="shop.example.com" onChange={setDomain} />
        <Button type="submit" disabled={!domain.trim()}>
          {t("domainAdd")}
        </Button>
      </form>
    </Section>
  );
}
