// The app's state: who is signed in, projects, each project's one chat (W44), and each open
// chat's items and live state.
// Chat items only ever come from the WebSocket, in order; this store appends them.

import { create } from "zustand";
import { api, ApiError } from "../api/client";
import { ForgeSocket } from "../api/socket";
import type {
  Attachment,
  Chat,
  ChatItem,
  LiveState,
  Model,
  Project,
  ServerMessage,
  StoredItem,
  User,
} from "../api/types";

export interface NewProject {
  name: string;
  source?: "empty" | "git" | "zip" | "folder" | "apple" | "app";
  url?: string;
  folder?: string;
  bundle_id?: string; // apple
}

export interface ChatData {
  items: StoredItem[];
  lastSeq: number;
  live: LiveState;
  subscribed: boolean;
}

const emptyLive = (): LiveState => ({ streaming: {}, outputs: {}, pending: [] });
const emptyChat = (): ChatData => ({ items: [], lastSeq: 0, live: emptyLive(), subscribed: false });

interface Store {
  user: User | null;
  signedOut: boolean;
  projects: Project[];
  chats: Record<string, Chat>; // project id -> its chat
  chatData: Record<string, ChatData>;
  error: string;
  socket: ForgeSocket | null;
  models: Model[] | null;
  init: () => Promise<void>;
  loadModels: () => Promise<void>;
  searchFiles: (projectId: string, query: string) => Promise<string[]>;
  loadChat: (projectId: string) => Promise<Chat>;
  createProject: (body: NewProject) => Promise<Project>;
  forgetProject: (projectId: string) => void;
  updateChat: (chat: Chat, changes: Partial<Pick<Chat, "title" | "mode" | "model" | "effort">>) => Promise<void>;
  clearChat: (chat: Chat) => Promise<void>;
  openChat: (chatId: string) => void;
  closeChat: (chatId: string) => void;
  send: (chatId: string, text: string, files?: Attachment[]) => Promise<void>;
  upload: (chatId: string, file: File) => Promise<Attachment>;
  answer: (chatId: string, requestId: string, answer: Record<string, unknown>) => Promise<boolean>;
  cancel: (chatId: string) => Promise<void>;
  handle: (message: ServerMessage) => void;
  setError: (error: string) => void;
  setUserName: (name: string) => void;
}

/** A chat's live state after one live-only item (streaming text, command output). */
export function applyLive(live: LiveState, item: ChatItem): LiveState {
  if (item.type !== "event") return live;
  const event = item.event as Record<string, unknown>;
  if (event.kind === "model_delta") {
    const agent = String(event.agent_id ?? "main");
    const text = (live.streaming[agent] ?? "") + String(event.text ?? "");
    return { ...live, streaming: { ...live.streaming, [agent]: text } };
  }
  if (event.kind === "tool_output") {
    const callId = String(event.call_id);
    const lines = [...(live.outputs[callId] ?? []), String(event.text ?? "")].slice(-500);
    return { ...live, outputs: { ...live.outputs, [callId]: lines } };
  }
  return live;
}

/** A chat's live state after a stored item ended what was streaming. */
export function settleLive(live: LiveState, item: ChatItem): LiveState {
  if (item.type === "turn" || item.type === "worker_exited") return emptyLive();
  if (item.type !== "event") return live;
  const event = item.event as Record<string, unknown>;
  if (event.kind === "model_done") {
    const streaming = { ...live.streaming };
    delete streaming[String(event.agent_id ?? "main")];
    return { ...live, streaming };
  }
  if (event.kind === "tool_finished") {
    const outputs = { ...live.outputs };
    delete outputs[String((event.result as { call_id?: string })?.call_id)];
    return { ...live, outputs };
  }
  return live;
}

/** A chat's data after one message from the server (items are taken once, in order). */
export function applyMessage(data: ChatData, message: ServerMessage): ChatData {
  switch (message.type) {
    case "item":
      if (message.seq <= data.lastSeq) return data;
      return {
        ...data,
        items: [...data.items, { seq: message.seq, item: message.item }],
        lastSeq: message.seq,
        live: settleLive(data.live, message.item),
      };
    case "live":
      return { ...data, live: applyLive(data.live, message.item) };
    case "subscribed":
      return { ...data, subscribed: true, live: { ...emptyLive(), ...message.live } };
    default:
      return data;
  }
}

