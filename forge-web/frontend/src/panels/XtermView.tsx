// One terminal on screen (xterm.js), loaded only when the terminal tab is opened. It fits its box
// and tells the sandbox every new size.

import { FitAddon } from "@xterm/addon-fit";
import { Terminal } from "@xterm/xterm";
import "@xterm/xterm/css/xterm.css";
import { useEffect, useRef } from "react";
import { FONT_SIZE, TerminalLink, type LinkState } from "./Terminal";

interface Props {
  url: string;
  active: boolean;
  onState: (state: LinkState) => void;
}

/** The page's colours (from the CSS tokens), so the terminal follows light and dark mode. */
function theme() {
  const css = getComputedStyle(document.documentElement);
  const token = (name: string) => css.getPropertyValue(name).trim() || undefined;
  return {
    background: token("--card"),
    foreground: token("--text"),
    cursor: token("--accent"),
    selectionBackground: "rgba(194, 98, 45, 0.3)",
  };
}

export default function XtermView({ url, active, onState }: Props) {
  const box = useRef<HTMLDivElement>(null);
  const view = useRef<{ term: Terminal; fit: () => void } | null>(null);
  const onStateRef = useRef(onState);
  onStateRef.current = onState;

  useEffect(() => {
    const element = box.current;
    if (!element) return;
    const term = new Terminal({
      cursorBlink: true,
      fontFamily: getComputedStyle(element).fontFamily,
      fontSize: FONT_SIZE,
      scrollback: 5000,
      theme: theme(),
    });
    const fitAddon = new FitAddon();
    term.loadAddon(fitAddon);
    term.open(element);
    const link = new TerminalLink(url, {
      output: (data) => term.write(data),
      state: (state) => onStateRef.current(state),
      reset: () => term.reset(),
    });
    // A hidden box has no size; fitting it would shrink the terminal to nothing.
    const fit = () => element.offsetWidth > 0 && element.offsetHeight > 0 && fitAddon.fit();
    const typing = term.onData((text) => link.type(text));
    const binary = term.onBinary((data) => link.typeBinary(data));
    const resizing = term.onResize(({ cols, rows }) => link.resize(cols, rows));
    const observer = new ResizeObserver(() => fit());
    observer.observe(element);
    fit();
    link.resize(term.cols, term.rows);
    link.connect();
    view.current = { term, fit };
    return () => {
      observer.disconnect();
      typing.dispose();
      binary.dispose();
      resizing.dispose();
      link.close();
      term.dispose();
      view.current = null;
    };
  }, [url]);

  useEffect(() => {
    if (!active || !view.current) return;
    view.current.fit();
    view.current.term.focus();
  }, [active]);

  return <div ref={box} className="min-h-0 flex-1 p-1 font-mono" data-testid="xterm" />;
}
