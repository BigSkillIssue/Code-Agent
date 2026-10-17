// The admin's view and control of every customer's billing (W40c): plans given for free,
// subscriptions ended, dunning held or closed, apps unlocked, cancellations carried out.

import type { Entitlements } from "./billing";
import { api } from "./client";

export interface CustomerRow {
  id: string;
  email: string;
  name: string;
  role: string;
  status: string;
  paying: string[];
  gift: string;
  dunning: string; // the open case's stage, "" without one
  held: boolean;
}

export interface Gift {
  item_id: string;
  until: number; // 0: for good
  note: string;
  granted_by: string;
  granted_at: number;
}

export interface Cancellation {
  id: string;
  received_at: number;
  name: string;
  email: string;
  contract: string;
  extraordinary: boolean;
  reason: string;
  status: string;
  effective_at: number;
  user_id: string;
}

export interface CustomerDetail {
  id: string;
  email: string;
  name: string;
  role: string;
  stripe_customer: string;
  entitlements: Entitlements;
  subscriptions: { item_id: string; status: string; interval: string; quantity: number; current_period_end: number; stripe_subscription: string }[];
  orders: { id: string; item_id: string; interval: string; status: string; created_at: number; completed_at: number; project_id: string; trial_days: number }[];
  apps: { project_id: string; name: string; unlocked: boolean; by: "" | "paid" | "admin" }[];
  dunning: { id: string; stage: string; opened_at: number; stage_at: number; closed_at: number; held_at: number; invoices: string } | null;
  gift: Gift | null;
  cancellations: Cancellation[];
}

const base = "/api/admin/billing";
const id = encodeURIComponent;

export const customersAdmin = {
  list: (q: string) => api.get<CustomerRow[]>(`${base}/customers?q=${id(q)}`),
  customer: (userId: string) => api.get<CustomerDetail>(`${base}/customers/${id(userId)}`),
  give: (userId: string, itemId: string, until: number, note: string) =>
    api.put<Gift>(`${base}/customers/${id(userId)}/gift`, { item_id: itemId, until, note }),
  takeBack: (userId: string) => api.delete(`${base}/customers/${id(userId)}/gift`),
  end: (userId: string, subscription: string, now: boolean) =>
    api.post(`${base}/customers/${id(userId)}/subscriptions/${id(subscription)}/end`, { now }),
  dunning: (userId: string, action: "hold" | "resume" | "close") => api.post(`${base}/customers/${id(userId)}/dunning/${action}`),
  app: (userId: string, projectId: string, change: "unlock" | "lock") =>
    api.post(`${base}/customers/${id(userId)}/apps/${id(projectId)}/${change}`),
  cancellations: () => api.get<{ waiting: Cancellation[]; recent: Cancellation[] }>(`${base}/cancellations`),
  carryOut: (cancellationId: string, action: "period_end" | "now" | "nothing") =>
    api.post<Cancellation>(`${base}/cancellations/${id(cancellationId)}`, { action }),
};
