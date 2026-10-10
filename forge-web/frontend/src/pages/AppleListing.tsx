// "Store texts": the App Store listing Forge drafted after the approval, finished and saved by
// the user. The server checks every save with Forge's own model (Apple's limits); the URLs are
// always the user's own.

import { FileText } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { appleListing, bytes, CATEGORIES, type Frequency, LIMITS, type StoreListing } from "../api/listing";
import { t, type TextKey } from "../lib/i18n";
import { useStore } from "../state/store";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

type TextField = "name" | "subtitle" | "description" | "keywords" | "promotional_text" | "whats_new";
const FREQUENCIES: Frequency[] = ["NONE", "INFREQUENT", "FREQUENT"];
const URL_LABELS: Record<string, TextKey> = {
  support_url: "listingSupportUrl",
  marketing_url: "listingMarketingUrl",
  privacy_policy_url: "listingPrivacyUrl",
};

export function AppleListingPage() {
  const { projectId = "" } = useParams();
  const project = useStore((s) => s.projects.find((p) => p.id === projectId));
  const api = useMemo(() => appleListing(projectId), [projectId]);
  const [view, reload] = useLoaded(api.get);
  const [listing, setListing] = useState<StoreListing | null>(null);
  const [source, setSource] = useState(view?.source ?? "none");
  useEffect(() => {
    if (view) {
      setListing(view.listing);
      setSource(view.source);
    }
  }, [view]);
  const act = useAction();
  const change = useCallback((changes: Partial<StoreListing>) => setListing((l) => (l ? { ...l, ...changes } : l)), []);
  const save = async () => {
    if (listing && (await act(() => api.save(listing)))) await reload();
  };
  const redraft = async () => {
    await act(async () => {
      const draft = await api.draft();
      setListing(draft.listing);
      setSource(draft.source);
    });
  };
  const missing = (view?.missing ?? []).map((field) => t(URL_LABELS[field] ?? "listingSupportUrl"));
  return (
    <div className="flex-1 overflow-y-auto">
      <div className="mx-auto max-w-4xl space-y-4 p-4 md:p-8" data-testid="apple-listing">
        <h1 className="flex items-center gap-2 text-xl font-semibold">
          <FileText className="size-5" />
          {t("listingTitle")}
          {project && <span className="font-normal text-muted">— {project.name}</span>}
        </h1>
        <p className="text-sm text-muted">{t("listingHint")}</p>
        {view && !listing && <p className="rounded-lg border border-line bg-card p-4 text-sm">{t("listingNone")}</p>}
        {listing && (
          <>
            <div className="flex flex-wrap items-center gap-2 text-sm">
              <span className="text-muted" data-testid="listing-source">
                {source === "saved" && view ? t("listingSaved", { when: date(view.updated_at) }) : t("listingFromDraft")}
              </span>
              {missing.length > 0 && (
                <span className="text-warn" data-testid="listing-missing">
                  {t("listingMissing", { what: missing.join(", ") })}
                </span>
              )}
              <span className="ml-auto flex gap-2">
                <Button onClick={() => void redraft()}>{t("listingRedraft")}</Button>
                <Button kind="primary" onClick={() => void save()}>
                  {t("listingSave")}
                </Button>
              </span>
            </div>
            <Texts listing={listing} change={change} />
            <Links listing={listing} change={change} />
            <Ratings listing={listing} change={change} />
            <Privacy listing={listing} />
          </>
        )}
      </div>
    </div>
  );
}

interface Part {
  listing: StoreListing;
  change: (changes: Partial<StoreListing>) => void;
}

