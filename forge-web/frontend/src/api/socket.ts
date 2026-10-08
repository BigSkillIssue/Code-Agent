// One WebSocket per tab. It reconnects with backoff and, after reconnecting, subscribes again to
// every chat it watched, from the last number it has — so nothing is missed or shown twice.

import type { ServerMessage } from "./types";

type Listener = (message: ServerMessage) => void;

export class ForgeSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private watched = new Map<string, () => number>(); // chat id -> its last stored number
  private delay = 500;
  private closed = false;
  connected = false;

  constructor(private readonly url: string = defaultUrl()) {}

  connect(): void {
    this.closed = false;
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.connected = true;
      this.delay = 500;
      for (const [chatId, lastSeq] of this.watched) this.sendSubscribe(chatId, lastSeq());
    };
    ws.onmessage = (event) => {
      const message = JSON.parse(String(event.data)) as ServerMessage;
      for (const listener of this.listeners) listener(message);
    };
    ws.onclose = () => {
      this.connected = false;
      this.ws = null;
      if (!this.closed) {
        setTimeout(() => this.connect(), this.delay);
        this.delay = Math.min(this.delay * 2, 10_000);
      }
    };
  }

  close(): void {
    this.closed = true;
    this.ws?.close();
  }

  onMessage(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Follow a chat; `lastSeq` is asked again on every reconnect. */
  watch(chatId: string, lastSeq: () => number): void {
    this.watched.set(chatId, lastSeq);
    if (this.connected) this.sendSubscribe(chatId, lastSeq());
  }

  unwatch(chatId: string): void {
    this.watched.delete(chatId);
    this.send({ type: "unsubscribe", chat_id: chatId });
  }

  private sendSubscribe(chatId: string, afterSeq: number): void {
    this.send({ type: "subscribe", chat_id: chatId, after_seq: afterSeq });
  }

  private send(message: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(message));
  }
}

function defaultUrl(): string {
  const scheme = window.location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${window.location.host}/api/ws`;
}
