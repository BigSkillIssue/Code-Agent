// The user's App Store Connect team key (W22): what Forge Web needs to send an approved Apple app
// to TestFlight and the App Store. The key itself is never shown again once saved.

import { type FormEvent, useCallback, useState } from "react";
import { appStoreKey } from "../api/apple";
import { t } from "../lib/i18n";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

export function AppStoreSection() {
  const [saved, reload] = useLoaded(useCallback(() => appStoreKey.get(), []));
  const [keyId, setKeyId] = useState("");
  const [issuerId, setIssuerId] = useState("");
  const [teamId, setTeamId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const act = useAction();
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (!file) return;
    const body = { key_id: keyId.trim(), issuer_id: issuerId.trim(), team_id: teamId.trim(), private_key: await readText(file) };
    if (await act(() => appStoreKey.save(body))) {
      setFile(null);
      await reload();
    }
  };
  return (
    <Section title={t("appStoreConnect")} hint={t("appStoreHint")}>
      {saved && (
        <div className="space-y-1" data-testid="appstore-key">
          <div>
            {t("keyId")} <code>{saved.key_id}</code> · {t("teamId")} <code>{saved.team_id}</code>
          </div>
          <div className={saved.check_ok ? "text-ok" : "text-bad"}>{saved.check_message}</div>
          <div className="text-xs text-muted">{t("keyChecked", { when: date(saved.checked_at) })}</div>
          <div className="flex gap-2">
            <Button onClick={() => void act(() => appStoreKey.check()).then(reload)}>{t("checkKey")}</Button>
            <Button kind="danger" onClick={() => void act(() => appStoreKey.remove()).then(reload)}>
              {t("remove")}
            </Button>
          </div>
        </div>
      )}
      <form className="grid gap-2 sm:grid-cols-3" onSubmit={save}>
        <TextInput label={t("keyId")} value={keyId} onChange={setKeyId} placeholder="ABC123DEFG" />
        <TextInput label={t("teamId")} value={teamId} onChange={setTeamId} placeholder="TEAM123456" />
        <TextInput label={t("issuerId")} value={issuerId} onChange={setIssuerId} />
        <label className="block sm:col-span-2">
          <span className="mb-1 block text-xs text-muted">{t("p8File")}</span>
          <input type="file" accept=".p8" className="block w-full text-sm" onChange={(e) => setFile(e.target.files?.[0] ?? null)} />
        </label>
        <div className="flex items-end">
          <Button type="submit" kind="primary" disabled={!file || !keyId.trim() || !issuerId.trim() || !teamId.trim()}>
            {t("save")}
          </Button>
        </div>
      </form>
    </Section>
  );
}

/** A small text file's content (FileReader: also where Blob.text is missing). */
function readText(file: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(reader.error ?? new Error("the file could not be read"));
    reader.readAsText(file);
  });
}
