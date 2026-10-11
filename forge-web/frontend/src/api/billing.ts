// Billing (W31–W33): the admin's Stripe keys and the catalog (plans, an app's one-time price,
// hosting, add-ons, coupons) that is synced to Stripe; a customer's offers, plan, limits and order.

import { api } from "./client";

export type ItemKind = "plan" | "app_once" | "hosting" | "addon";
export type Interval = "month" | "year" | "once";

export interface StripeSetup {
  configured: boolean;
  mode: "" | "test" | "live";
  hint: string;
  updated_at: number;
  enabled: boolean;
  webhook_url: string;
}

/** What a plan allows (W33a enforces it); null: no limit. */
export interface Limits {
  projects: number | null;
  ai_budget_month_eur: number | null;
  ai_budget_per_product_eur: number | null;
  mac_minutes: number | null;
  hosted_apps: number | null;
  storage_gb: number | null;
  custom_domains: number | null;
  store_publishing: boolean;
}

export interface ItemInput {
  kind: ItemKind;
  name: string;
  description: string;
  currency: string;
  amounts: Partial<Record<Interval, number>>; // cents
  trial_days: number;
  limits: Partial<Limits>;
}

export interface CatalogItem extends ItemInput {
  id: string;
  limits: Limits;
  archived_at: number;
  stripe_product: string;
  prices: Partial<Record<Interval, { id: string; amount: number; currency: string }>>;
  synced: boolean;
}

export interface CouponInput {
  code: string;
  name: string;
  percent_off: number | null;
  amount_off: number | null;
  currency: string;
  duration: "once" | "repeating" | "forever";
  duration_in_months: number | null;
}

export interface Coupon extends CouponInput {
  id: string;
  archived_at: number;
  synced: boolean;
}

export interface SyncReport {
  created: string[];
  updated: string[];
  archived: string[];
  unchanged: number;
}

const base = "/api/admin/billing";
const id = encodeURIComponent;

export const billingAdmin = {
  stripe: () => api.get<StripeSetup>(`${base}/stripe`),
  setKeys: (secretKey: string, webhookSecret: string) =>
    api.put<StripeSetup>(`${base}/stripe`, { secret_key: secretKey, webhook_secret: webhookSecret }),
  catalog: () => api.get<{ items: CatalogItem[]; coupons: Coupon[] }>(`${base}/catalog`),
  putItem: (itemId: string, item: ItemInput) => api.put<CatalogItem>(`${base}/catalog/items/${id(itemId)}`, item),
  removeItem: (itemId: string) => api.delete(`${base}/catalog/items/${id(itemId)}`),
  putCoupon: (couponId: string, coupon: CouponInput) => api.put<Coupon>(`${base}/catalog/coupons/${id(couponId)}`, coupon),
  removeCoupon: (couponId: string) => api.delete(`${base}/catalog/coupons/${id(couponId)}`),
  sync: () => api.post<SyncReport>(`${base}/catalog/sync`, {}),
};

/** An offer as customers see it. */
export interface Offer {
  id: string;
  kind: ItemKind;
  name: string;
  description: string;
  currency: string;
  amounts: Partial<Record<Interval, number>>;
  trial_days: number;
  limits: Limits;
}

/** Every limit of the signed-in user (null: no limit), and where it comes from. */
export interface Entitlements {
  source: string; // "server", "admin" or "plan:<offer>"
  projects: number | null;
  ai_month_usd: number | null;
  ai_product_usd: number | null;
  server_keys: boolean;
  mac_minutes: number | null;
  hosted_apps: number | null;
  storage_mb: number | null;
  custom_domains: number | null;
  store_publishing: boolean;
}

export interface MyBilling {
  enabled: boolean;
  entitlements: Entitlements;
  subscriptions: { item_id: string; status: string; interval: string; quantity: number; current_period_end: number }[];
}

export const billing = {
  offers: () => api.get<Offer[]>("/api/billing/catalog"),
  me: () => api.get<MyBilling>("/api/billing/me"),
  checkout: (itemId: string, interval: Interval, waiver: boolean) =>
    api.post<{ url: string }>("/api/billing/checkout", { item_id: itemId, interval, waiver }),
  cancel: (contract: string) =>
    api.post<{ id: string; status: "done" | "manual"; received_at: number; effective_at: number }>(
      "/api/billing/cancel",
      { contract },
    ),
  portal: () => api.post<{ url: string }>("/api/billing/portal", {}),
};
