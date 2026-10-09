# Forge Web einrichten – Schritt für Schritt

Diese Anleitung bringt Forge Web auf einen eigenen Server, sodass du und andere sich im Browser
anmelden (mit Google, GitHub oder E-Mail und Passwort) und mit Forge an Projekten arbeiten können.
Jedes Projekt läuft in einem eigenen, abgeschotteten Docker-Container.

Es gibt zwei Wege:

- **Weg A – Docker Compose (empfohlen, Linux):** Forge Web, die Projekt-Container und Caddy (für
  HTTPS) laufen alle in Docker. Am wenigsten Handarbeit.
- **Weg B – ohne Docker für den Server:** Forge Web läuft als Dienst direkt auf Linux, macOS oder
  Windows. Die Projekte brauchen trotzdem Docker.

Nur zum Ausprobieren auf dem eigenen Rechner reicht: `uv run forge-web serve --dev --fake`
(siehe `README.md`). Das ist nicht für andere Personen gedacht.

---

## 1. Was du brauchst

- Einen Server mit **Linux** (z. B. Ubuntu 24.04 oder Debian 12), mindestens 2 CPU-Kerne, 4 GB
  Arbeitsspeicher und 40 GB Platte. Mehr, wenn viele Leute gleichzeitig arbeiten (pro aktivem
  Projekt rechnet man mit bis zu 2 Kernen und 4 GB).
- Eine **Domain** für Forge, z. B. `forge.example.com`.
- Eine **zweite Domain für die Live-Vorschau**, z. B. `meine-vorschau.de`. Wichtig: am besten eine
  *eigene* Domain, nicht eine Subdomain von Forge. Dann kann eine Vorschau (die ja beliebigen Code
  aus den Projekten ausführt) niemals an die Anmeldung von Forge heran.
- Die Ports **80 und 443** müssen aus dem Internet erreichbar sein (Firewall/Router freigeben).
- Für die KI: mindestens einen **API-Schlüssel** eines Anbieters (z. B. Anthropic, OpenAI oder
  Google). Den trägst du später in der Weboberfläche ein.

### DNS-Einträge anlegen

Beim Anbieter deiner Domains legst du zwei Einträge an (statt `203.0.113.10` die IP deines Servers):

| Name | Typ | Wert |
| --- | --- | --- |
| `forge.example.com` | A | `203.0.113.10` |
| `*.meine-vorschau.de` | A | `203.0.113.10` |

Der Stern ist ein „Wildcard“-Eintrag: Jede Vorschau bekommt ihren eigenen Namen wie
`p5173-ab12cd34ef56ab78.meine-vorschau.de`. Hat der Server auch IPv6, lege zusätzlich
AAAA-Einträge an.

---

## 2. Weg A: Docker Compose (empfohlen)

### 2.1 Docker installieren

```bash
curl -fsSL https://get.docker.com | sudo sh
sudo systemctl enable --now docker
docker compose version          # muss eine Version zeigen
```

### 2.2 Dateien holen

```bash
git clone https://github.com/BigSkillIssue/Code-Agent.git
cd Code-Agent/forge-web
cp deploy/env.example .env
cp deploy/forge-web.example.toml forge-web.toml
```

### 2.3 `.env` ausfüllen

Öffne `.env` mit einem Editor (z. B. `nano .env`) und trage ein:

