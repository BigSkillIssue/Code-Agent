import { ChevronDown, ChevronRight, FolderPlus, MessageSquarePlus } from "lucide-react";
import { useEffect, useState } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { api } from "../api/client";
import type { Chat, Project } from "../api/types";
import { t } from "../lib/i18n";
import { useStore } from "../state/store";

function StateDot({ chat }: { chat: Chat }) {
  if (chat.state === "running") return <span className="size-2 shrink-0 animate-pulse rounded-full bg-accent" />;
  if (chat.state === "waiting") return <span className="size-2 shrink-0 rounded-full bg-warn" />;
  return <span className="size-2 shrink-0" />;
}

function ProjectItem({ project, activeChat }: { project: Project; activeChat?: string }) {
  const chats = useStore((s) => s.chats[project.id]);
  const { loadChats, createChat, setError } = useStore();
  const [open, setOpen] = useState(true);
  const navigate = useNavigate();
  useEffect(() => {
    if (open && chats === undefined) loadChats(project.id).catch((err) => setError(String(err)));
  }, [open, chats, project.id, loadChats, setError]);
  const newChat = async () => {
    try {
      const chat = await createChat(project.id);
      navigate(`/c/${chat.id}`);
    } catch (err) {
      setError(String(err));
    }
  };
  return (
    <div>
      <div className="group flex items-center gap-1 rounded-md px-2 py-1 hover:bg-card">
        <button type="button" className="flex min-w-0 flex-1 items-center gap-1 text-left" onClick={() => setOpen(!open)}>
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

export function Sidebar({ activeChat }: { activeChat?: string }) {
  const { projects, createProject, user, setError } = useStore();
  const [naming, setNaming] = useState(false);
  const [name, setName] = useState("");
  const navigate = useNavigate();
  const create = async () => {
    if (!name.trim()) return;
    try {
      const project = await createProject(name.trim());
      setNaming(false);
      setName("");
      navigate(`/p/${project.id}`);
    } catch (err) {
      setError(String(err));
    }
  };
  return (
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-line bg-panel">
      <div className="flex items-center justify-between px-4 py-3">
        <span className="text-lg font-semibold">{t("appName")}</span>
        <button type="button" title={t("newProject")} onClick={() => setNaming(true)}>
          <FolderPlus className="size-5" />
        </button>
      </div>
      {naming && (
        <form
          className="mx-3 mb-2 flex gap-1"
          onSubmit={(e) => {
            e.preventDefault();
            void create();
          }}
        >
          <input
            autoFocus
            className="min-w-0 flex-1 rounded-md border border-line bg-card px-2 py-1 text-sm"
            placeholder={t("projectName")}
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <button type="submit" className="rounded-md bg-accent px-2 text-sm text-on-accent">
            {t("create")}
          </button>
        </form>
      )}
      <div className="px-4 pb-1 text-xs font-medium tracking-wide text-muted uppercase">{t("projects")}</div>
      <nav className="flex-1 space-y-1 overflow-y-auto px-2">
        {projects.length === 0 && <div className="px-2 py-2 text-sm text-muted">{t("noProjects")}</div>}
        {projects.map((project) => (
          <ProjectItem key={project.id} project={project} activeChat={activeChat} />
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
