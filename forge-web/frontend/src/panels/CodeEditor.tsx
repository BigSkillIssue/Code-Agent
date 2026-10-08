// The code editor (CodeMirror), loaded only when a file is opened. The language comes from the
// file name; Ctrl/Cmd+S saves.

import { LanguageDescription, type LanguageSupport } from "@codemirror/language";
import { languages } from "@codemirror/language-data";
import CodeMirror from "@uiw/react-codemirror";
import { useEffect, useState } from "react";

export interface CodeEditorProps {
  path: string;
  value: string;
  readOnly: boolean;
  onChange: (value: string) => void;
}

function prefersDark(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-color-scheme: dark)").matches === true;
}

export default function CodeEditor({ path, value, readOnly, onChange }: CodeEditorProps) {
  const [language, setLanguage] = useState<LanguageSupport | null>(null);
  useEffect(() => {
    let current = true;
    const found = LanguageDescription.matchFilename(languages, path);
    setLanguage(null);
    found
      ?.load()
      .then((support) => current && setLanguage(support))
      .catch(() => undefined);
    return () => {
      current = false;
    };
  }, [path]);
  return (
    <CodeMirror
      value={value}
      height="100%"
      className="h-full text-sm"
      theme={prefersDark() ? "dark" : "light"}
      extensions={language ? [language] : []}
      readOnly={readOnly}
      onChange={onChange}
      basicSetup={{ foldGutter: true, highlightActiveLine: !readOnly }}
    />
  );
}