- `FORGE_DOMAIN` – deine Forge-Adresse, z. B. `forge.example.com`
- `PREVIEW_DOMAIN` – die Vorschau-Domain, z. B. `meine-vorschau.de`
- `ACME_EMAIL` – deine E-Mail für die HTTPS-Zertifikate (Let's Encrypt)
- `DOCKER_GID` – die Nummer der Gruppe „docker“. Du bekommst sie mit:

  ```bash
  getent group docker | cut -d: -f3
  ```

Die Felder `GOOGLE_SECRET` und `GITHUB_SECRET` füllst du in Abschnitt 4 und 5.

### 2.4 Die Images bauen (solange es noch kein Release gibt)

Sobald ein Release veröffentlicht ist, lädt `docker compose` die fertigen Images selbst. Bis dahin
baust du sie einmal aus dem Quellcode (dauert ein paar Minuten):

```bash
cd ..   # in den Ordner Code-Agent
docker build -f forge-web/docker/sandbox.Dockerfile -t forge-web-sandbox:local .
docker build -f forge-web/docker/server.Dockerfile -t forge-web:local .
cd forge-web
```

und trägst in `.env` ein:

```bash
FORGE_WEB_IMAGE=forge-web:local
SANDBOX_IMAGE=forge-web-sandbox:local
```

### 2.5 Starten

```bash
docker compose up -d
docker compose logs forge-web
```

In der Ausgabe steht eine Zeile wie

```text
Forge Web: create the first admin account at https://forge.example.com/setup#token=...
```

Öffne diesen Link im Browser und lege das **erste Admin-Konto** an. Der Link funktioniert nur, solange
es noch kein Konto gibt.

### 2.6 Prüfen

```bash
docker compose exec forge-web forge-web doctor
```

`doctor` prüft Docker, das Sandbox-Image, HTTPS, die Vorschau-Domain und die Anmeldeanbieter und sagt
bei jedem Problem, was zu tun ist. `FAIL` muss weg, `WARN` ist ein Hinweis.

---

## 3. Erste Schritte in der Weboberfläche

1. **Zwei-Faktor-Anmeldung einschalten:** unten links auf das Zahnrad (Einstellungen) →
   „Zwei-Faktor-Anmeldung einrichten“. Mit einer Authenticator-App (z. B. Google Authenticator,
   Microsoft Authenticator, 2FAS oder Bitwarden) den Schlüssel eintragen, Code eingeben,
   **Wiederherstellungscodes sicher aufbewahren**. In der Beispiel-Konfiguration müssen Admins das
   tun, bevor sie die Verwaltung benutzen können.
2. **KI-Schlüssel eintragen:** Schild-Symbol (Verwaltung) → „Server-Schlüssel“ → Anbieter wählen und
   den API-Schlüssel einfügen. Wer ihn nutzen darf, stellst du unter „Einstellungen“ (z. B. „Admins
   und Nutzer mit Freigabe“) und pro Person unter „Nutzer“ ein, mit Monatslimit in US-Dollar.
   Alternativ trägt jede Person in ihren Einstellungen einen eigenen Schlüssel ein.
3. **Leute einladen:** Verwaltung → „Einladungen“ → Einladung erstellen und den Link verschicken.
   Unter „Einstellungen“ kannst du die Registrierung auch öffnen (für alle oder nur bestimmte
   E-Mail-Domains, mit oder ohne Freischaltung).
4. **Erstes Projekt:** links „Neues Projekt“ → leer, Git-Repository, ZIP-Datei oder (nur Admins)
   Ordner auf dem Server.

---

## 4. Anmeldung mit Google einrichten (optional)

1. Öffne <https://console.cloud.google.com/> und lege ein Projekt an (oben links „Projekt
   auswählen“ → „Neues Projekt“).
2. „APIs & Dienste“ → „OAuth-Zustimmungsbildschirm“: Nutzertyp **Extern**, App-Name (z. B.
   „Forge“), Support-E-Mail und Kontakt-E-Mail eintragen. Bei den Bereichen (Scopes) reichen
   `openid`, `email` und `profile`. Danach „App veröffentlichen“, damit sich nicht nur
   Test-Nutzer anmelden können.
3. „APIs & Dienste“ → „Anmeldedaten“ → „Anmeldedaten erstellen“ → „OAuth-Client-ID“ →
   Anwendungstyp **Webanwendung**.
4. Unter „Autorisierte Weiterleitungs-URIs“ eintragen:
   `https://forge.example.com/api/auth/oauth/google/callback` (mit deiner Forge-Domain).
5. Google zeigt **Client-ID** und **Clientschlüssel**. Die Client-ID kommt in `forge-web.toml`:

   ```toml
   [auth.providers.google]
   client_id = "1234-abc.apps.googleusercontent.com"
   ```

   Den Clientschlüssel trägst du in `.env` als `GOOGLE_SECRET=...` ein.
6. Neu starten: `docker compose up -d`. Auf der Anmeldeseite erscheint „Weiter mit Google“.

Ein Google-Konto wird mit einem bestehenden Forge-Konto nur verbunden, wenn Google die E-Mail
bestätigt hat und das Forge-Konto dieselbe bestätigte E-Mail hat. Sonst verknüpft man es selbst in
den Einstellungen („Verknüpfte Anmeldungen“).

