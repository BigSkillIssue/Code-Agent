// Legal texts (W34): the admin's versioned terms, privacy policy, data processing agreement and
// acceptable use policy; a customer's acceptances and EU VAT ID.

import { api } from "./client";

export type LegalKind = "terms" | "privacy" | "dpa" | "aup";
export const LEGAL_KINDS: LegalKind[] = ["terms", "privacy", "dpa", "aup"];

export interface LegalText {
  kind: LegalKind;
  version: number;
  title: string;
  body: string;
  published_at: number;
}

/** A text the customer still has to accept (version 0: not published yet). */
export interface MissingText {
  kind: LegalKind;
  version: number;
  title: string;
}

export interface VatResult {
  vat_id: string;
  valid: boolean;
  name: string;
  address: string;
  checked_at?: number;
}

export const legal = {
  me: () => api.get<{ missing: MissingText[]; vat: VatResult | null }>("/api/legal/me"),
  accept: (kind: LegalKind, version: number) => api.post<{ accepted: boolean }>("/api/legal/accept", { kind, version }),
  vat: (vatId: string) => api.post<VatResult>("/api/legal/vat", { vat_id: vatId }),
};

export const legalAdmin = {
  versions: () => api.get<LegalText[]>("/api/admin/legal"),
  publish: (kind: LegalKind, title: string, body: string) =>
    api.post<LegalText>(`/api/admin/legal/${kind}`, { title, body }),
};
