// Forge's mark: an anvil with the spark struck from it, and the word mark beside it. The anvil
// takes the text colour around it; the spark is always ember (`.logo-spark` in styles.css).
// public/favicon.svg draws the same paths (the Logo test checks that they stay the same).

/** The anvil: horn to the left, face on top, waist and foot below (32 × 32 grid). */
export const ANVIL_PATH =
  "M2.5 12H28.5V15.5C26 15.5 23.5 16.3 22.5 19V22L26 24.5V27H8V24.5L12 22V19C11.2 16.8 9 16 6.5 15.6C4.6 15.3 3.2 14 2.5 12Z";
/** The spark above the face, four-pointed. */
export const SPARK_PATH =
  "M21 1.2C21.35 4.2 22 5.2 25.2 5.6C22 6 21.35 7 21 10C20.65 7 20 6 16.8 5.6C20 5.2 20.65 4.2 21 1.2Z";

/** The mark alone; with a `label` it is an image of its own, without one it is decoration. */
export function LogoMark({ size = 28, label, className = "" }: { size?: number; label?: string; className?: string }) {
  const a11y = label ? { role: "img", "aria-label": label } : { "aria-hidden": true };
  return (
    <svg viewBox="0 0 32 32" width={size} height={size} className={`shrink-0 ${className}`} {...a11y}>
      <path d={ANVIL_PATH} fill="currentColor" />
      <path className="logo-spark" d={SPARK_PATH} />
      <circle className="logo-spark" cx="27.4" cy="2.4" r="1.1" />
    </svg>
  );
}

/** The mark and the word mark "Forge", sized together by `size` (the mark's height in pixels). */
export function Logo({ size = 28, wordmark = true, className = "" }: { size?: number; wordmark?: boolean; className?: string }) {
  if (!wordmark) return <LogoMark size={size} label="Forge" className={className} />;
  return (
    <span className={`inline-flex items-end gap-[0.12em] ${className}`} style={{ fontSize: Math.round(size * 0.86) }}>
      <LogoMark size={size} />
      <span className="wordmark translate-y-[-0.02em]">Forge</span>
    </span>
  );
}
