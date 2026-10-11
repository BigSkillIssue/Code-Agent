// The owner's part of a hosted app's data (W29c): an export of each live environment (its
// database and its files, made fresh and downloaded decrypted) and the app's full deletion, which
// asks for the app's name first.

import { Download, Trash2 } from "lucide-react";
import { useState } from "react";
import type { Environment, hosting, HostingOverview } from "../api/hosting";
import { t } from "../lib/i18n";
import { Button, date, Section, TextInput, useAction } from "./parts";

type Props = { api: ReturnType<typeof hosting>; data: HostingOverview; onChanged: () => Promise<void> };

export function AppData({ api, data, onChanged }: Props) {
  const act = useAction();
  const [typed, setTyped] = useState("");
  const [asked, setAsked] = useState<Environment[]>([]);
  const live = (["staging", "production"] as const).filter((env) => data[env]);
  const exportOf = async (environment: Environment) => {
    if (await act(() => api.exportData(environment))) setAsked([...asked, environment]);
  };
  const remove = async () => {
    if (await act(() => api.deleteApp())) await onChanged();
  };
  return (
    <Section title={t("appDataTitle")} hint={t("appDataHint")}>
      <div className="flex flex-wrap gap-2">
        {live.map((environment) => (
          <Button key={environment} disabled={data.deleting} onClick={() => void exportOf(environment)}>
            <Download className="size-4" />
            {t(environment === "production" ? "exportProduction" : "exportStaging")}
          </Button>
        ))}
      </div>
      {asked.length > 0 && <p className="text-muted">{t("exportStarted")}</p>}
      <ul className="space-y-1" data-testid="exports">
        {data.backups.map((backup) => (
          <li key={backup.id}>
            <a className="text-accent underline" href={api.exportUrl(backup.id)} download>
              {t(backup.environment === "production" ? "envProduction" : "envStaging")} · {date(backup.created_at)} ·{" "}
              {(backup.size / 1e6).toFixed(1)} MB
            </a>
          </li>
        ))}
      </ul>
      {data.deleting ? (
        <p className="text-bad" data-testid="deleting">
          {t("appDeleting")} {data.deleting_error && t("appDeletingFailed", { error: data.deleting_error })}
        </p>
      ) : (
        <div className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
          <TextInput label={t("deleteAppConfirm", { app: data.app })} value={typed} onChange={setTyped} />
          <Button kind="danger" disabled={typed.trim() !== data.app} onClick={() => void remove()}>
            <Trash2 className="size-4" />
            {t("deleteApp")}
          </Button>
        </div>
      )}
      {data.deleting && data.deleting_error && (
        <Button kind="danger" onClick={() => void remove()}>
          {t("deleteAppAgain")}
        </Button>
      )}
    </Section>
  );
}
