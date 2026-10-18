import { FolderPlus, Search, Settings, Shield } from "lucide-react";
import { useState } from "react";
import { Link, NavLink, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Chat, Project } from "../api/types";
import { t } from "../lib/i18n";
import { useStore } from "../state/store";
import { NewProjectDialog } from "./NewProjectDialog";

function StateDot({ chat }: { chat: Chat | undefined }) {
  if (chat?.state === "running") return <span className="size-2 shrink-0 animate-pulse rounded-full bg-accent" />;
  if (chat?.state === "waiting") return <span className="size-2 shrink-0 rounded-full bg-warn" />;
  return <span className="size-2 shrink-0" />;
}

/** The projects a search shows: those whose name matches (a project is its one chat, W44). */
export function searchSidebar(projects: Project[], query: string): Project[] {
  const q = query.trim().toLowerCase();
  return q ? projects.filter((project) => project.name.toLowerCase().includes(q)) : projects;
}

export function Sidebar({ activeProject, onNavigate }: { activeProject?: string; onNavigate?: () => void }) {
  const { projects, chats, createProject, forgetProject, user } = useStore();
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState("");
  const shown = searchSidebar(projects, query);
  const navigate = useNavigate();
  return (
    <aside className="flex h-full w-72 max-w-[85vw] shrink-0 flex-col border-r border-line bg-panel">
      <div className="flex items-center justify-between px-4 py-3">
        <span className="text-lg font-semibold">{t("appName")}</span>
        <button type="button" title={t("newProject")} onClick={() => setCreating(true)}>
          <FolderPlus className="size-5" />
        </button>
      </div>
      {creating && (
        <NewProjectDialog
          isAdmin={user?.role === "admin"}
          appleApps={user?.apple_apps === true}
          create={createProject}
          forget={forgetProject}
          onClose={() => setCreating(false)}
          onCreated={(project) => {
            setCreating(false);
            onNavigate?.();
            navigate(`/p/${project.id}`);
          }}
        />
      )}
      <label className="mx-3 mb-2 flex items-center gap-2 rounded-md border border-line bg-card px-2 py-1">
        <Search className="size-4 shrink-0 text-muted" />
        <input
          type="search"
          className="min-w-0 flex-1 bg-transparent text-sm outline-none"
          placeholder={t("searchPlaceholder")}
          aria-label={t("searchPlaceholder")}
          value={query}
          onChange={(e) => setQuery(e.target.value)}
        />
      </label>
      <div className="px-4 pb-1 text-xs font-medium tracking-wide text-muted uppercase">{t("projects")}</div>
      <nav className="flex-1 space-y-1 overflow-y-auto px-2">
        {projects.length === 0 && <div className="px-2 py-2 text-sm text-muted">{t("noProjects")}</div>}
        {projects.length > 0 && shown.length === 0 && <div className="px-2 py-2 text-sm text-muted">{t("noMatches")}</div>}
        {shown.map((project) => (
          <NavLink
            key={project.id}
            to={`/p/${project.id}`}
            onClick={onNavigate}
            className={`flex items-center gap-2 rounded-md px-2 py-1.5 text-sm ${project.id === activeProject ? "bg-card font-medium" : "hover:bg-card"}`}
          >
            <StateDot chat={chats[project.id]} />
            <span className="truncate">{project.name}</span>
          </NavLink>
        ))}
      </nav>
      <div className="flex items-center gap-2 border-t border-line px-4 py-3 text-sm text-muted">
        <span className="min-w-0 flex-1 truncate">{user?.name}</span>
        <Link to="/settings" title={t("settings")} onClick={onNavigate} className="p-0.5">
          <Settings className="size-4" />
        </Link>
        {user?.role === "admin" && (
          <Link to="/admin" title={t("administration")} onClick={onNavigate} className="p-0.5">
            <Shield className="size-4" />
          </Link>
        )}
        <button
          type="button"
          className="underline"
          onClick={() => api.post("/api/auth/logout").finally(() => window.location.assign("/"))}
        >
          {t("signOut")}
        </button>
      </div>
    </aside>
  );
}
