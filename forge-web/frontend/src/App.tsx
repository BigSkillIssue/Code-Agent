import { Menu, X } from "lucide-react";
import { lazy, Suspense, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Outlet, Route, Routes, useLocation, useParams } from "react-router-dom";
import { api } from "./api/client";
import type { Chat } from "./api/types";
import { ChatView } from "./components/ChatView";
import { Logo, LogoMark } from "./components/Logo";
import { Sidebar } from "./components/Sidebar";
import { TrialNotice } from "./components/TrialNotice";
import { t } from "./lib/i18n";
import { AdminPage } from "./pages/Admin";
import { AppleListingPage } from "./pages/AppleListing";
import { GoLivePage } from "./pages/GoLive";
import { AppleReleasePage } from "./pages/AppleRelease";
import { AppleReviewPage } from "./pages/AppleReview";
import { AuthPages, type AuthConfig } from "./pages/Auth";
import { BillingPage } from "./pages/Billing";
import { SettingsPage } from "./pages/Settings";
import { useStore } from "./state/store";

// Loaded only when shown: visitors never load the app, people in the app never the start page.
const LandingPage = lazy(() => import("./pages/Landing"));
const OnboardingPage = lazy(() => import("./pages/Onboarding"));

function ErrorBar() {
  const { error, setError } = useStore();
  if (!error) return null;
  return (
    <div className="fixed right-4 bottom-4 z-50 flex max-w-md items-start gap-2 rounded-lg border border-bad bg-card p-3 text-sm text-bad shadow-lg" role="alert">
      <span className="flex-1">{error}</span>
      <button type="button" onClick={() => setError("")}>
        <X className="size-4" />
      </button>
    </div>
  );
}

function Empty() {
  return (
    <div className="flex flex-1 items-center justify-center p-8">
      <div className="ember-glow-center flex flex-col items-center gap-5 px-16 py-12 text-center">
        <LogoMark size={72} className="text-line-strong" />
        <p className="max-w-[26ch] font-display text-[28px] leading-tight font-bold text-muted">{t("pickProject")}</p>
      </div>
    </div>
  );
}

/** A project is its one chat (W44). */
export function ProjectPage() {
  const { projectId = "" } = useParams();
  const chat = useStore((s) => s.chats[projectId]);
  const { loadChat } = useStore();
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    if (!chat) loadChat(projectId).catch(() => setMissing(true));
  }, [chat, projectId, loadChat]);
  if (missing) return <Empty />;
  if (!chat) return <div className="flex-1" />;
  return <ChatView key={chat.id} chat={chat} />;
}

/** Links to a chat from before W44 (and the Apple pages' links) lead to its project. */
export function ChatLink() {
  const { chatId = "" } = useParams();
  const [projectId, setProjectId] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    api
      .get<Chat>(`/api/chats/${chatId}`)
      .then((found) => setProjectId(found.project_id))
      .catch(() => setMissing(true));
  }, [chatId]);
  if (missing) return <Empty />;
  return projectId ? <Navigate replace to={`/p/${projectId}`} /> : <div className="flex-1" />;
}

function Shell() {
  const { projectId } = useParams();
  const [menu, setMenu] = useState(false); // the sidebar on small screens
  return (
    <div className="flex h-full">
      {menu && <div className="fixed inset-0 z-30 bg-black/40 md:hidden" onClick={() => setMenu(false)} />}
      <div
        className={`fixed inset-y-0 left-0 z-40 transition-transform md:static md:translate-x-0 ${menu ? "translate-x-0" : "-translate-x-full"}`}
      >
        <Sidebar activeProject={projectId} onNavigate={() => setMenu(false)} />
      </div>
      <div className="flex min-w-0 flex-1 flex-col">
        <div className="ember-glow-top flex items-center gap-3 border-b border-line px-3 py-2 md:hidden">
          <button type="button" aria-label={t("menu")} onClick={() => setMenu(true)}>
            <Menu className="size-5" />
          </button>
          <Logo size={30} />
        </div>
        <TrialNotice />
        <div className="flex min-h-0 flex-1">
          <Outlet />
        </div>
      </div>
    </div>
  );
}

/** Signed out: the start page at /, signing in everywhere else (and where a sign-in came back). */
export function SignedOut({ config }: { config: AuthConfig }) {
  const location = useLocation();
  const returned = new URLSearchParams(location.search).has("auth_error");
  if (config.setup_needed || (location.pathname === "/" && returned)) return <AuthPages config={config} />;
  return (
    <Routes>
      <Route
        index
        element={
          <Suspense fallback={null}>
            <LandingPage config={config} />
          </Suspense>
        }
      />
      <Route path="*" element={<AuthPages config={config} />} />
    </Routes>
  );
}

export function App() {
  const { user, signedOut, init } = useStore();
  const [config, setConfig] = useState<AuthConfig | null>(null);
  useEffect(() => {
    void init();
  }, [init]);
  useEffect(() => {
    if (signedOut) api.get<AuthConfig>("/api/auth/config").then(setConfig).catch(() => undefined);
  }, [signedOut]);
  if (signedOut) {
    return config ? (
      <BrowserRouter>
        <SignedOut config={config} />
      </BrowserRouter>
    ) : null;
  }
  if (!user) return null;
  return (
    <BrowserRouter>
      <Routes>
        <Route
          path="willkommen"
          element={
            <Suspense fallback={null}>
              <OnboardingPage />
            </Suspense>
          }
        />
        <Route path="anmelden" element={<Navigate replace to="/" />} />
        <Route element={<Shell />}>
          <Route index element={user.onboarding ? <Navigate replace to="/willkommen" /> : <Empty />} />
          <Route path="p/:projectId" element={<ProjectPage />} />
          <Route path="p/:projectId/apple" element={<AppleReviewPage />} />
          <Route path="p/:projectId/apple/release" element={<AppleReleasePage />} />
          <Route path="p/:projectId/apple/listing" element={<AppleListingPage />} />
          <Route path="p/:projectId/live" element={<GoLivePage />} />
          <Route path="c/:chatId" element={<ChatLink />} />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="billing" element={<BillingPage />} />
          <Route path="admin" element={<AdminPage />} />
          <Route path="*" element={<Empty />} />
        </Route>
      </Routes>
      <ErrorBar />
    </BrowserRouter>
  );
}
