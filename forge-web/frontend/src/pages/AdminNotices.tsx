// Reports of illegal content in hosted apps (DSA Art. 16): what the notifier said, and the
// admin's decision with reasons. A suspension stops the app and sends its creator the reasons.

import { useCallback, useState } from "react";
import { type AbuseNotice, hostingAdmin } from "../api/hosting";
import { t, type TextKey } from "../lib/i18n";
import { Button, date, Section, TextInput, useAction, useLoaded } from "./parts";

const CATEGORY: Record<AbuseNotice["category"], TextKey> = {
  hate: "categoryHate",
  child_abuse: "categoryChildAbuse",
  terror: "categoryTerror",
  fraud: "categoryFraud",
  copyright: "categoryCopyright",
  privacy: "categoryPrivacy",
  other: "categoryOther",
};

export function NoticesSection() {
  const [notices, reload] = useLoaded(useCallback(() => hostingAdmin.notices(), []));
  return (
    <Section title={t("noticesTitle")} hint={t("noticesHint")}>
      {notices?.length === 0 && <p className="text-muted">{t("noticesEmpty")}</p>}
      <ul className="space-y-3">
        {(notices ?? []).map((notice) => (
          <NoticeCard key={notice.id} notice={notice} onDecided={reload} />
        ))}
      </ul>
    </Section>
  );
}

function NoticeCard({ notice, onDecided }: { notice: AbuseNotice; onDecided: () => Promise<void> }) {
  const [reason, setReason] = useState("");
  const act = useAction();
  const decide = async (action: "dismiss" | "suspend") => {
    if (await act(() => hostingAdmin.decide(notice.id, action, reason.trim()))) await onDecided();
  };
  const open = notice.status === "open";
  return (
    <li className="rounded-lg border border-line p-3" data-testid="notice">
      <div className="flex flex-wrap items-baseline gap-2">
        <span className="font-medium">{notice.app}</span>
        <span className="text-bad">{t(CATEGORY[notice.category])}</span>
        <span className="text-muted">
          {notice.name ? t("noticeFrom", { name: `${notice.name} <${notice.email}>` }) : t("noticeAnonymous")}
        </span>
        <span className="ml-auto text-xs text-muted">{date(notice.created_at)}</span>
      </div>
      <p className="mt-1 break-all">{notice.location}</p>
      <p className="mt-1 whitespace-pre-wrap text-muted">{notice.reason}</p>
      {!open && (
        <p className="mt-1" data-testid="notice-decision">
          {t(notice.status === "actioned" ? "noticeActioned" : "noticeDismissed", { reason: notice.decision })}
        </p>
      )}
      {open && (
        <div className="mt-2 grid gap-2 sm:grid-cols-[1fr_auto_auto] sm:items-end">
          <TextInput label={t("noticeReason")} value={reason} onChange={setReason} />
          <Button disabled={reason.trim().length < 5} onClick={() => void decide("dismiss")}>
            {t("noticeDismiss")}
          </Button>
          <Button kind="danger" disabled={reason.trim().length < 5} onClick={() => void decide("suspend")}>
            {t("noticeSuspend")}
          </Button>
        </div>
      )}
    </li>
  );
}
