// The guided start (W72), shown once after the first sign-in: 1) the goal, 2) a demo project made
// from the template that fits it, 3) its chat with a first task written in (not sent), 4) where
// the preview opens. Steps 1 and 2 are this page; 3 and 4 are a bar over the project's chat
// (`GuideBar` in components/Guide.tsx). Done or skipped, the server keeps that it was shown.

import { Globe, Loader2, MonitorDown, Smartphone, Sparkles } from "lucide-react";
import { type FormEvent, type ReactNode, useState } from "react";
import { useNavigate } from "react-router-dom";
import { LogoMark } from "../components/Logo";
import { t, type TextKey } from "../lib/i18n";
import { useStore } from "../state/store";
import { Progress } from "../components/Guide";

export type Goal = "web" | "mobile" | "windows" | "try";
/** What the project's chat gets from this page (router state). */
export interface GuideState {
  guide: { prompt: string };
}

const GOALS: { id: Goal; icon: ReactNode; title: TextKey; hint: TextKey; name: TextKey; prompt: TextKey }[] = [
  { id: "web", icon: <Globe className="size-7" aria-hidden />, title: "onboardingGoalWeb", hint: "onboardingGoalWebHint", name: "onboardingNameWeb", prompt: "onboardingPromptWeb" },
  { id: "mobile", icon: <Smartphone className="size-7" aria-hidden />, title: "onboardingGoalMobile", hint: "onboardingGoalMobileHint", name: "onboardingNameMobile", prompt: "onboardingPromptMobile" },
  { id: "windows", icon: <MonitorDown className="size-7" aria-hidden />, title: "onboardingGoalWindows", hint: "onboardingGoalWindowsHint", name: "onboardingNameWindows", prompt: "onboardingPromptWindows" },
  { id: "try", icon: <Sparkles className="size-7" aria-hidden />, title: "onboardingGoalTry", hint: "onboardingGoalTryHint", name: "onboardingNameTry", prompt: "onboardingPromptTry" },
];

/** The template for a goal: Forge's full-stack app where the server offers it, else empty. */
export function sourceFor(goal: Goal, sources: string[] | undefined): "app" | "empty" {
  const appAllowed = sources === undefined || sources.includes("app");
  return goal !== "try" && appAllowed ? "app" : "empty";
}

function GoalStep({ goal, setGoal, onNext }: { goal: Goal; setGoal: (goal: Goal) => void; onNext: () => void }) {
  return (
    <fieldset className="space-y-5">
      <legend className="font-display text-[34px] leading-tight font-bold sm:text-[44px]">{t("onboardingGoalTitle")}</legend>
      <div className="grid gap-3 sm:grid-cols-2">
        {GOALS.map((option) => (
          <label
            key={option.id}
            className={`flex cursor-pointer items-start gap-3 rounded-xl border-2 bg-card p-4 transition-colors has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-accent ${goal === option.id ? "border-accent" : "border-line hover:border-line-strong"}`}
          >
            <input type="radio" name="goal" value={option.id} className="sr-only" checked={goal === option.id} onChange={() => setGoal(option.id)} />
            <span className={goal === option.id ? "text-accent" : "text-muted"}>{option.icon}</span>
            <span>
              <span className="block font-semibold">{t(option.title)}</span>
              <span className="block text-sm text-muted">{t(option.hint)}</span>
            </span>
          </label>
        ))}
      </div>
      <div className="flex justify-end">
        <button type="button" onClick={onNext} className="h-11 rounded-md bg-accent px-6 font-medium text-on-accent hover:opacity-90">
          {t("onboardingNext")}
        </button>
      </div>
    </fieldset>
  );
}

function ProjectStep(props: { goal: Goal; source: "app" | "empty"; onBack: () => void; onCreate: (name: string) => Promise<void> }) {
  const chosen = GOALS.find((g) => g.id === props.goal) ?? GOALS[0];
  const [name, setName] = useState(t(chosen.name));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true);
    setError("");
    try {
      await props.onCreate(name.trim());
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };
  return (
    <form className="space-y-5" onSubmit={submit}>
      <h2 className="text-[34px] sm:text-[44px]">{t("onboardingProjectTitle")}</h2>
      <p className="max-w-[60ch] text-muted">{t(props.source === "app" ? "onboardingProjectApp" : "onboardingProjectEmpty")}</p>
      <label className="block space-y-1.5 text-sm">
        <span className="font-medium">{t("projectName")}</span>
        <input
          className="h-11 w-full rounded-md border border-line-strong bg-card px-3 text-[15px] focus:border-accent focus:outline-none"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
      </label>
      {error && <p className="text-sm text-bad" role="alert">{error}</p>}
      <div className="flex flex-wrap justify-between gap-3">
        <button type="button" onClick={props.onBack} disabled={busy} className="h-11 rounded-md border border-line px-5 hover:bg-panel">
          {t("onboardingBack")}
        </button>
        <button type="submit" disabled={busy || !name.trim()} className="inline-flex h-11 items-center gap-2 rounded-md bg-accent px-6 font-medium text-on-accent hover:opacity-90 disabled:opacity-60">
          {busy && <Loader2 className="size-4 animate-spin" aria-hidden />}
          {busy ? t("onboardingCreating") : t("onboardingCreate")}
        </button>
      </div>
    </form>
  );
}

export default function OnboardingPage() {
  const { user, createProject, finishOnboarding } = useStore();
  const navigate = useNavigate();
  const [step, setStep] = useState<1 | 2>(1);
  const [goal, setGoal] = useState<Goal>("web");
  const source = sourceFor(goal, user?.project_sources);
  const skip = async () => {
    await finishOnboarding();
    navigate("/", { replace: true });
  };
  const create = async (name: string) => {
    const project = await createProject({ name, source });
    await finishOnboarding();
    const prompt = t(GOALS.find((g) => g.id === goal)?.prompt ?? "onboardingPromptTry");
    navigate(`/p/${project.id}`, { replace: true, state: { guide: { prompt } } satisfies GuideState });
  };
  return (
    <div className="ember-glow-top min-h-full overflow-y-auto bg-bg" data-testid="onboarding">
      <div className="mx-auto max-w-2xl space-y-8 px-4 py-8 sm:py-14">
        <div className="flex items-center justify-between gap-3">
          <p className="flex items-center gap-2 font-display text-[22px] font-bold">
            <LogoMark size={30} />
            {t("onboardingTitle")}
          </p>
          <button type="button" onClick={() => void skip()} className="rounded-md px-3 py-1.5 text-sm text-muted hover:bg-panel hover:text-fg">
            {t("onboardingSkip")}
          </button>
        </div>
        <Progress step={step} />
        {step === 1 ? (
          <GoalStep goal={goal} setGoal={setGoal} onNext={() => setStep(2)} />
        ) : (
          <ProjectStep key={goal} goal={goal} source={source} onBack={() => setStep(1)} onCreate={create} />
        )}
      </div>
    </div>
  );
}
