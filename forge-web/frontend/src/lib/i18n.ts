// German and English texts; the browser's language picks one.

const en = {
  appName: "Forge",
  newChat: "New chat",
  newProject: "New project",
  projectName: "Project name",
  create: "Create",
  cancel: "Cancel",
  projects: "Projects",
  noProjects: "No projects yet. Create one to start.",
  noChats: "No chats yet",
  pickChat: "Pick a chat on the left, or start a new one.",
  placeholder: "Ask Forge to do something… (Enter to send, Shift+Enter for a new line)",
  send: "Send",
  stop: "Stop",
  allow: "Allow",
  allowAlways: "Allow for this chat",
  deny: "Deny",
  feedback: "Tell Forge what to do instead (optional)",
  allowed: "Allowed",
  denied: "Denied",
  withdrawn: "Withdrawn",
  answer: "Answer",
  answered: "Answered",
  dismiss: "Skip",
  other: "Other…",
  wantsTo: "Forge wants to run",
  question: "Forge has a question",
  taskUnderstood: "Task understood",
  plan: "Plan",
  done: "Done",
  failed: "Did not finish",
  stopped: "Stopped",
  filesChanged: "Files changed",
  cost: "Cost",
  working: "Working…",
  waiting: "Waiting for you",
  modeAsk: "Ask before changes",
  modeEdits: "Edits run, commands ask",
  modeAuto: "Allow everything",
  signIn: "Sign in",
  devSignIn: "Development mode: open the login link printed in the server log, or paste its token.",
  token: "Token",
  output: "Output",
  arguments: "Arguments",
  result: "Result",
  deleteChat: "Delete chat",
  confirmDelete: "Delete this chat?",
  reconnecting: "Reconnecting…",
};

type Texts = typeof en;

const de: Texts = {
  appName: "Forge",
  newChat: "Neuer Chat",
  newProject: "Neues Projekt",
  projectName: "Projektname",
  create: "Anlegen",
  cancel: "Abbrechen",
  projects: "Projekte",
  noProjects: "Noch keine Projekte. Lege eines an, um zu starten.",
  noChats: "Noch keine Chats",
  pickChat: "Wähle links einen Chat oder starte einen neuen.",
  placeholder: "Sag Forge, was zu tun ist … (Enter sendet, Umschalt+Enter für eine neue Zeile)",
  send: "Senden",
  stop: "Stopp",
  allow: "Erlauben",
  allowAlways: "Für diesen Chat erlauben",
  deny: "Ablehnen",
  feedback: "Was Forge stattdessen tun soll (optional)",
  allowed: "Erlaubt",
  denied: "Abgelehnt",
  withdrawn: "Zurückgezogen",
  answer: "Antworten",
  answered: "Beantwortet",
  dismiss: "Überspringen",
  other: "Andere …",
  wantsTo: "Forge möchte ausführen",
  question: "Forge hat eine Frage",
  taskUnderstood: "Aufgabe verstanden",
  plan: "Plan",
  done: "Fertig",
  failed: "Nicht fertig geworden",
  stopped: "Gestoppt",
  filesChanged: "Geänderte Dateien",
  cost: "Kosten",
  working: "Arbeitet …",
  waiting: "Wartet auf dich",
  modeAsk: "Vor Änderungen fragen",
  modeEdits: "Änderungen ja, Befehle fragen",
  modeAuto: "Alles erlauben",
  signIn: "Anmelden",
  devSignIn:
    "Entwicklungsmodus: Öffne den Anmeldelink aus dem Server-Log oder füge sein Token ein.",
  token: "Token",
  output: "Ausgabe",
  arguments: "Argumente",
  result: "Ergebnis",
  deleteChat: "Chat löschen",
  confirmDelete: "Diesen Chat löschen?",
  reconnecting: "Verbinde neu …",
};

export type TextKey = keyof Texts;

function pick(): Texts {
  const lang = typeof navigator === "undefined" ? "en" : navigator.language.toLowerCase();
  return lang.startsWith("de") ? de : en;
}

const texts = pick();

export function t(key: TextKey): string {
  return texts[key];
}
