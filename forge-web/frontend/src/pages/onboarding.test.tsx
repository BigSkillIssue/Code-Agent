import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Composer } from "../components/Composer";
import { GuideBar } from "../components/Guide";
import { BUILTIN_COMMANDS } from "../lib/completion";
import { useStore } from "../state/store";
import OnboardingPage, { sourceFor } from "./Onboarding";

afterEach(cleanup);

function Arrived() {
  const location = useLocation();
  return <p data-testid="arrived">{`${location.pathname} ${JSON.stringify(location.state)}`}</p>;
}

function start(sources = ["empty", "app"]) {
  const createProject = vi.fn(async () => ({ id: "p1", name: "x", role: "owner" as const, source: "app", created_at: 0, updated_at: 0 }));
  const finishOnboarding = vi.fn(async () => undefined);
  useStore.setState({
    user: { id: "u1", email: "maria@example.com", name: "Maria", role: "member", project_sources: sources, onboarding: true },
    createProject,
    finishOnboarding,
  });
  render(
    <MemoryRouter initialEntries={["/willkommen"]}>
      <Routes>
        <Route path="/willkommen" element={<OnboardingPage />} />
        <Route path="*" element={<Arrived />} />
      </Routes>
    </MemoryRouter>,
  );
  return { createProject, finishOnboarding };
}

describe("the guided start", () => {
  it("goes from the goal to a demo project and its chat with a first task", async () => {
    const { createProject, finishOnboarding } = start();
    expect(screen.getByText(/^(Step 1 of 4|Schritt 1 von 4)/)).toBeTruthy();
    fireEvent.click(screen.getByRole("radio", { name: /Phone app|Handy-App/ }));
    fireEvent.click(screen.getByRole("button", { name: /^(Next|Weiter)$/ }));
    expect(screen.getByText(/^(Step 2 of 4|Schritt 2 von 4)/)).toBeTruthy();
    const name = screen.getByRole("textbox") as HTMLInputElement;
    expect(name.value).toMatch(/My phone app|Meine Handy-App/);
    fireEvent.click(screen.getByRole("button", { name: /^(Create the project|Projekt anlegen)$/ }));
    const arrived = await screen.findByTestId("arrived");
    expect(createProject).toHaveBeenCalledWith({ name: name.value, source: "app" });
    expect(finishOnboarding).toHaveBeenCalledOnce(); // kept on the server: not shown again
    expect(arrived.textContent).toMatch(/^\/p\/p1 /);
    expect(arrived.textContent).toMatch(/iPhone/); // the suggested first task travels along
  });

  it("can be skipped at once", async () => {
    const { createProject, finishOnboarding } = start();
    fireEvent.click(screen.getByRole("button", { name: /^(Skip|Überspringen)$/ }));
    await waitFor(() => expect(finishOnboarding).toHaveBeenCalledOnce());
    expect((await screen.findByTestId("arrived")).textContent).toMatch(/^\/ /);
    expect(createProject).not.toHaveBeenCalled();
  });

  it("starts from the template that fits the goal and the server", () => {
    expect(sourceFor("web", ["empty", "app"])).toBe("app");
    expect(sourceFor("windows", undefined)).toBe("app");
    expect(sourceFor("try", ["empty", "app"])).toBe("empty");
    expect(sourceFor("mobile", ["empty"])).toBe("empty"); // no app template on this server
  });

  it("writes the first task into the box without sending it, then points at the preview", () => {
    const onSend = vi.fn();
    render(<Composer running={false} onSend={onSend} onStop={vi.fn()} commands={BUILTIN_COMMANDS} initialText="Baue eine Einkaufsliste" />);
    expect((screen.getByRole("textbox") as HTMLTextAreaElement).value).toBe("Baue eine Einkaufsliste");
    expect(onSend).not.toHaveBeenCalled();
    cleanup();
    const onNext = vi.fn();
    const onClose = vi.fn();
    const { rerender } = render(<GuideBar step={3} onNext={onNext} onClose={onClose} />);
    expect(screen.getByText(/^(Step 3 of 4|Schritt 3 von 4)/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^(Next|Weiter)$/ }));
    expect(onNext).toHaveBeenCalledOnce();
    rerender(<GuideBar step={4} onNext={onNext} onClose={onClose} />);
    expect(screen.getByText(/preview|Vorschau/i, { selector: "p:not([aria-live])" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: /^(Done|Fertig)$/ }));
    expect(onClose).toHaveBeenCalledOnce();
  });
});
