// The admin's agents (W53): whether people pick a chat's model, which models each role and agent
// uses per plan, and the agents Forge's lead has (built-ins changed, the admin's own added).

import { useCallback, useState } from "react";
import { admin, type AgentEntry, type AgentsView } from "../api/account";
import { t } from "../lib/i18n";
import { Select } from "../components/Select";
import { Button, Section, TextInput, useAction, useLoaded } from "./parts";

const NAME = /^[a-z0-9-]{1,32}$/;

/** "a/b, c/d" -> ["a/b", "c/d"]. */
export function chainOf(text: string): string[] {
  return text
    .split(",")
    .map((part) => part.trim())
    .filter(Boolean);
}

export function AgentsTab() {
  const [view, reload] = useLoaded(useCallback(() => admin.agents(), []));
  if (!view) return null;
  return (
    <>
      <AgentsForm key={JSON.stringify(view)} view={view} onSaved={reload} />
      <GuidelinesForm />
    </>
  );
}

/** The stores' guidelines the store-review agent loads (W58). */
function GuidelinesForm() {
  const [saved, reload] = useLoaded(useCallback(() => admin.storeGuidelines(), []));
  const [texts, setTexts] = useState<{ apple: string; google_play: string } | null>(null);
  const act = useAction();
  if (!saved) return null;
  const shown = texts ?? { apple: saved.apple ?? "", google_play: saved.google_play ?? "" };
  const area = (key: "apple" | "google_play", label: string) => (
    <label className="block">
      <span className="mb-1.5 block text-sm font-medium">{label}</span>
      <textarea
        className="h-40 w-full rounded-md border border-line bg-card px-2.5 py-2 font-mono text-xs focus:border-accent focus:outline-none"
        value={shown[key]}
        onChange={(e) => setTexts({ ...shown, [key]: e.target.value })}
      />
    </label>
  );
  return (
    <Section title={t("storeGuidelines")} hint={t("storeGuidelinesHint")}>
      {area("apple", t("guidelinesApple"))}
      {area("google_play", t("guidelinesPlay"))}
      <Button kind="primary" onClick={() => void act(() => admin.saveStoreGuidelines(shown)).then(reload)}>
        {t("save")}
      </Button>
    </Section>
  );
}

