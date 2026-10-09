// The server answers in English; these are its common messages in German. A message that is
// not listed stays as the server wrote it, so nothing is ever lost in translation.

import { language } from "./i18n";

const exact: Record<string, string> = {
  "too many attempts; try again later": "Zu viele Versuche – bitte später noch einmal.",
  "wrong email or password": "E-Mail oder Passwort stimmt nicht.",
  "sign in first": "Bitte zuerst anmelden.",
  "sign in again first": "Bitte zuerst neu anmelden.",
  "sign in with your password or provider first": "Bitte zuerst mit Passwort oder Anbieter anmelden.",
  "missing or wrong CSRF token": "Die Seite ist veraltet – bitte neu laden.",
  "that code is not right": "Der Code stimmt nicht.",
  "that code is not right; is the time on your phone right?":
    "Der Code stimmt nicht – geht die Uhr auf deinem Handy richtig?",
  "start the setup first": "Bitte die Einrichtung zuerst starten.",
  "two-factor sign-in is on already": "Die Zwei-Faktor-Anmeldung ist schon an.",
  "two-factor sign-in is off already": "Die Zwei-Faktor-Anmeldung ist schon aus.",
  "this server requires two-factor sign-in for admins":
    "Dieser Server verlangt für Admins die Zwei-Faktor-Anmeldung.",
  "password accounts are turned off": "Konten mit Passwort sind auf diesem Server ausgeschaltet.",
  "password sign-in is turned off on this server":
    "Die Anmeldung mit Passwort ist auf diesem Server ausgeschaltet.",
  "the current password is not right": "Das aktuelle Passwort stimmt nicht.",
  "use a less repetitive password": "Passwort: bitte weniger Wiederholungen verwenden.",
  "this link is not valid (any more)": "Dieser Link gilt nicht (mehr).",
  "this reset link is not valid (any more)": "Dieser Link zum Zurücksetzen gilt nicht (mehr).",
  "this login link is not valid": "Dieser Anmeldelink gilt nicht.",
  "this invite is not valid (any more)": "Diese Einladung gilt nicht (mehr).",
  "this invite is not valid for this email": "Diese Einladung gilt nicht für diese E-Mail-Adresse.",
  "signing up needs an invite from an admin": "Registrieren geht nur mit einer Einladung eines Admins.",
  "this email domain may not sign up here": "Mit dieser E-Mail-Domain kann man sich hier nicht registrieren.",
  "an account with this email exists already": "Ein Konto mit dieser E-Mail-Adresse gibt es schon.",
  "that does not look like an email address": "Das sieht nicht nach einer E-Mail-Adresse aus.",
  "your account is waiting for an admin to approve it": "Dein Konto wartet auf die Freischaltung durch einen Admin.",
  "please confirm your email address first (see the mail we sent)":
    "Bitte bestätige zuerst deine E-Mail-Adresse (siehe unsere Mail).",
  "this account is disabled": "Dieses Konto ist gesperrt.",
  "this account no longer exists": "Dieses Konto gibt es nicht mehr.",
  "this server is already set up": "Dieser Server ist schon eingerichtet.",
  "the setup token is not right": "Das Einrichtungs-Token stimmt nicht.",
  "this is your only way to sign in; set a password first":
    "Das ist deine einzige Anmeldung – lege zuerst ein Passwort fest.",
  "this sign-in attempt is not valid any more; please start again":
    "Dieser Anmeldeversuch gilt nicht mehr – bitte neu beginnen.",
  "the sign-in was cancelled": "Die Anmeldung wurde abgebrochen.",
  "only admins can do that": "Das dürfen nur Admins.",
  "only the chat's owner can do that": "Das darf nur, wem der Chat gehört.",
  "the server needs at least one active admin": "Der Server braucht mindestens einen aktiven Admin.",
  "a project needs at least one owner": "Ein Projekt braucht mindestens einen Besitzer.",
  "already a member": "Schon Mitglied.",
  "no active account with this email": "Es gibt kein aktives Konto mit dieser E-Mail-Adresse.",
  "this account has not confirmed its email yet": "Dieses Konto hat seine E-Mail-Adresse noch nicht bestätigt.",
  "no account waiting for approval": "Kein Konto wartet auf Freischaltung.",
  "no such project": "Dieses Projekt gibt es nicht.",
  "no such chat": "Diesen Chat gibt es nicht.",
  "no such user": "Diese Person gibt es nicht.",
  "no such account": "Dieses Konto gibt es nicht.",
  "no such member": "Dieses Mitglied gibt es nicht.",
  "no such terminal": "Dieses Terminal gibt es nicht.",
  "no such key": "Diesen Schlüssel gibt es nicht.",
  "not found": "Nicht gefunden.",
  "the project's sandbox is not reachable": "Die Sandbox des Projekts ist nicht erreichbar.",
  "the project's sandbox could not be started": "Die Sandbox des Projekts konnte nicht starten.",
  "the chat is still working on the last message": "Der Chat arbeitet noch an der letzten Nachricht.",
  "the project has no remote yet; set one first": "Das Projekt hat noch kein Remote – bitte zuerst eines festlegen.",
  "no branch is checked out": "Es ist kein Branch ausgecheckt.",
  "nothing is staged for the commit": "Für den Commit ist nichts vorgemerkt.",
  "the folder must be an absolute path": "Der Ordner muss ein absoluter Pfad sein.",
  "the folder does not exist": "Den Ordner gibt es nicht.",
  "that is not a folder": "Das ist kein Ordner.",
  "the folder is not under one of the allowed folders": "Der Ordner liegt nicht unter einem freigegebenen Ordner.",
  "only admins can open server folders as projects": "Nur Admins können Server-Ordner als Projekt öffnen.",
  "this server does not open server folders as projects": "Dieser Server öffnet keine Server-Ordner als Projekt.",
};

