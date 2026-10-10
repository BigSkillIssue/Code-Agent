// The App Store listing of an Apple project: Forge's draft and what the user saved.

import { api } from "./client";

export type Frequency = "NONE" | "INFREQUENT" | "FREQUENT";

export interface AgeRating {
  [question: string]: Frequency | boolean;
}

export interface PrivacyAnswer {
  data_type: string;
  purposes: string[];
  linked_to_user: boolean;
  used_for_tracking: boolean;
}

/** The listing as Forge's model (and Apple) defines it. */
export interface StoreListing {
  locale: string;
  name: string;
  subtitle: string;
  description: string;
  keywords: string;
  promotional_text: string;
  whats_new: string;
  copyright: string;
  primary_category: string;
  secondary_category: string | null;
  age_rating: AgeRating;
  collects_data: boolean;
  privacy: PrivacyAnswer[];
  notes: string[];
  support_url: string;
  marketing_url: string;
  privacy_policy_url: string;
}

export interface ListingView {
  listing: StoreListing | null;
  source: "saved" | "draft" | "none";
  updated_at: number;
  missing: string[];
}

export const appleListing = (projectId: string) => {
  const base = `/api/projects/${encodeURIComponent(projectId)}/apple/listing`;
  return {
    get: () => api.get<ListingView>(base),
    draft: () => api.get<ListingView>(`${base}/draft`),
    save: (listing: StoreListing) => api.put<ListingView>(base, listing),
  };
};

export const CATEGORIES = [
  "BOOKS", "BUSINESS", "DEVELOPER_TOOLS", "EDUCATION", "ENTERTAINMENT", "FINANCE", "FOOD_AND_DRINK",
  "GAMES", "GRAPHICS_AND_DESIGN", "HEALTH_AND_FITNESS", "LIFESTYLE", "MEDICAL", "MUSIC", "NAVIGATION",
  "NEWS", "PHOTO_AND_VIDEO", "PRODUCTIVITY", "REFERENCE", "SHOPPING", "SOCIAL_NETWORKING", "SPORTS",
  "TRAVEL", "UTILITIES", "WEATHER",
];

/** Apple's limits, as Forge's model checks them (keywords count bytes). */
export const LIMITS = { name: 30, subtitle: 30, description: 4000, keywords: 100, promotional_text: 170, whats_new: 4000 };

export const bytes = (text: string) => new TextEncoder().encode(text).length;
