// The guided start's progress (W72) and its steps 3 and 4 over the project's chat (the page with
// steps 1 and 2 is pages/Onboarding.tsx, loaded only when it is shown).

import { t, type TextKey } from "../lib/i18n";

const STEP_NAMES: TextKey[] = ["onboardingStepGoal", "onboardingStepProject", "onboardingStepPrompt", "onboardingStepPreview"];

/** Where the guided start stands, as four marks and words for screen readers. */
export function Progress({ step }: { step: number }) {
  return (
    <div className="space-y-2">
      <p className="text-sm text-muted" aria-live="polite">
        {t("onboardingProgress", { n: String(step) })}: {t(STEP_NAMES[step - 1])}
      </p>
      <ol className="grid grid-cols-4 gap-1.5" aria-hidden>
        {STEP_NAMES.map((name, index) => (
          <li key={name} aria-current={index + 1 === step ? "step" : undefined} className="space-y-1.5">
            <span className={`block h-1.5 rounded-full ${index < step ? "bg-accent" : "bg-line"}`} />
            <span className={`hidden text-xs sm:block ${index + 1 === step ? "font-semibold" : "text-muted"}`}>{t(name)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}

/** Steps 3 and 4 over the project's chat: the first task waits in the box, then the preview. */
export function GuideBar({ step, onNext, onClose }: { step: 3 | 4; onNext: () => void; onClose: () => void }) {
  return (
    <aside className="mx-auto w-full max-w-3xl px-3 sm:px-4" data-testid="guide" aria-label={t("onboardingTitle")}>
      <div className="ember-glow-top space-y-3 rounded-xl border border-accent bg-card p-4 shadow-lg">
        <Progress step={step} />
        <p>{t(step === 3 ? "onboardingGuidePrompt" : "onboardingGuidePreview")}</p>
        <div className="flex justify-end gap-2">
          <button type="button" onClick={onClose} className="h-9 rounded-md px-3 text-sm text-muted hover:bg-panel hover:text-fg">
            {t("onboardingSkip")}
          </button>
          <button type="button" onClick={step === 3 ? onNext : onClose} className="h-9 rounded-md bg-accent px-4 text-sm font-medium text-on-accent hover:opacity-90">
            {t(step === 3 ? "onboardingNext" : "onboardingDone")}
          </button>
        </div>
      </div>
    </aside>
  );
}
