// The trial's notice over the app (W72): when it is over, that runs need a plan now; in its last
// three days, how many are left. Quiet otherwise.

import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { billing, type TrialView } from "../api/billing";
import { t } from "../lib/i18n";

const DAY = 86400;
const SOON_DAYS = 3;

/** The words for a trial, or "" when it needs none. */
export function trialMessage(trial: TrialView | null | undefined, now = Date.now() / 1000): string {
  if (!trial) return "";
  if (trial.status === "expired" || trial.status === "used_up") return t("trialOver");
  const days = Math.ceil((trial.ends_at - now) / DAY);
  return trial.status === "active" && days <= SOON_DAYS ? t("trialEnds", { days: String(Math.max(days, 1)) }) : "";
}

export function TrialNotice() {
  const [trial, setTrial] = useState<TrialView | null>(null);
  useEffect(() => {
    billing
      .me()
      .then((mine) => setTrial(mine?.trial ?? null))
      .catch(() => undefined);
  }, []);
  const message = trialMessage(trial);
  if (!message) return null;
  const over = trial?.status !== "active";
  return (
    <div
      role="status"
      data-testid="trial-notice"
      className={`flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-b px-4 py-2 text-sm ${over ? "border-accent bg-accent-soft font-medium" : "border-line bg-panel"}`}
    >
      <span>{message}</span>
      <Link to="/billing" className="font-semibold text-accent underline underline-offset-4">
        {t("trialChoose")}
      </Link>
    </div>
  );
}
