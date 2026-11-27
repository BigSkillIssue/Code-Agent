// WCAG contrast of the colour tokens in styles.css, which stays their one source: the tokens are
// read from its `:root` blocks (light) and from its `prefers-color-scheme: dark` block (dark).

export type Tokens = Record<string, string>;

const DARK = /@media\s*\(prefers-color-scheme:\s*dark\)\s*\{\s*:root\s*\{([^}]*)\}\s*\}/;

/** The `--name: value` pairs of one CSS block, names without the dashes. */
function declarations(block: string): Tokens {
  const found: Tokens = {};
  for (const match of block.matchAll(/--([\w-]+)\s*:\s*([^;]+);/g)) found[match[1]] = match[2].trim();
  return found;
}

/** The light and the dark tokens of a stylesheet; dark starts from light and overrides it. */
export function themeTokens(css: string): { light: Tokens; dark: Tokens } {
  const plain = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const dark = DARK.exec(plain);
  const outside = dark ? plain.replace(dark[0], "") : plain;
  const light: Tokens = {};
  for (const block of outside.matchAll(/:root\s*\{([^}]*)\}/g)) Object.assign(light, declarations(block[1]));
  return { light, dark: { ...light, ...(dark ? declarations(dark[1]) : {}) } };
}

/** The relative luminance of a `#rrggbb` colour. */
export function luminance(hex: string): number {
  const match = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!match) throw new Error(`not a #rrggbb colour: ${hex}`);
  const [r, g, b] = [0, 2, 4].map((at) => {
    const channel = parseInt(match[1].slice(at, at + 2), 16) / 255;
    return channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4;
  });
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** The WCAG contrast ratio of two colours (1 to 21). */
export function contrast(a: string, b: string): number {
  const [light, dark] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (light + 0.05) / (dark + 0.05);
}
