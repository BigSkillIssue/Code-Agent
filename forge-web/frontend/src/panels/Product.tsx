// What Forge's agent made of the project (W55): a website or an app with a server, the clients it
// runs on, where it runs on Forge's servers and how its latest deploys went. People look here;
// the agent decides and builds.

import { useEffect, useState } from "react";
import type { ProductInfo, ProjectApi } from "../api/project";
import { t, type TextKey } from "../lib/i18n";

interface Props {
  api: ProjectApi;
  refreshKey: number;
  onError: (message: string) => void;
}

export function ProductPanel({ api, refreshKey, onError }: Props) {
  const [info, setInfo] = useState<ProductInfo | null>(null);
  useEffect(() => {
    api
      .product()
      .then(setInfo)
      .catch((err: unknown) => onError(err instanceof Error ? err.message : String(err)));
  }, [api, refreshKey, onError]);
  if (!info) return null;
  if (!info.product) return <p className="max-w-[48ch] p-4 text-sm text-muted">{t("productNone")}</p>;
  return (
    <div className="space-y-5 overflow-y-auto p-4 text-sm" data-testid="product">
      <div>
        <h2 className="text-lg font-semibold">{info.name}</h2>
        <p className="text-muted">{t(info.kind === "site" ? "productSite" : "productFullstack")}</p>
      </div>
      <dl className="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-2">
        <dt className="text-muted">{t("productClients")}</dt>
        <dd>{(info.clients ?? []).map((c) => t(`client_${c}` as TextKey)).join(" · ")}</dd>
        {info.payments && (
          <>
            <dt className="text-muted">{t("productPayments")}</dt>
            <dd>✓</dd>
          </>
        )}
        <dt className="text-muted">{t("productStaging")}</dt>
        <dd>{info.staging_url ? <a className="text-accent underline" href={info.staging_url} target="_blank" rel="noreferrer">{info.staging_url}</a> : "–"}</dd>
        <dt className="text-muted">{t("productProduction")}</dt>
        <dd>{info.production_url ? <a className="text-accent underline" href={info.production_url} target="_blank" rel="noreferrer">{info.production_url}</a> : "–"}</dd>
      </dl>
      {!info.hosted && <p className="text-muted">{t("productNotHosted")}</p>}
      {(info.deploys ?? []).length > 0 && (
        <section>
          <h3 className="mb-2 font-medium">{t("productDeploys")}</h3>
          <ul className="space-y-1">
            {(info.deploys ?? []).map((d) => (
              <li key={`${d.environment}-${d.created_at}`} className="flex gap-2">
                <span className={d.status === "live" ? "text-ok" : d.status === "failed" ? "text-bad" : "text-muted"}>●</span>
                <span>{d.environment}</span>
                <span className="text-muted">{d.status === "running" ? d.step : d.status}</span>
                <span className="font-mono text-xs text-muted">{d.commit.slice(0, 7)}</span>
                {d.error && <span className="truncate text-bad">{d.error}</span>}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