const ROLES: Record<string, string> = { owner: "Besitzer", editor: "Bearbeiter", viewer: "Betrachter" };

const patterns: [RegExp, (...parts: string[]) => string][] = [
  [/^password: use at least (\d+) characters$/, (n) => `Passwort: bitte mindestens ${n} Zeichen.`],
  [/^password: use at most (\d+) characters$/, (n) => `Passwort: bitte höchstens ${n} Zeichen.`],
  [/^password: use a less repetitive password$/, () => exact["use a less repetitive password"]],
  [/^you have reached the limit of (\d+) projects$/, (n) => `Du hast die Grenze von ${n} Projekten erreicht.`],
  [/^this needs the (\w+) role in the project$/, (role) => `Dafür brauchst du im Projekt die Rolle „${ROLES[role] ?? role}“.`],
  [/^your (.+) account has no verified email$/, (p) => `Dein ${p}-Konto hat keine bestätigte E-Mail-Adresse.`],
  [/^this (.+) account is linked to another user$/, (p) => `Dieses ${p}-Konto ist mit einer anderen Person verknüpft.`],
  [
    /^an account with this email exists; sign in with its password and link (.+) in your settings$/,
    (p) => `Ein Konto mit dieser E-Mail-Adresse gibt es schon: Melde dich mit seinem Passwort an und verknüpfe ${p} in deinen Einstellungen.`,
  ],
  [/^'(.+)' is not a valid branch name$/, (name) => `„${name}“ ist kein gültiger Branch-Name.`],
  [/^this server does not push to or pull from (.+)$/, (host) => `Dieser Server pusht und pullt nicht mit ${host}.`],
  [/^git (\w+) failed: ([\s\S]*)$/, (action, why) => `git ${action} fehlgeschlagen: ${why}`],
  [/^(.+) leads outside the workspace$/, (path) => `${path} führt aus dem Projekt hinaus.`],
];

/** The German text for a server message, or null when there is none. */
export function germanError(message: string): string | null {
  const text = message.trim();
  if (Object.hasOwn(exact, text)) return exact[text];
  for (const [pattern, render] of patterns) {
    const match = text.match(pattern);
    if (match) return render(...match.slice(1));
  }
  return null;
}

/** A server message in the language of the page. */
export function localError(message: string, lang: "de" | "en" = language): string {
  return lang === "de" ? (germanError(message) ?? message) : message;
}
