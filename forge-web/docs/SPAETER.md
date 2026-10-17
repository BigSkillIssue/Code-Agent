# Für später

Offene Entscheidungen und bekannte Restpunkte von Forge Web, damit nichts verloren geht.
Stand: 17. Oktober 2026 (angelegt nach Schritt W18). Erledigtes hier einfach streichen.

## Zu entscheiden

1. **Schnellere Tests mit `pytest-xdist`?**
   Die Testsuite von Forge Web braucht hier etwa 3 Minuten (Ziel waren 90 Sekunden). `pytest-xdist` lässt
   die Tests parallel laufen. Das Paket steht noch nicht auf der erlaubten Liste in `AGENTS.md`.
   Wenn ja: als Dev-Abhängigkeit aufnehmen und im Gate `pytest -q -n auto` verwenden.

2. **Release `forge-web-v0.1.0` setzen?**
   Ein Tag mit diesem Namen startet `.github/workflows/forge-web-release.yml`: Wheels, Server- und
   Sandbox-Image (nach GHCR) und ein GitHub-Release. Danach aktualisiert man einen Server mit
   `forge-web/deploy/update.sh --release`, statt die Images selbst zu bauen.

3. **Pull Request nach `main`?**
   Dann liegt Forge Web direkt neben dem Forge-Kern auf `main`, und jede Kern-Änderung wird gleich mit
   Forge Web getestet. Danach aufräumen:
   - `.github/workflows/forge-web-sync.yml` und `forge-web/scripts/sync-core.sh` löschen (der Abgleich
     ist dann nicht mehr nötig);
   - in `docs/EINRICHTUNG.md` (Abschnitt 2.2) das `-b claude/elegant-tesla-h2iugo` beim Klonen streichen.

4. **Windows-Apps signieren** (am 17. Oktober 2026 auf später gelegt).
   Ohne Signatur warnt Windows beim Installieren mit „Unbekannter Herausgeber“ (SmartScreen); die
   Download-Seite sagt das bisher offen. Vorschlag: Azure Artifact Signing (etwa 10 $ im Monat) und eine
   zweite kleine Windows-VM, die nur signiert und nie den Code eines Produkts ausführt. Forge Web kann das
   schon (EINRICHTUNG §13, „Signier-Rechner“); es fehlen nur das Konto, das Zertifikat und die VM.
   Schon entschieden: zuerst signiert das Zertifikat des Admins alle Kunden-Apps; eigene Zertifikate der
   Kunden kommen später.

## Schon entschieden

- **Android nur über Google Play** (17. Oktober 2026): keine APK-Datei zum Herunterladen von der Website.
- **Keine Download-Seite in Forge Web** (17. Oktober 2026): die Programme der Kunden gibt es nur über die
  Website oder App des Kunden (`/downloads` der App und die Updates der App selbst), nie auf Forge Webs
  eigener Domain. Freigaben laufen über den Admin-Bereich.

## Bekannte Restpunkte

Die Einzelheiten stehen in [`SECURITY.md`](SECURITY.md) (Abschnitt *Accepted risks*) und in
[`../PROGRESS.md`](../PROGRESS.md) (*Open issues*).

- **Git-Aufträge im normalen Docker-Netz** (Klonen, Push, Pull): Ein Angriffsweg wurde nicht gefunden. Auf
  Cloud-Servern die Firewall-Regel aus `EINRICHTUNG.md` Abschnitt 9 setzen. Ein Umbau auf ein abgeschottetes
  Netz ist möglich, wenn gewünscht.
- **Plattenquote**: Sie wird alle 5 Minuten gemessen. Dazwischen kann ein Projekt kurz mehr schreiben,
  deshalb gehören die Docker-Daten auf eine eigene Partition.
- **Chats in fremden Projekten** laufen in der Sandbox der Besitzer. Diese können sie lesen und, solange ein
  Lauf geht, dessen Modellzugang nutzen.
- **Safari**: Die Vorschau im Rahmen ist dort nicht getestet; notfalls „In neuem Tab öffnen“.
- **Kein QR-Code** für die Zwei-Faktor-Anmeldung (dafür fehlt eine erlaubte Bibliothek).
- **IPv6-Test** (`::1`) läuft hier nicht (die Maschine hat kein IPv6), nur in der CI.

## Bis zum Merge: so kommen Kern-Änderungen in Forge Web an

- Forge Web nutzt Forge als Bibliothek, nicht als Kopie. Am Code muss dafür nichts nachgezogen werden.
- Nach jedem Push auf `main`, der den Kern ändert, mischt `.github/workflows/forge-web-sync.yml` `main` in
  den Forge-Web-Branch. Er führt die Prüfungen von Forge Web aus und pusht nur, wenn sie grün sind; danach
  läuft die CI von Forge Web. Bei einem Konflikt oder roten Prüfungen öffnet er ein Issue
  „Forge Web: Kern-Änderung nicht übernommen“. Von Hand geht dasselbe mit `bash forge-web/scripts/sync-core.sh`.
- Auf dem Server holt `forge-web/deploy/update.sh` den neuen Stand, baut beide Images neu und startet neu.
  Projekte bekommen das neue Forge beim nächsten Start ihrer Sandbox; ihre Dateien bleiben.
  `forge-web doctor` zeigt, ob im Sandbox-Image dasselbe Forge steckt wie im Server.
