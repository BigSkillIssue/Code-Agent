// Legal texts (W34): the admin publishes versions of the terms, privacy policy, data processing
// agreement and acceptable use policy (the words come from their lawyer); customers read them at
// /rechtstexte/<kind> and accept the current versions before they order; an EU VAT ID is checked
// with VIES.

import { type FormEvent, useCallback, useState } from "react";
import { LEGAL_KINDS, type LegalKind, type LegalText, legal, legalAdmin, type MissingText, type VatResult } from "../api/legal";
import { t, type TextKey } from "../lib/i18n";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

const KIND_TEXT: Record<LegalKind, TextKey> = {
  terms: "legalTerms",
  privacy: "legalPrivacy",
  dpa: "legalDpa",
  aup: "legalAup",
};
const shownAt = (kind: string) => `/rechtstexte/${kind}`;

/** The admin's texts: the current version of each, and publishing a new one. */
export function LegalTextsSection() {
  const [versions, reload] = useLoaded(useCallback(() => legalAdmin.versions(), []));
  const [kind, setKind] = useState<LegalKind>("terms");
  const newest = (k: LegalKind) => versions?.find((v) => v.kind === k); // the list comes newest first
  return (
    <Section title={t("legalTitle")} hint={t("legalHint")}>
      <ul className="space-y-1" data-testid="legal-texts">
        {LEGAL_KINDS.map((k) => (
          <li key={k} className="flex flex-wrap gap-2">
            <span className="font-medium">{t(KIND_TEXT[k])}</span>
            <CurrentVersion text={newest(k)} />
          </li>
        ))}
      </ul>
      {versions && (
        <TextForm key={`${kind}-${newest(kind)?.version ?? 0}`} kind={kind} setKind={setKind} current={newest(kind)} onSaved={reload} />
      )}
    </Section>
  );
}

function CurrentVersion({ text }: { text: LegalText | undefined }) {
  if (!text) return <span className="text-bad">{t("legalNone")}</span>;
  return (
    <a className="text-accent underline" href={shownAt(text.kind)} target="_blank" rel="noreferrer">
      {t("legalVersion", { version: String(text.version), date: date(text.published_at) })}
    </a>
  );
}

function TextForm(props: {
  kind: LegalKind;
  setKind: (kind: LegalKind) => void;
  current: LegalText | undefined;
  onSaved: () => Promise<void>;
}) {
  const [title, setTitle] = useState(props.current?.title ?? "");
  const [body, setBody] = useState(props.current?.body ?? "");
  const act = useAction();
  const publish = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => legalAdmin.publish(props.kind, title.trim(), body))) await props.onSaved();
  };
  return (
    <form onSubmit={publish} className="space-y-2">
      <label className="block">
        <span className="mb-1 block text-xs text-muted">{t("legalWhich")}</span>
        <select
          className="rounded-md border border-line bg-bg px-2 py-1.5"
          value={props.kind}
          onChange={(e) => props.setKind(e.target.value as LegalKind)}
        >
          {LEGAL_KINDS.map((k) => (
            <option key={k} value={k}>
              {t(KIND_TEXT[k])}
            </option>
          ))}
        </select>
      </label>
      <TextInput label={t("legalTextTitle")} value={title} onChange={setTitle} />
      <label className="block">
        <span className="mb-1 block text-xs text-muted">{t("legalTextBody")}</span>
        <textarea
          className="w-full rounded-md border border-line bg-bg px-2 py-1.5"
          rows={10}
          value={body}
          onChange={(e) => setBody(e.target.value)}
        />
      </label>
      <Button type="submit" kind="primary" disabled={!title.trim() || !body.trim()}>
        {t("legalPublish")}
      </Button>
    </form>
  );
}

/** The texts a customer accepts before ordering, each a box and a link to read it. */
export function LegalConsent({ missing, onAccepted }: { missing: MissingText[]; onAccepted: () => Promise<void> }) {
  const [ticked, setTicked] = useState<Record<string, boolean>>({});
  const act = useAction();
  const published = missing.filter((m) => m.version > 0);
  const accept = async () => {
    const all = () => Promise.all(published.map((m) => legal.accept(m.kind, m.version)));
    if (await act(async () => void (await all()))) await onAccepted();
  };
  return (
    <Section title={t("consentTitle")} hint={t("consentHint")}>
      <ul className="space-y-2" data-testid="consent">
        {missing.map((m) =>
          m.version === 0 ? (
            <li key={m.kind} className="text-muted">
              {t("consentUnpublished", { title: t(KIND_TEXT[m.kind]) })}
            </li>
          ) : (
            <li key={m.kind}>
              <label className="flex items-start gap-2">
                <input
                  type="checkbox"
                  checked={!!ticked[m.kind]}
                  onChange={(e) => setTicked({ ...ticked, [m.kind]: e.target.checked })}
                />
                <span>
                  {t("consentBox", { title: m.title, version: String(m.version) })}{" "}
                  <a className="text-accent underline" href={shownAt(m.kind)} target="_blank" rel="noreferrer">
                    {t("consentRead")}
                  </a>
                </span>
              </label>
            </li>
          ),
        )}
      </ul>
      {published.length > 0 && (
        <Button kind="primary" disabled={!published.every((m) => ticked[m.kind])} onClick={() => void accept()}>
          {t("consentAccept")}
        </Button>
      )}
    </Section>
  );
}

/** A business customer's EU VAT ID, checked with VIES. */
export function VatSection({ vat }: { vat: VatResult | null }) {
  const [entered, setEntered] = useState(vat?.vat_id ?? "");
  const [result, setResult] = useState<VatResult | null>(vat);
  const act = useAction();
  const check = async (event: FormEvent) => {
    event.preventDefault();
    await act(async () => setResult(await legal.vat(entered.trim())));
  };
  return (
    <Section title={t("vatTitle")} hint={t("vatHint")}>
      <form onSubmit={check} className="grid gap-2 sm:grid-cols-[1fr_auto] sm:items-end">
        <TextInput label={t("vatLabel")} value={entered} onChange={setEntered} />
        <Button type="submit" disabled={entered.trim().length < 4}>
          {t("vatCheck")}
        </Button>
      </form>
      {result && (
        <p data-testid="vat-result" className={result.valid ? "text-ok" : "text-bad"}>
          {result.valid
            ? t("vatValid", { vat: result.vat_id, name: result.name || "–" })
            : t("vatInvalid", { vat: result.vat_id })}
        </p>
      )}
    </Section>
  );
}
