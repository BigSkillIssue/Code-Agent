import { describe, expect, it } from "vitest";
import css from "../styles.css?raw";
import { contrast, luminance, themeTokens } from "./contrast";

const themes = themeTokens(css);

// Every text colour the components use, on every surface it appears on.
const SURFACES = ["bg", "panel", "card", "code", "accent-soft"];
const TEXT = ["text", "muted", "accent", "ok", "warn", "bad", "copper"];
// Colours that fill a control, with the text set on them.
const FILLED: [string, string][] = [
  ["accent-text", "accent"], // primary buttons
  ["bg", "bad"], // a danger button on hover
  ["bg", "text"], // the stop button
];
// Edges that alone show where a field is (WCAG 1.4.11, 3:1), and the focus ring.
const EDGES: [string, string][] = [
  ["border-strong", "bg"],
  ["border-strong", "panel"],
  ["border-strong", "card"],
  ["accent", "bg"],
  ["accent", "panel"],
  ["accent", "card"],
];

describe.each(Object.entries(themes))("the %s theme", (_name, tokens) => {
  it("has every token as a #rrggbb colour", () => {
    for (const name of [...SURFACES, ...TEXT, "accent-text", "border", "border-strong", "spark"]) {
      expect(() => luminance(tokens[name] ?? "missing"), name).not.toThrow();
    }
  });

  it.each(TEXT)("sets %s at AA (4.5:1) on every surface", (text) => {
    for (const surface of SURFACES) {
      expect(contrast(tokens[text], tokens[surface]), `${text} on ${surface}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("sets text on filled controls at AA", () => {
    for (const [text, fill] of FILLED) {
      expect(contrast(tokens[text], tokens[fill]), `${text} on ${fill}`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("draws field edges and the focus ring at 3:1", () => {
    for (const [edge, surface] of EDGES) {
      expect(contrast(tokens[edge], tokens[surface]), `${edge} on ${surface}`).toBeGreaterThanOrEqual(3);
    }
  });
});

describe("the themes", () => {
  it("are Glut & Schmiede: ember on near-black, a burnt ember on cream", () => {
    expect(themes.dark).toMatchObject({ bg: "#14110f", panel: "#1f1a17", accent: "#ff6b2c", copper: "#c8875a", ok: "#4cc38a" });
    expect(themes.light).toMatchObject({ bg: "#fbf6ef", spark: "#e2531b" });
  });

  it("keep the forge side of sign-in readable in both", () => {
    const { light, dark } = themes;
    expect(light["forge-bg"]).toBe(dark["forge-bg"]);
    expect(contrast(light["forge-text"], light["forge-bg"])).toBeGreaterThanOrEqual(4.5);
    // The hottest point of the fire behind the tagline, measured in a screenshot (1400 × 900).
    expect(contrast(light["forge-text"], "#96522c")).toBeGreaterThanOrEqual(4.5);
  });
});

describe("contrast", () => {
  it("matches the WCAG reference values", () => {
    expect(contrast("#000000", "#ffffff")).toBeCloseTo(21, 5);
    expect(contrast("#777777", "#ffffff")).toBeCloseTo(4.48, 2);
    expect(contrast("#123456", "#123456")).toBe(1);
  });
});