function Texts({ listing, change }: Part) {
  const field = (name: TextField, label: TextKey, long = false) => {
    const used = name === "keywords" ? bytes(listing[name]) : listing[name].length;
    const over = used > LIMITS[name];
    return (
      <label className="block" key={name}>
        <span className="mb-1 flex text-xs text-muted">
          {t(label)}
          <span className={`ml-auto ${over ? "text-bad" : ""}`}>{t("listingCount", { n: String(used), max: String(LIMITS[name]) })}</span>
        </span>
        {long ? (
          <textarea
            className="h-40 w-full rounded-md border border-line bg-bg px-2 py-1.5"
            value={listing[name]}
            onChange={(event) => change({ [name]: event.target.value })}
          />
        ) : (
          <input
            className="w-full rounded-md border border-line bg-bg px-2 py-1.5"
            value={listing[name]}
            onChange={(event) => change({ [name]: event.target.value })}
          />
        )}
      </label>
    );
  };
  return (
    <Section title={t("listingTitle")}>
      <TextInput label={t("listingLocale")} value={listing.locale} onChange={(locale) => change({ locale })} />
      {field("name", "listingName")}
      {field("subtitle", "listingSubtitle")}
      {field("description", "listingDescription", true)}
      {field("keywords", "listingKeywords")}
      {field("promotional_text", "listingPromo")}
      {field("whats_new", "listingWhatsNew", true)}
      <TextInput label={t("listingCopyright")} value={listing.copyright} onChange={(copyright) => change({ copyright })} />
      <div className="flex flex-wrap gap-3">
        <Category label={t("listingCategory")} value={listing.primary_category} onChange={(c) => change({ primary_category: c ?? listing.primary_category })} />
        <Category label={t("listingSecondCategory")} value={listing.secondary_category} optional onChange={(c) => change({ secondary_category: c })} />
      </div>
    </Section>
  );
}

function Category(props: { label: string; value: string | null; optional?: boolean; onChange: (value: string | null) => void }) {
  return (
    <label className="block text-xs text-muted">
      {props.label}
      <select
        className="mt-1 block rounded-md border border-line bg-bg px-2 py-1.5 text-sm text-fg"
        value={props.value ?? ""}
        onChange={(event) => props.onChange(event.target.value || null)}
      >
        {props.optional && <option value="">{t("listingNoCategory")}</option>}
        {CATEGORIES.map((c) => (
          <option key={c} value={c}>
            {c.replaceAll("_", " ").toLowerCase()}
          </option>
        ))}
      </select>
    </label>
  );
}

function Links({ listing, change }: Part) {
  return (
    <Section title="URLs">
      {(["support_url", "marketing_url", "privacy_policy_url"] as const).map((name) => (
        <TextInput
          key={name}
          label={t(URL_LABELS[name])}
          type="url"
          placeholder="https://"
          value={listing[name]}
          onChange={(value) => change({ [name]: value })}
        />
      ))}
    </Section>
  );
}

function Ratings({ listing, change }: Part) {
  const set = (question: string, value: Frequency | boolean) => change({ age_rating: { ...listing.age_rating, [question]: value } });
  return (
    <Section title={t("listingAgeRating")}>
      <div className="grid gap-2 md:grid-cols-2">
        {Object.entries(listing.age_rating).map(([question, value]) => (
          <label key={question} className="flex items-center gap-2 text-xs">
            <span className="flex-1">{question.replaceAll("_", " ")}</span>
            {typeof value === "boolean" ? (
              <input type="checkbox" checked={value} onChange={(event) => set(question, event.target.checked)} />
            ) : (
              <select className="rounded-md border border-line bg-bg px-1 py-0.5" value={value} onChange={(event) => set(question, event.target.value as Frequency)}>
                {FREQUENCIES.map((f) => (
                  <option key={f} value={f}>
                    {t(`frequency${f}` as TextKey)}
                  </option>
                ))}
              </select>
            )}
          </label>
        ))}
      </div>
    </Section>
  );
}

function Privacy({ listing }: { listing: StoreListing }) {
  return (
    <Section title={t("listingPrivacy")}>
      {!listing.collects_data && listing.privacy.length === 0 && <p className="text-muted">{t("listingNoData")}</p>}
      <ul className="space-y-1">
        {listing.privacy.map((answer) => (
          <li key={answer.data_type}>
            <span className="font-medium">{answer.data_type}</span>
            {answer.purposes.length > 0 && <span className="text-muted"> — {answer.purposes.join(", ")}</span>}
          </li>
        ))}
      </ul>
      {listing.notes.length > 0 && (
        <div>
          <p className="text-xs text-muted">{t("listingNotes")}</p>
          <ul className="list-disc pl-5">
            {listing.notes.map((note) => (
              <li key={note}>{note}</li>
            ))}
          </ul>
        </div>
      )}
    </Section>
  );
}