function AgentsForm({ view, onSaved }: { view: AgentsView; onSaved: () => Promise<void> }) {
  const [choice, setChoice] = useState(view.user_model_choice);
  const [tier, setTier] = useState("default");
  const [roles, setRoles] = useState(view.roles);
  const [parallel, setParallel] = useState(view.parallel ?? {});
  const [agents, setAgents] = useState<AgentEntry[]>(() => mergedAgents(view));
  const [newName, setNewName] = useState("");
  const act = useAction();
  const rows = [...view.role_groups, ...agents.map((a) => a.name)];
  const chain = (row: string) => (roles[tier]?.[row] ?? []).join(", ");
  const setChain = (row: string, text: string) =>
    setRoles((all) => ({ ...all, [tier]: { ...(all[tier] ?? {}), [row]: chainOf(text) } }));
  const change = (name: string, patch: Partial<AgentEntry>) =>
    setAgents((all) => all.map((a) => (a.name === name ? { ...a, ...patch } : a)));
  const save = async () => {
    const cleaned = Object.fromEntries(
      Object.entries(roles).map(([key, row]) => [key, Object.fromEntries(Object.entries(row).filter(([, c]) => c.length > 0))]),
    );
    if (await act(() => admin.saveAgents({ user_model_choice: choice, roles: cleaned, agents: agents.map(stored(view)), parallel }))) await onSaved();
  };
  return (
    <div data-testid="agents-settings">
      <Section title={t("modelChoice")} hint={t("modelChoiceHint")}>
        <label className="flex items-center gap-2">
          <input type="checkbox" checked={choice} onChange={(e) => setChoice(e.target.checked)} />
          {t("usersPickModels")}
        </label>
      </Section>
      <Section title={t("modelsPerRole")} hint={t("modelsPerRoleHint")}>
        <Select
          label={t("forPlan")}
          className="max-w-xs"
          value={tier}
          onChange={setTier}
          options={view.tiers.map((id) => ({ value: id, label: id === "default" ? t("everyoneElse") : id }))}
        />
        <div className="max-w-xs">
          <TextInput
            label={t("agentsAtOnce")}
            value={parallel[tier] ? String(parallel[tier]) : ""}
            placeholder="4"
            onChange={(v) =>
              setParallel((all) => {
                const next = { ...all };
                const n = Number(v);
                if (Number.isInteger(n) && n >= 1 && n <= 16) next[tier] = n;
                else delete next[tier];
                return next;
              })
            }
          />
        </div>
        <div className="grid gap-3 sm:grid-cols-2">
          {rows.map((row) => (
            <TextInput key={`${tier}-${row}`} label={row} value={chain(row)} placeholder="anthropic/claude-sonnet, openai/gpt-5" onChange={(v) => setChain(row, v)} />
          ))}
        </div>
      </Section>
      <Section title={t("agentsTitle")} hint={t("agentsHint")}>
        {agents.map((agent) => (
          <details key={agent.name} className="border-t border-line py-3 first:border-t-0">
            <summary className="flex cursor-pointer items-center gap-2">
              <span className="font-medium">{agent.name}</span>
              <span className="text-muted">{t(`policy_${agent.policy}`)}</span>
            </summary>
            <div className="mt-3 space-y-3">
              <TextInput label={t("agentDescription")} value={agent.description} onChange={(v) => change(agent.name, { description: v })} />
              <div className="grid gap-3 sm:grid-cols-2">
                <Select
                  label={t("policy")}
                  value={agent.policy}
                  onChange={(v) => change(agent.name, { policy: v as AgentEntry["policy"] })}
                  options={(["required", "recommended", "off"] as const).map((p) => ({ value: p, label: t(`policy_${p}`) }))}
                />
                <Select
                  label={t("appliesTo")}
                  value={agent.applies_to}
                  onChange={(v) => change(agent.name, { applies_to: v as AgentEntry["applies_to"] })}
                  options={(["always", "product", "ui"] as const).map((p) => ({ value: p, label: t(`applies_${p}`) }))}
                />
              </div>
              <label className="block">
                <span className="mb-1.5 block text-sm font-medium">{t("agentPrompt")}</span>
                <textarea
                  className="h-48 w-full rounded-md border border-line bg-card px-2.5 py-2 font-mono text-xs focus:border-accent focus:outline-none"
                  value={agent.prompt}
                  onChange={(e) => change(agent.name, { prompt: e.target.value })}
                />
              </label>
            </div>
          </details>
        ))}
        <div className="flex items-end gap-2">
          <div className="w-56">
            <TextInput label={t("newAgent")} value={newName} placeholder="seo" onChange={setNewName} />
          </div>
          <Button
            disabled={!NAME.test(newName) || agents.some((a) => a.name === newName)}
            onClick={() => {
              setAgents((all) => [...all, { name: newName, description: "", prompt: "", tools: [], policy: "recommended", applies_to: "always" }]);
              setNewName("");
            }}
          >
            {t("addAgent")}
          </Button>
        </div>
      </Section>
      <Button kind="primary" onClick={() => void save()}>
        {t("save")}
      </Button>
    </div>
  );
}

/** The built-ins (with the admin's changes) and the admin's own agents, for editing. */
export function mergedAgents(view: AgentsView): AgentEntry[] {
  const own = new Map(view.agents.map((a) => [a.name, a]));
  const builtins = view.builtin.map((b) => {
    const changed = own.get(b.name);
    return {
      name: b.name,
      description: changed?.description || b.description,
      prompt: changed?.prompt || b.prompt,
      tools: changed?.tools ?? [],
      policy: changed?.policy ?? b.policy,
      applies_to: changed?.applies_to ?? b.applies_to,
    };
  });
  const extra = view.agents.filter((a) => !view.builtin.some((b) => b.name === a.name));
  return [...builtins, ...extra];
}

/** What is saved: a built-in keeps an empty prompt while its text is unchanged, so later
 * versions of Forge Web's text reach it. */
function stored(view: AgentsView) {
  return (agent: AgentEntry): AgentEntry => {
    const builtin = view.builtin.find((b) => b.name === agent.name);
    if (!builtin) return agent;
    return {
      ...agent,
      prompt: agent.prompt === builtin.prompt ? "" : agent.prompt,
      description: agent.description === builtin.description ? "" : agent.description,
    };
  };
}
