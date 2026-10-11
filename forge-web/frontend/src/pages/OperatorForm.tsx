// "Who runs the app": the creator's details for the app's Impressum and privacy page, which the
// app servers' edge shows at fixed addresses. Going live needs them.

import { type FormEvent, useState } from "react";
import type { Operator } from "../api/hosting";
import { t, type TextKey } from "../lib/i18n";
import { Button, Section, TextInput, useAction } from "./parts";

const EMPTY: Operator = {
  name: "", representative: "", address: "", email: "", phone: "", trade_register: "", vat_id: "",
  privacy_email: "", privacy_extra: "",
};
const LINES: [keyof Operator, TextKey][] = [
  ["name", "operatorName"],
  ["representative", "operatorRepresentative"],
  ["email", "operatorEmail"],
  ["phone", "operatorPhone"],
  ["trade_register", "operatorRegister"],
  ["vat_id", "operatorVat"],
  ["privacy_email", "operatorPrivacyEmail"],
];

export function OperatorForm(props: {
  saved: Operator | null;
  canEdit: boolean;
  onSave: (operator: Operator) => Promise<unknown>;
  onSaved: () => Promise<void>;
}) {
  const [edited, setEdited] = useState<Operator>({ ...EMPTY, ...(props.saved ?? {}) });
  const act = useAction();
  const set = (key: keyof Operator, value: string) => setEdited((current) => ({ ...current, [key]: value }));
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (await act(() => props.onSave(edited))) await props.onSaved();
  };
  const complete = Boolean(edited.name.trim() && edited.address.trim() && edited.email.trim());
  return (
    <form onSubmit={save} data-testid="operator">
      <Section title={t("operatorTitle")} hint={t("operatorHint")}>
        <div className="grid gap-3 sm:grid-cols-2">
          {LINES.map(([key, label]) => (
            <TextInput key={key} label={t(label)} value={edited[key]} onChange={(value) => set(key, value)} />
          ))}
        </div>
        {(["address", "privacy_extra"] as const).map((key) => (
          <label key={key} className="block">
            <span className="mb-1 block text-xs text-muted">{t(key === "address" ? "operatorAddress" : "operatorPrivacyExtra")}</span>
            <textarea
              className="w-full rounded-md border border-line bg-bg px-2 py-1.5"
              rows={key === "address" ? 3 : 4}
              value={edited[key]}
              onChange={(event) => set(key, event.target.value)}
            />
          </label>
        ))}
        {props.canEdit && (
          <Button type="submit" kind="primary" disabled={!complete}>
            {t("operatorSave")}
          </Button>
        )}
      </Section>
    </form>
  );
}
