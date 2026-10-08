// The app's state: who is signed in, projects, chats, and each open chat's items and live state.
// Chat items only ever come from the WebSocket, in order; this store appends them.

import { create } from "zustand";
import { api, ApiError } from "../api/client";
import { ForgeSocket } from "../api/socket";
import type {
  Chat,
  ChatItem,
  ChatMode,
  LiveState,
  Model,
  Project,
  ServerMessage,
  StoredItem,
  User,
} from "../api/types";

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
  chats: Record<string, Chat[]>; // project id -> chats
  chatData: Record<string, ChatData>;
  error: string;
  socket: ForgeSocket | null;
  models: Model[] | null;
  init: () => Promise<void>;
  loadModels: () => Promise<void>;
  searchFiles: (projectId: string, query: string) => Promise<string[]>;
  loadChats: (projectId: string) => Promise<void>;
  createProject: (name: string) => Promise<Project>;
  createChat: (projectId: string, mode?: ChatMode) => Promise<Chat>;
  updateChat: (chat: Chat, changes: Partial<Pick<Chat, "title" | "mode" | "model">>) => Promise<void>;
  deleteChat: (chat: Chat) => Promise<void>;
  openChat: (chatId: string) => void;
  closeChat: (chatId: string) => void;
  send: (chatId: string, text: string) => Promise<void>;
  answer: (chatId: string, requestId: string, answer: Record<string, unknown>) => Promise<boolean>;
  cancel: (chatId: string) => Promise<void>;
  handle: (message: ServerMessage) => void;
  setError: (error: string) => void;
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

  async loadChats(projectId) {
    const chats = await api.get<Chat[]>(`/api/projects/${projectId}/chats`);
    set((s) => ({ chats: { ...s.chats, [projectId]: chats } }));
  },

  async createProject(name) {
    const project = await api.post<Project>("/api/projects", { name });
    set((s) => ({ projects: [project, ...s.projects], chats: { ...s.chats, [project.id]: [] } }));
    return project;
  },

  async createChat(projectId, mode = "edits") {
    const chat = await api.post<Chat>(`/api/projects/${projectId}/chats`, { mode });
    set((s) => ({ chats: { ...s.chats, [projectId]: [chat, ...(s.chats[projectId] ?? [])] } }));
    return chat;
  },

  async updateChat(chat, changes) {
    const updated = await api.patch<Chat>(`/api/chats/${chat.id}`, changes);
    set((s) => ({
      chats: {
        ...s.chats,
        [chat.project_id]: (s.chats[chat.project_id] ?? []).map((c) => (c.id === chat.id ? updated : c)),
      },
    }));
  },

  async deleteChat(chat) {
    await api.delete(`/api/chats/${chat.id}`);
    get().closeChat(chat.id);
    set((s) => ({
      chats: {
        ...s.chats,
        [chat.project_id]: (s.chats[chat.project_id] ?? []).filter((c) => c.id !== chat.id),
      },
    }));
  },

  openChat(chatId) {
    if (!get().chatData[chatId]) set((s) => ({ chatData: { ...s.chatData, [chatId]: emptyChat() } }));
    get().socket?.watch(chatId, () => get().chatData[chatId]?.lastSeq ?? 0);
  },

  closeChat(chatId) {
    get().socket?.unwatch(chatId);
  },

  async send(chatId, text) {
    await api.post(`/api/chats/${chatId}/messages`, { text });
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
          Object.entries(s.chats).map(([projectId, chats]) => [
            projectId,
            chats.map((c) =>
              c.id === message.chat_id ? { ...c, state: message.state, title: message.title } : c,
            ),
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

  setError(error) {
    set({ error });
  },
}));