export const useStore = create<Store>((set, get) => ({
  user: null,
  signedOut: false,
  projects: [],
  chats: {},
  chatData: {},
  error: "",
  socket: null,
  models: null,

  async init() {
    try {
      const user = await api.get<User>("/api/me");
      const projects = await api.get<Project[]>("/api/projects");
      const socket = new ForgeSocket();
      socket.onMessage((message) => get().handle(message));
      socket.connect();
      set({ user, projects, socket, signedOut: false });
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) set({ signedOut: true });
      else set({ error: String(err) });
    }
  },

  async loadModels() {
    const models = await api.get<Model[]>("/api/models");
    set({ models });
  },

  async searchFiles(projectId, query) {
    const params = new URLSearchParams({ query, limit: "20" });
    const found = await api.get<{ files: string[] }>(`/api/projects/${projectId}/files/search?${params}`);
    return found.files;
  },

  async loadChat(projectId) {
    const chat = await api.get<Chat>(`/api/projects/${projectId}/chat`);
    set((s) => ({ chats: { ...s.chats, [projectId]: chat } }));
    return chat;
  },

  async createProject(body) {
    const project = await api.post<Project>("/api/projects", body);
    set((s) => ({ projects: [project, ...s.projects] }));
    return project;
  },

  forgetProject(projectId) {
    set((s) => ({ projects: s.projects.filter((p) => p.id !== projectId) }));
  },

  async updateChat(chat, changes) {
    const updated = await api.patch<Chat>(`/api/chats/${chat.id}`, changes);
    set((s) => ({ chats: { ...s.chats, [chat.project_id]: updated } }));
  },

  async clearChat(chat) {
    // The server puts an empty chat in its place; the project then shows that one.
    await api.delete(`/api/chats/${chat.id}`);
    get().closeChat(chat.id);
    set((s) => {
      const chatData = { ...s.chatData };
      delete chatData[chat.id];
      return { chatData };
    });
    await get().loadChat(chat.project_id);
  },

  openChat(chatId) {
    if (!get().chatData[chatId]) set((s) => ({ chatData: { ...s.chatData, [chatId]: emptyChat() } }));
    get().socket?.watch(chatId, () => get().chatData[chatId]?.lastSeq ?? 0);
  },

  closeChat(chatId) {
    get().socket?.unwatch(chatId);
  },

  async send(chatId, text, files = []) {
    await api.post(`/api/chats/${chatId}/messages`, { text, attachments: files });
  },

  async upload(chatId, file) {
    const name = encodeURIComponent(file.name || "file");
    return api.postBytes<Attachment>(`/api/chats/${chatId}/attachments?name=${name}`, file, file.type || undefined);
  },

  async answer(chatId, requestId, answer) {
    const result = await api.post<{ accepted: boolean }>(`/api/chats/${chatId}/answer`, {
      request_id: requestId,
      answer,
    });
    return result.accepted;
  },

  async cancel(chatId) {
    await api.post(`/api/chats/${chatId}/cancel`);
  },

  handle(message) {
    if (message.type === "chat_state") {
      set((s) => ({
        chats: Object.fromEntries(
          Object.entries(s.chats).map(([projectId, chat]) => [
            projectId,
            chat.id === message.chat_id ? { ...chat, state: message.state, title: message.title } : chat,
          ]),
        ),
      }));
      return;
    }
    if (message.type === "item" || message.type === "live" || message.type === "subscribed") {
      const current = get().chatData[message.chat_id];
      if (!current) return;
      const next = applyMessage(current, message);
      if (next !== current) set((s) => ({ chatData: { ...s.chatData, [message.chat_id]: next } }));
    }
  },

  setUserName(name) {
    set((s) => ({ user: s.user ? { ...s.user, name } : s.user }));
  },
  setError(error) {
    set({ error });
  },
}));
