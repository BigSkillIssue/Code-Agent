import { describe, expect, it } from "vitest";
import { coreEn } from "./i18n";
import { adminEn } from "./texts/admin";
import { appleEn } from "./texts/apple";
import { appsEn } from "./texts/apps";
import { billingEn } from "./texts/billing";
import { hostingEn } from "./texts/hosting";
import { listingEn } from "./texts/listing";
import { releaseEn } from "./texts/release";
import { submitEn } from "./texts/submit";

const BUNDLES = { coreEn, adminEn, appleEn, appsEn, releaseEn, listingEn, submitEn, hostingEn, billingEn };

describe("the texts", () => {
  it("never use a key twice, so no bundle silently replaces another one's text", () => {
    const seen = new Map<string, string>();
    for (const [bundle, texts] of Object.entries(BUNDLES)) {
      for (const key of Object.keys(texts)) {
        expect(seen.get(key), `${key} is in ${seen.get(key)} and ${bundle}`).toBeUndefined();
        seen.set(key, bundle);
      }
    }
  });
});