## 5. Anmeldung mit GitHub einrichten (optional)

1. Auf GitHub: Profilbild → **Settings** → **Developer settings** → **OAuth Apps** →
   **New OAuth App**.
2. *Homepage URL*: `https://forge.example.com`,
   *Authorization callback URL*: `https://forge.example.com/api/auth/oauth/github/callback`.
3. Nach dem Anlegen: **Client ID** kopieren und **Generate a new client secret** klicken.
4. In `forge-web.toml`:

   ```toml
   [auth.providers.github]
   client_id = "Ov23li..."
   ```

   und in `.env`: `GITHUB_SECRET=...`. Danach `docker compose up -d`.

Dieselbe App dient auch dazu, **private Repositories** zu klonen und zu pushen: In den Einstellungen
unter „Git-Zugang“ auf „GitHub für Repositories verbinden“ klicken. GitHub fragt dann zusätzlich die
Berechtigung `repo` an. Der Schlüssel dafür bleibt verschlüsselt auf dem Server und gelangt nie in
die Container der Projekte. Statt GitHub-Anmeldung geht auch ein persönliches Zugangs-Token
(„Fine-grained token“) im selben Abschnitt.

Andere Anbieter (Microsoft Entra, GitLab, Keycloak …) gehen über OpenID Connect, siehe `README.md`.

---

## 6. Weg B: Forge Web ohne Docker für den Server

Die Projekte laufen weiterhin in Docker-Containern, also muss Docker installiert sein. Nur der Server
selbst läuft als normaler Dienst.

### Linux (systemd)

```bash
sudo useradd --system --create-home --home-dir /var/lib/forge-web --groups docker forge-web
sudo mkdir -p /etc/forge-web /opt/forge-web
sudo python3.12 -m venv /opt/forge-web
# Die drei Wheels aus dem Release (forge, forge_sandbox, forge_web):
sudo /opt/forge-web/bin/pip install ./forge-*.whl ./forge_sandbox-*.whl ./forge_web-*.whl
sudo cp deploy/forge-web.example.toml /etc/forge-web/forge-web.toml
sudo cp deploy/forge-web.service /etc/systemd/system/
```

In `/etc/forge-web/forge-web.toml` mindestens eintragen:

```toml
[server]
public_url = "https://forge.example.com"

[preview]
domain = "meine-vorschau.de"
```

Geheimnisse kommen in `/etc/forge-web/forge-web.env` (nur für root lesbar, `chmod 600`):

```bash
FORGE_WEB_GOOGLE_SECRET=...
FORGE_WEB_GITHUB_SECRET=...
```

Starten und den Link für das erste Admin-Konto ansehen:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now forge-web
sudo journalctl -u forge-web | grep setup
sudo -u forge-web FORGE_WEB_DATA_DIR=/var/lib/forge-web \
  FORGE_WEB_CONFIG=/etc/forge-web/forge-web.toml /opt/forge-web/bin/forge-web doctor
```

Für HTTPS installierst du Caddy (`sudo apt install caddy`), kopierst `deploy/Caddyfile` nach
`/etc/caddy/Caddyfile` und setzt für den Caddy-Dienst die Umgebungsvariablen `FORGE_DOMAIN`,
`PREVIEW_DOMAIN`, `ACME_EMAIL` und `FORGE_UPSTREAM=127.0.0.1:8420` (mit
`sudo systemctl edit caddy`, Abschnitt `[Service]`, Zeilen `Environment=...`).

### macOS (launchd)

Docker Desktop muss installiert sein und laufen. Python 3.12 und die Wheels wie oben in z. B.
`/usr/local/opt/forge-web` installieren, dann `deploy/com.forge.web.plist` anpassen (Pfade) und:

```bash
cp deploy/com.forge.web.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.forge.web.plist
```

### Windows (WinSW)

Am zuverlässigsten ist **WSL2 mit Ubuntu** und darin Weg A. Wer Forge Web direkt unter Windows
betreiben will: Python 3.12 installieren, ein venv in `C:\forge-web\venv` anlegen, die Wheels
installieren, [WinSW](https://github.com/winsw/winsw) als `forge-web-winsw.exe` neben
`deploy\forge-web-winsw.xml` legen und

```powershell
.\forge-web-winsw.exe install
.\forge-web-winsw.exe start
```

Docker Desktop braucht unter Windows einen angemeldeten Benutzer; für einen Server ohne Anmeldung
ist deshalb WSL2 mit Docker Engine besser.

---

## 7. Sichern und aktualisieren

**Was gesichert werden muss:**

- Das Daten-Volume `forge-web_forge-web-data` (Datenbank, Konfiguration) – darin die Datei
  `secret.key`. **Ohne diese Datei sind gespeicherte API-Schlüssel und Tokens verloren.**
- Die Projekt-Volumes `forge-web-<projekt>-workspace` (der Code der Projekte).

```bash
docker run --rm -v forge-web_forge-web-data:/data -v "$PWD":/backup busybox \
  tar czf /backup/forge-web-data.tgz -C /data .
