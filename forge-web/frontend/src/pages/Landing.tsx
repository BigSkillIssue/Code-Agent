// The start page for visitors (W72): what Forge does, how, what people build with it, the prices
// from the plans (`/api/public/plans`), questions and the legal links. The hero is the forge of
// the sign-in page: the same dark side, its fire low and hot, the spark struck once. Nothing here
// sets a cookie, tracks anyone or loads anything from another host.

import { Check, ChevronDown, Globe, MonitorDown, Smartphone } from "lucide-react";
import { type ReactNode, useEffect, useId, useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api/client";
import type { Offer } from "../api/billing";
import { Logo } from "../components/Logo";
import { t, type TextKey } from "../lib/i18n";
import type { AuthConfig } from "./Auth";
import { features, price } from "./Plans";

export interface PublicPlans {
  plans: Offer[];
  trial: { days: number; credit_usd: number } | null;
}

const SIGN_IN = "/anmelden";
const STEPS: [TextKey, TextKey][] = [
  ["landingStep1", "landingStep1Text"],
  ["landingStep2", "landingStep2Text"],
  ["landingStep3", "landingStep3Text"],
];
const EXAMPLES: { icon: ReactNode; kind: TextKey; ask: TextKey; gets: TextKey }[] = [
  { icon: <Globe className="size-6" aria-hidden />, kind: "landingExampleWeb", ask: "landingExampleWebAsk", gets: "landingExampleWebGets" },
  { icon: <Smartphone className="size-6" aria-hidden />, kind: "landingExampleMobile", ask: "landingExampleMobileAsk", gets: "landingExampleMobileGets" },
  { icon: <MonitorDown className="size-6" aria-hidden />, kind: "landingExampleWindows", ask: "landingExampleWindowsAsk", gets: "landingExampleWindowsGets" },
];
const FAQ: [TextKey, TextKey][] = [
  ["landingFaq1", "landingFaq1Answer"],
  ["landingFaq2", "landingFaq2Answer"],
  ["landingFaq3", "landingFaq3Answer"],
  ["landingFaq4", "landingFaq4Answer"],
];
const MADE: TextKey[] = ["landingHeroMade1", "landingHeroMade2", "landingHeroMade3", "landingHeroMade4"];

const usd = (amount: number) => price(Math.round(amount * 100), "usd");

/** Whether a visitor can make an account here without an invite. */
function canSignUp(config: AuthConfig): boolean {
  return config.signup !== "invite" && (config.passwords || config.providers.length > 0);
}

function Cta({ config, trial }: { config: AuthConfig; trial: PublicPlans["trial"] }) {
  const open = canSignUp(config);
  return (
    <div className="flex flex-wrap items-center gap-3">
      <Link
        to={open ? "/signup" : SIGN_IN}
        className="inline-flex h-12 items-center rounded-md bg-[var(--forge-spark)] px-6 text-[17px] font-semibold text-[var(--forge-bg)] hover:brightness-110"
      >
        {open ? t(trial ? "landingStart" : "landingStartPlain") : t("landingSignIn")}
      </Link>
      {open && (
        <Link to={SIGN_IN} className="inline-flex h-12 items-center rounded-md border border-[rgb(243_234_225/0.35)] px-5 text-[17px] hover:border-[var(--forge-text)]">
          {t("landingSignIn")}
        </Link>
      )}
    </div>
  );
}

function Hero({ config, offer }: { config: AuthConfig; offer: PublicPlans | null }) {
  const trial = offer?.trial ?? null;
  return (
    <header className="forge-side relative overflow-hidden">
      <nav className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 pt-5 sm:px-8">
        <Logo size={34} />
        <Link to={SIGN_IN} className="rounded-md px-3 py-2 text-[15px] font-medium hover:bg-[rgb(243_234_225/0.1)]">
          {t("landingSignIn")}
        </Link>
      </nav>
      <div className="mx-auto grid max-w-6xl gap-10 px-4 pt-14 pb-20 sm:px-8 md:pt-20 md:pb-28 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)] lg:items-end">
        <div className="space-y-7">
          <h1 className="landing-headline">
            <span className="block">{t("landingHeroLead")}</span>
            <span className="landing-headline-rest">{t("landingHeroRest")}</span>
          </h1>
          <p className="max-w-[56ch] text-[17px] leading-relaxed text-[rgb(243_234_225/0.86)]">{t("landingHeroText")}</p>
          <Cta config={config} trial={trial} />
          {trial && canSignUp(config) && (
            <p className="text-sm text-[rgb(243_234_225/0.75)]">
              {t("landingTrialNote", { days: String(trial.days), credit: usd(trial.credit_usd) })}
            </p>
          )}
        </div>
        <Workpiece />
      </div>
    </header>
  );
}

/** What comes out of one sentence: the task, and what Forge made from it. */
function Workpiece() {
  return (
    <figure className="rounded-xl border border-[rgb(243_234_225/0.16)] bg-[rgb(20_17_15/0.72)] p-5 shadow-[var(--shadow-4)] backdrop-blur-sm sm:p-6">
      <blockquote className="text-[17px] leading-snug">„{t("landingHeroAsk")}“</blockquote>
      <ul className="mt-5 space-y-2.5 border-t border-[rgb(243_234_225/0.14)] pt-4 text-[15px]">
        {MADE.map((key) => (
          <li key={key} className="flex items-center gap-2.5">
            <Check className="size-4 shrink-0 text-[var(--forge-spark)]" aria-hidden />
            {t(key)}
          </li>
        ))}
      </ul>
    </figure>
  );
}

function Steps() {
  return (
    <section className="mx-auto max-w-6xl px-4 py-16 sm:px-8 md:py-24" aria-labelledby="steps">
      <h2 id="steps" className="text-[40px] md:text-[56px]">{t("landingStepsTitle")}</h2>
      <ol className="mt-10 grid gap-10 md:grid-cols-3 md:gap-8">
        {STEPS.map(([title, text], index) => (
          <li key={title} className="space-y-3">
            <span className="landing-numeral" aria-hidden>{index + 1}</span>
            <h3 className="font-display text-[28px] leading-none font-bold">{t(title)}</h3>
            <p className="max-w-[40ch] text-muted">{t(text)}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}

function Examples() {
  return (
    <section className="bg-panel" aria-labelledby="examples">
      <div className="mx-auto max-w-6xl px-4 py-16 sm:px-8 md:py-24">
        <h2 id="examples" className="text-[40px] md:text-[56px]">{t("landingExamplesTitle")}</h2>
        <ul className="mt-10 space-y-4">
          {EXAMPLES.map((example) => (
            <li key={example.kind} className="grid gap-3 rounded-xl bg-card p-5 shadow-sm md:grid-cols-[14rem_minmax(0,1fr)_16rem] md:items-center md:gap-8 md:p-7">
              <p className="flex items-start gap-3 font-display text-[26px] leading-none font-bold text-accent">
                {example.icon}
                {t(example.kind)}
              </p>
              <p className="text-[18px] leading-snug">„{t(example.ask)}“</p>
              <p className="text-sm text-muted">{t(example.gets)}</p>
            </li>
          ))}
        </ul>
      </div>
    </section>
  );
}

function PlanColumn({ plan }: { plan: Offer }) {
  const month = plan.amounts.month ?? (plan.amounts.year ? Math.round(plan.amounts.year / 12) : 0);
  return (
    <article data-testid="landing-plan" className="flex flex-col rounded-xl border border-line bg-card p-6 shadow-sm">
      <h3 className="font-display text-[30px] leading-none font-bold">{plan.name}</h3>
      {plan.description && <p className="mt-2 text-sm text-muted">{plan.description}</p>}
      <p className="mt-5">
        <span className="font-display text-[44px] leading-none font-extrabold">{price(month, plan.currency)}</span>
        <span className="text-muted"> {t("landingPerMonth")}</span>
      </p>
      <p className="min-h-5 text-sm text-muted">
        {plan.amounts.month && plan.amounts.year ? t("landingOrYearly", { price: price(plan.amounts.year, plan.currency) }) : ""}
      </p>
      <ul className="mt-5 space-y-2 text-sm">
        {features(plan.limits).map((line) => (
          <li key={line} className="flex items-start gap-2">
            <Check className="mt-0.5 size-4 shrink-0 text-accent" aria-hidden />
            <span>{line}</span>
          </li>
        ))}
      </ul>
    </article>
  );
}

function Prices({ offer }: { offer: PublicPlans | null }) {
  const trial = offer?.trial ?? null;
  const plans = offer?.plans ?? [];
  return (
    <section className="mx-auto max-w-6xl px-4 py-16 sm:px-8 md:py-24" aria-labelledby="prices" data-testid="landing-prices">
      <h2 id="prices" className="text-[40px] md:text-[56px]">{t("landingPricesTitle")}</h2>
      <p className="mt-2 max-w-[60ch] text-muted">{t("landingPricesText")}</p>
      {trial && (
        <div className="ember-glow-top mt-8 rounded-xl border border-accent bg-accent-soft p-6">
          <h3 className="font-display text-[30px] leading-none font-bold">{t("landingTrialTitle")}</h3>
          <p className="mt-2 max-w-[60ch]">{t("landingTrialBody", { days: String(trial.days), credit: usd(trial.credit_usd) })}</p>
        </div>
      )}
      {offer && plans.length === 0 && <p className="mt-6 text-muted">{t("landingNoPlans")}</p>}
      {plans.length > 0 && (
        <div className={`mt-6 grid gap-4 ${plans.length > 1 ? "md:grid-cols-2" : ""} ${plans.length > 2 ? "lg:grid-cols-3" : ""}`}>
          {plans.map((plan) => (
            <PlanColumn key={plan.id} plan={plan} />
          ))}
        </div>
      )}
    </section>
  );
}

function Question({ question, answer }: { question: TextKey; answer: TextKey }) {
  const [open, setOpen] = useState(false);
  const panel = useId();
  return (
    <li className="border-b border-line">
      <h3>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={panel}
          onClick={() => setOpen(!open)}
          className="flex w-full items-center justify-between gap-4 py-5 text-left text-[18px] font-semibold"
        >
          {t(question)}
          <ChevronDown className={`size-5 shrink-0 text-muted transition-transform ${open ? "rotate-180" : ""}`} aria-hidden />
        </button>
      </h3>
      {open && (
        <p id={panel} className="max-w-[65ch] pb-5 text-muted">
          {t(answer)}
        </p>
      )}
    </li>
  );
}

function Questions() {
  return (
    <section className="mx-auto max-w-3xl px-4 pb-20 sm:px-8 md:pb-28" aria-labelledby="faq">
      <h2 id="faq" className="text-[40px] md:text-[56px]">{t("landingFaqTitle")}</h2>
      <ul className="mt-6 border-t border-line">
        {FAQ.map(([question, answer]) => (
          <Question key={question} question={question} answer={answer} />
        ))}
      </ul>
    </section>
  );
}

function Footer() {
  const links: [string, TextKey][] = [
    ["/impressum", "landingImprint"],
    ["/rechtstexte/privacy", "landingPrivacy"],
    ["/rechtstexte/terms", "landingTerms"],
    ["/kuendigen", "landingCancel"],
  ];
  return (
    <footer className="border-t border-line bg-[var(--forge-bg)] text-[var(--forge-text)]">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-10 sm:px-8 md:flex-row md:items-center md:justify-between">
        <Logo size={28} />
        <ul className="flex flex-wrap gap-x-6 gap-y-2 text-sm">
          {links.map(([href, label]) => (
            <li key={href}>
              <a className="underline-offset-4 hover:underline" href={href}>
                {t(label)}
              </a>
            </li>
          ))}
        </ul>
      </div>
    </footer>
  );
}

/** The whole start page; the sign-in itself is at /anmelden. */
export default function LandingPage({ config }: { config: AuthConfig }) {
  const [offer, setOffer] = useState<PublicPlans | null>(null);
  useEffect(() => {
    api
      .get<PublicPlans>("/api/public/plans")
      .then(setOffer)
      .catch(() => setOffer({ plans: [], trial: null }));
  }, []);
  return (
    <div className="min-h-full bg-bg" data-testid="landing">
      <Hero config={config} offer={offer} />
      <main>
        <Steps />
        <Examples />
        <Prices offer={offer} />
        <Questions />
      </main>
      <Footer />
    </div>
  );
}
