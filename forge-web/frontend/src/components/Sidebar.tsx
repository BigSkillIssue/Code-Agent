import { ChevronDown, ChevronRight, FolderPlus, MessageSquarePlus, Search } from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Chat, Project } from "../api/types";
import { t } from "../lib/i18n";
import { useStore } from "../state/store";
import { NewProjectDialog } from "./NewProjectDialog";

function StateDot({ chat }: { chat: Chat }) {
  if (chat.state === "running") return <span className="size-2 shrink-0 animate-pulse rounded-full bg-accent" />;
  if (chat.state === "waiting") return <span className="size-2 shrink-0 rounded-full bg-warn" />;
  return <span className="size-2 shrink-0" />;
}

/** What a search shows: projects whose name matches (with all chats) or that have matching chats. */
export function searchSidebar(
  projects: Project[],
  chats: Record<string, Chat[] | undefined>,
  query: string,
): { project: Project; chats: Chat[] | undefined }[] {
  const q = query.trim().toLowerCase();
  if (!q) return projects.map((project) => ({ project, chats: chats[project.id] }));
  return projects.flatMap((project) => {
    const all = chats[project.id] ?? [];
    if (project.name.toLowerCase().includes(q)) return [{ project, chats: all }];
    const found = all.filter((chat) => chat.title.toLowerCase().includes(q));
    return found.length > 0 ? [{ project, chats: found }] : [];
  });
}

interface ProjectItemProps {
  project: Project;
  chats: Chat[] | undefined;
  activeChat?: string;
  searching: boolean;
  onNavigate?: () => void;
}

function ProjectItem({ project, chats, activeChat, searching, onNavigate }: ProjectItemProps) {
  const { loadChats, createChat, setError } = useStore();
  const [folded, setFolded] = useState(false);
  const open = searching || !folded;
  const navigate = useNavigate();
  useEffect(() => {
    if (open && chats === undefined) loadChats(project.id).catch((err) => setError(String(err)));
  }, [open, chats, project.id, loadChats, setError]);
  const newChat = async () => {
    try {
      const chat = await createChat(project.id);
      onNavigate?.();
      navigate(`/c/${chat.id}`);
    } catch (err) {
      setError(String(err));
    }
  };
  return (
    <div>
      <div className="group flex items-center gap-1 rounded-md px-2 py-1 hover:bg-card">
        <button type="button" className="flex min-w-0 flex-1 items-center gap-1 text-left" onClick={() => setFolded(!folded)}>
          {open ? <ChevronDown className="size-4 shrink-0" /> : <ChevronRight className="size-4 shrink-0" />}
          <span className="truncate font-medium">{project.name}</span>
        </button>
        {project.role !== "viewer" && (
          <button type="button" title={t("newChat")} className="opacity-60 group-hover:opacity-100" onClick={() => void newChat()}>
            <MessageSquarePlus className="size-4" />
          </button>
        )}
      </div>
      {open && (
        <div className="ml-4 border-l border-line pl-2">
          {(chats ?? []).length === 0 && <div className="px-2 py-1 text-xs text-muted">{t("noChats")}</div>}
          {(chats ?? []).map((chat) => (
            <NavLink
              key={chat.id}
              to={`/c/${chat.id}`}
              onClick={onNavigate}
              className={`flex items-center gap-2 rounded-md px-2 py-1 text-sm ${chat.id === activeChat ? "bg-card font-medium" : "hover:bg-card"}`}
            >
              <StateDot chat={chat} />
              <span className="truncate">{chat.title}</span>
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

export function Sidebar({ activeChat, onNavigate }: { activeChat?: string; onNavigate?: () => void }) {
  const { projects, chats, createProject, forgetProject, user } = useStore();
  const [creating, setCreating] = useState(false);
  const [query, setQuery] = useState("");
  const shown = searchSidebar(projects, chats, query);
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
        {shown.map(({ project, chats: visible }) => (
          <ProjectItem
            key={project.id}
            project={project}
            chats={visible}
            activeChat={activeChat}
            searching={query.trim() !== ""}
            onNavigate={onNavigate}
          />
        ))}
      </nav>
      <div className="flex items-center gap-2 border-t border-line px-4 py-3 text-sm text-muted">
        <span className="min-w-0 flex-1 truncate">{user?.name}</span>
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