```

**Aktualisieren** (Weg A):

```bash
git pull                      # neue compose.yaml und Vorlagen
docker compose pull           # neue Images (bzw. neu bauen, siehe 2.4)
docker compose up -d
```

Die Datenbank wird beim Start automatisch auf den neuen Stand gebracht.

---

## 8. Wenn etwas nicht geht

| Problem | Lösung |
| --- | --- |
| Die Seite lädt nicht, Zertifikatsfehler | DNS-Einträge prüfen (`nslookup forge.example.com`), Ports 80/443 offen? `docker compose logs caddy` |
| „The sandbox image … is missing“ | Sandbox-Image ziehen oder bauen (2.4), `SANDBOX_IMAGE` in `.env` prüfen |
| „The Docker daemon is not reachable“ | `DOCKER_GID` in `.env` stimmt nicht; neu ermitteln (2.3) |
| Vorschau zeigt „This preview is private“ | Vorschau aus Forge heraus öffnen (Tab „Vorschau“). In Safari ggf. „In neuem Tab öffnen“ nutzen |
| Vorschau lädt nicht | Wildcard-DNS `*.meine-vorschau.de` prüfen, `forge-web doctor` |
| Anmeldung mit Google/GitHub fehlt | Client-Geheimnis in `.env` gesetzt? `forge-web doctor` zeigt es |
| Admin kommt nicht in die Verwaltung | Zwei-Faktor-Anmeldung in den Einstellungen einschalten |
| Zwei-Faktor-Handy verloren | Wiederherstellungscode nutzen, oder ein anderer Admin schaltet es unter „Nutzer“ aus |

Logs ansehen: `docker compose logs -f forge-web`.

---

## 9. Sicherheit in Kürze

- **Der Docker-Zugang ist so mächtig wie root.** Forge Web braucht ihn, um Projekt-Container zu
  starten. Betreibe Forge Web deshalb auf einem eigenen Server (oder einer eigenen VM), auf dem
  sonst nichts Wichtiges läuft.
- **gVisor** (`runsc`) macht die Container noch sicherer; `forge-web doctor` sagt, ob es installiert
  ist. Anleitung: <https://gvisor.dev/docs/user_guide/install/>.
- Die **Vorschau-Domain** soll eine eigene Domain sein (siehe Abschnitt 1).
- **Zwei-Faktor-Anmeldung** für Admins einschalten (Standard in der Beispiel-Konfiguration).
- **Docker-Daten auf eine eigene Partition** legen (`data-root` in `/etc/docker/daemon.json`).
  Forge Web misst den Platz der Projekte alle paar Minuten und stoppt Projekte über ihrer Quote;
  dazwischen kann ein Projekt kurz mehr schreiben. Auf einer eigenen Partition legt eine volle
  Platte dann nie den Server selbst (und seine Datenbank) lahm.
- **Cloud-Metadaten sperren**: Git-Aufträge (Klonen, Push, Pull) laufen in kurzen Containern im
  normalen Docker-Netz. Auf Cloud-Servern den Zugriff aus Docker-Netzen auf `169.254.169.254` und
  auf interne Dienste des Hosts per Firewall sperren, z. B.
  `iptables -I DOCKER-USER -d 169.254.169.254 -j DROP`.
- Lokale Modell-Server (Ollama, LM Studio, vLLM) bietet Forge Web nur an, wenn ein Admin ihre
  Adresse unter `[gateway.upstreams]` einträgt, z. B. `ollama = "http://gpu-rechner:11434/v1"`.
- Regelmäßig **sichern** (Abschnitt 7) und **aktualisieren**.
- Mehr dazu: `docs/SECURITY.md`.
