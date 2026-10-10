# Die Produkt-Fabrik – „Sag, was du willst; Forge baut, hostet und veröffentlicht es“

## Context

Der Nutzer (Betreiber/Admin von Forge Web) will, dass ein zahlender Kunde nur noch sagt, *was* er will
(„ein Instagram“, „ein Windows-Programm“, …). Forge baut daraus ein **komplettes Produkt** mit einer
sauberen, üblichen Struktur, an der auch Menschen weiterprogrammieren können: Server, Datenbank (Schema,
Migrationen), die nötigen Apps (Web; native Apple-Apps über den fertigen Apple-Weg; später Android und
Windows), Doku, Tests, CI. Danach wird es **automatisch auf den Linux-Servern des Admins gehostet**
(Docker) – erst nach Prüfung durch einen **unabhängigen Agenten** und Freigabe durch **den Ersteller und
dann den Admin** (vertrauenswürdige Nutzer kann der Admin von seinem Klick befreien) – und in die Stores
gestellt. **Bezahlt wird doppelt:** Kunden zahlen den Admin (Abo, pro App, Hosting pro Monat – alles vom
Admin einstellbar), und die erzeugten Apps können selbst Geld von ihren Nutzern nehmen.

Ausgangslage: Forge-Kern (`src/forge`, Schritte S01–S62) und Forge Web (`forge-web/`, W01–W23, Apple-Weg
bis App Store fertig, CI-Lauf 50 grün). Wiederverwendet werden die erprobten Muster des Apple-Wegs:
unabhängiger Prüfer (`src/forge/apple_review.py`), Kontrollpunkte mit Nutzerfreigabe
(`src/forge/apple_flow.py`), Vorlage (`apple_template.py`), Worker, der sich selbst verbindet
(`forge-web/packages/macworker`), gespeicherte Zustandsmaschinen (`forge_web/apple/release.py`,
`submission.py`), Freigabe an einen Commit gebunden (`apple/approvals.py`), `git.archive`, Vault, Audit,
Attrappen externer APIs in Tests (`tests/asc_standin*.py`).

## Dieser Plan ist für einen Claude-Code-Agenten

**Schritt 0 (vor allem anderen):** Dieser Plan liegt als `docs/PRODUKTFABRIK.md` im Repo. Der erste Agent
trägt die Kern-Karten (S63 ff.) als „Phase 8 · Produkt-Fabrik“ in `docs/STEPS.md` ein (auf `main`) und die
Forge-Web-Karten (W24 ff.) als „Phase D–G“ in `forge-web/docs/STEPS.md` (auf dem Branch) – auf Englisch wie
die übrigen Karten –, setzt „Next step“ in beiden `PROGRESS.md`, committet und pusht. Danach reicht als
Auftrag an einen Agenten:

```text
Read AGENTS.md, PROGRESS.md and docs/PRODUKTFABRIK.md. Do the next step from docs/STEPS.md
(core) or forge-web/docs/STEPS.md (Forge Web) exactly as AGENTS.md says: plan, tests first,
implement, gate + verify, PROGRESS.md, one commit, push, check CI. Report in German, 5 lines.
```

**Regeln, die immer gelten** (zusätzlich zu `AGENTS.md` und `forge-web/AGENTS.md`):
- Kern-Schritte (S…) auf `main` (eigener Worktree), Forge-Web-Schritte (W…) auf
  `claude/elegant-tesla-h2iugo`; Forge Web ändert nie `src/`. Der Abgleich-Workflow holt `main` in den Branch.
- Tests rufen **nie** echte Dienste (Stripe, Apple, Google, Microsoft, LLMs): jede externe API bekommt eine
  Attrappe in `forge-web/tests/*_standin*.py`, die Signaturen/Regeln des echten Dienstes prüft.
- Keine neuen Python-Pakete ohne Rückfrage. Stripe, Play, Store-APIs über `httpx` + `cryptography` + `hmac`.
  Pakete *in den erzeugten Apps* (FastAPI, React …) sind Teil der Vorlage, nicht von Forge – Versionen pinnen.
- Vorlagen-Dateien liegen als Paketdaten `src/forge/templates/<art>/**.tmpl` (sonst prüfen ruff/mypy
  fremden Code); ihre eigenen Tests laufen unter dem pytest-Marker `fullstack` in einem eigenen CI-Job.
- Nach jedem Push die CI lesen und bis grün nachbessern; ein Push bricht laufende Läufe ab – lange
  Mac-Jobs vorher abwarten.

## Grundentscheidungen (Annahmen – bei Freigabe änderbar)

1. **Betreiber jeder gehosteten App ist ihr Ersteller** (Kunde); der Admin ist Hosting-Anbieter und
   Auftragsverarbeiter (AV-Vertrag). Impressum/Datenschutz der App kommen aus Daten, die der Ersteller in
   Forge Web einträgt.
2. **Verbraucher und Firmen** als Kunden – deshalb von Anfang an Button-Lösung (§312j BGB),
   Kündigungsbutton (§312k), Widerrufsverzicht für digitale Inhalte, Rechnungen mit MwSt. (Stripe Tax/OSS).
3. **Zahlungen der Apps:** Stripe Connect (Express) mit einer Plattform-Gebühr, die der Admin festlegt; der
   Stripe-Schlüssel des Admins kommt nie in eine App (Zahlungs-Relay in Forge Web). iPhone-Apps mit
   digitalen Inhalten nutzen Apples In-App-Kauf (Richtlinie 3.1.1).
4. **Ein Postgres-Container pro App** (Trennung, einfaches Backup und Löschen).
5. **Ein Hosting-Server mit Wildcard-DNS** zuerst, mehrere später; **eigene Apps-Domain** (getrennt von
   Forge und den Vorschauen, in die Public Suffix List eingetragen).
6. **Stack der erzeugten Produkte** (Standard, für Menschen gut weiterzuentwickeln): Server Python 3.12 +
   FastAPI + SQLAlchemy 2 + Alembic + PostgreSQL 16; Web React + Vite + TypeScript mit typisiertem Client aus
   OpenAPI; Apple Swift/SwiftUI (vorhanden); Android Kotlin + Jetpack Compose; Windows .NET 10 LTS +
   WinUI 3 (MSIX). **Android vor Windows** (baut unter Linux, billiger).

## Architektur in einem Bild

```
Kunde ──▶ Forge Web (Chat, Vorschau, Freigaben, Abrechnung) ──▶ Sandbox des Projekts (Forge-Kern baut:
          Vorlage → Blueprint → Code → `forge app check` → Prüfer → „Bereit zum Livegang?“)
          │  fester Commit + bestandene Prüfung + Ersteller-Freigabe + Admin-Freigabe
          ▼
     Deploy-Zustandsmaschine (Forge Web) ── DeployPlan (nur feste Runtime-Images, Limits, Namen von Secrets)
          ▼  (Host-Worker verbindet sich selbst, langes Polling wie der Mac-Dienst)
     forge-host-worker auf dem Linux-Server: Build in Wegwerf-Container (gVisor) → Tests/Migrationen erneut →
     pg_dump → Migration → Umschalten → Health-Check (sonst Rollback); Caddy mit HTTPS je App
```

**Sicherheitsgrundsätze** (der Code der Apps ist fremder Code):
- Gehostete Apps laufen **nie** auf dem Forge-Web-Server (dort liegen Docker-Socket und Vault-Schlüssel).
- `runsc` (gVisor) ist **Pflicht**, dazu Nur-Lesen-Root, `cap-drop ALL`, Nutzer-Namespaces, Limits, ein
  Netz pro App, Firewall (`DOCKER-USER`): keine privaten Netze, keine Cloud-Metadaten, kein Port 25, keine
  anderen Apps; Raten gegen Scans/Mining.
- Der Host-Worker führt **nie** `docker-compose.yml`/`Dockerfile` der App aus; er bekommt einen fertig
  aufgelösten `DeployPlan`. Builds (mit `npm install`-Skripten) nur in Wegwerf-Containern ohne Secrets, die
  nur Paket-Registries erreichen.
- Ergebnisse aus der Sandbox sind nicht vertrauenswürdig: der Host prüft den gepackten Commit selbst
  (Tests, Migrationen) bevor der Admin „Freigeben“ sieht. Der LLM-Prüfer berät nur; die harten Tore sind
  die festen Prüfungen und die zwei Menschen. Die Befreiung „vertrauenswürdig“ spart nur den Admin-Klick.
- Secrets werden pro Host-Schlüssel (X25519, `cryptography`) verschlüsselt und je Deploy einmal abgeholt.
- Mail und Zahlungen der Apps nur über Relays von Forge Web (Kontingente, ein Token je App).
- Impressum, Datenschutz und „Inhalt melden“ liefert Caddy unter festen Pfaden aus – die App kann sie nicht
  entfernen.

## Phase 8 (Kern, `main`) – das Produkt bauen

- [ ] **S63 — App manifest** · Contracts: AppManifest
  - Files: `src/forge/app_manifest.py`, `docs/CONTRACTS.md`, `tests/test_app_manifest.py`
  - Build: pydantic model of `forge.app.toml`: services (runtime `python3.12|node22|static`, command, port,
    health path), database, storage, mail, env and secret *names* (never values), resource class, clients,
    payments; problems as values.
  - Tests: valid/invalid manifests; a secret with a value is refused; unknown runtime; duplicate ports.
  - Verify: `uv run pytest tests/test_app_manifest.py -q`
- [ ] **S64a — Full-stack template: server, docs, CI** · `src/forge/app_template.py`,
  `templates/fullstack/{server,docs,.github}/**.tmpl`, `cli.py` (`forge app new`), packaging data.
  FastAPI + SQLAlchemy + Alembic + Postgres; accounts (password reset, account deletion – Apple 5.1.1(v),
  GDPR export), storage and mail interfaces, rate limiting, „report content“ + moderation queue (DSA),
  `__Host-` cookies, `/healthz`, OpenAPI snapshot test, Dockerfile for humans, CI with Postgres, README,
  runbook, expand/contract migration guide. Tests: placeholders filled, every `.py` compiles, TOML/YAML/JSON
  parse, manifest valid; under `fullstack` the server's own tests run.
- [ ] **S64b — Full-stack template: web** · `templates/fullstack/web/**.tmpl`, `docker-compose.yml.tmpl`.
  React + Vite + TS, typed client from the OpenAPI snapshot, `/api` proxied to the backend (one preview port),
  account pages, links to `/impressum` and `/datenschutz`, vitest. Under `fullstack`: `npm ci && npm test &&
  npm run build`.
- [ ] **S65 — Run and check an app** · `src/forge/app_dev.py`, `app_checks.py`, `runtime/postgres.py`,
  `cli.py` (`forge app dev|check`), `tools.py` (`app_check`). `dev`: throwaway Postgres (`initdb`/`pg_ctl`,
  socket in `/tmp`) + services as monitors. `check`: fixed gates → `.forge/out/app/checks.json` (tests on
  Postgres, `alembic upgrade head` without pending autogenerate, OpenAPI drift, web build, lockfiles, secret
  scan). Tests with fake `pg_ctl`/`npm`/`pytest`; every failed gate names its fix.
- [ ] **S66 — Release reviewer with rulebooks** · Contracts: `ReleaseReview`, `ReleaseFinding` (area as
  string) · `src/forge/release_review.py`, `prompts.py`, `config.py` (role `release_reviewer`,
  `[release] rulebook_files`), `events.py`. Like `apple_review.py` (own role, fresh context, read-only, JSON,
  worst finding wins, failed review never passes). Rulebooks: hosting/AUP, privacy/GDPR, German law
  (Impressum, consumer law), content/DSA (UGC needs report/block), security, resources. Extra rules from the
  admin's files. The Apple reviewer stays unchanged.
- [ ] **S67a — Checkpoints as plug-ins** · `src/forge/release_flow.py`, `pipeline.py`, `apple_flow.py`.
  A `Checkpoint` protocol (request, plan, product); Apple becomes its first user, behaviour unchanged
  (`pipeline.py` is near 500 lines – extract first). Apple pipeline tests stay green untouched.
- [ ] **S67b — App checkpoints and „Ready to go live“** · `src/forge/app_flow.py`, `pipeline.py`
  (`Report.ready_to_host`), `cli.py` (`--app`). Product review only after a green `app check`, with desktop and
  mobile screenshots; question GO_LIVE / SEND_BACK / NOT_YET; only the user approves.
- [ ] **S68 — Product blueprint** · `src/forge/blueprint.py`, `prompts.py` (architect role). From the refined
  request: entities, API, auth, storage, clients, payments, hosting needs → `.forge/out/product/blueprint.json`
  + `docs/architecture.md` in the product; one group of plan steps per component; the plan review reads it.

## Phase D (Forge Web) – hosten

- [ ] **W24 — Full-stack projects** · `docker/sandbox.Dockerfile` (+ `postgresql`, Chromium),
  `forge_web/projects.py`, `forge_web/apps/template.py`, `NewProjectDialog.tsx`. Kind `app`, chats with `--app`,
  preview shows the web port; under `docker` marker `forge app check` passes inside the image.
- [ ] **W25a — Host worker: run a release** · new package `packages/hostworker/forge_hostworker/{wire,client,
  build,runner,postgres,cli}.py` (`forge-host-worker`), copy of the Mac-worker pattern. Receives a `DeployPlan`;
  builds in throwaway runsc containers; runs apps only with runsc; one Postgres + one network per app; health
  checks; keeps the previous release for rollback. Tests with a fake `docker` binary; refuses to run without runsc.
- [ ] **W25b — Host edge and firewall** · `forge_hostworker/{edge,firewall,doctor}.py`, `deploy/host/*`. Caddy
  on-demand TLS asks the worker (only live apps); legal pages and report link served by the edge;
  `DOCKER-USER` rules; `forge-host-worker doctor`. German guide `docs/EINRICHTUNG.md` §11 (server, gVisor,
  DNS, Apps-Domain, token).
- [ ] **W26 — Deploy state machine** · `forge_web/hosting/{deploy,deploy_api,hosts,plan}.py`, migration `0012`.
  One stored deploy per environment (**staging**, then production): pack approved commit → host checks →
  creator approval → admin approval (skipped for trusted users) → sealed secrets → `pg_dump` → migrate →
  switch → health → live URL; resumes after restart, never migrates twice, rolls back on failed health.
  `test_access.py` rules for every new route.
- [ ] **W27 — Go-live page and admin hosting panel** · `frontend/src/pages/{GoLive,AdminHosting}.tsx`,
  `forge_web/hosting/admin.py`. Creator: reviews, checks, staging link, „Live schalten“. Admin: hosts + tokens,
  approval queue (shows the checked commit), trusted users, suspend, logs.
- [ ] **W28 — App services: mail, legal pages, abuse** · `forge_web/hosting/{mail_relay,legal_pages,abuse}.py`,
  migration `0013`. Mail relay per app with quotas; operator data (Impressum) required before go-live; DSA
  notice form → admin review → suspension with statement of reasons.
- [ ] **W29 — Operations** · `forge_hostworker/{backup,metrics}.py`, `forge_web/hosting/{ops,gdpr}.py`. Nightly
  encrypted off-host backups + restore drill, metering per app (CPU, RAM, disk, traffic), mail alerts, export
  and full deletion of an app.
- [ ] **W30 — Custom domains** · `forge_web/hosting/domains.py`. DNS TXT check before TLS for a customer domain.

## Phase E (Forge Web) – Abrechnung: Kunden zahlen den Admin

- [ ] **W31 — Stripe client and webhooks** · `forge_web/billing/{stripe_client,webhooks,events}.py`, migration
  `0014`, `tests/stripe_standin*.py`. httpx client with idempotency keys; webhook HMAC + timestamp window, each
  event stored once and handled in order; keys in the vault; Stripe test mode first.
- [ ] **W32 — Catalog (Admin)** · `forge_web/billing/catalog.py`, `frontend/src/pages/AdminBilling.tsx`. Plans
  (monthly/yearly), one-time price per app, hosting per app and month, add-ons, trials, coupons; synced to
  Stripe (twice changes nothing; removed plan archived).
- [ ] **W33a — Checkout and entitlements** · `forge_web/billing/{checkout,entitlements}.py`, `quotas.py`,
  `gateway/meter.py`, `frontend/src/pages/Billing.tsx`. Stripe Checkout (no card data on the server), order
  button text per §312j, withdrawal waiver for digital content (box + mail confirmation), Stripe Tax. One
  entitlement engine sets limits: projects, AI budget per month **and per product** (an „Instagram“ can cost a
  lot of tokens), Mac minutes, hosted apps, storage, custom domains, store publishing.
- [ ] **W33b — Cancellation and dunning** · `forge_web/billing/{cancel,dunning}.py`. §312k cancellation
  button (two steps + confirmation mail), Stripe Customer Portal; unpaid → grace period → suspend → delete
  after notice and export.
- [ ] **W34 — Legal setup** · `forge_web/legal.py`, migration `0015`. Versioned AGB, Datenschutz, AV-Vertrag
  (with TOMs and subprocessors), AUP; every acceptance stored, new versions must be accepted again; EU VAT ID
  check (VIES). Texts come from the admin (lawyer/tax adviser) – Forge supplies only the mechanics.

## Phase F – Zahlungen in den Apps

- [ ] **W35 — Stripe Connect and payments relay** · `forge_web/billing/{connect,relay}.py`. Creators onboard
  as Express accounts; the relay acts for exactly one connected account per app token and takes the admin's
  application fee; the platform key never leaves the server.
- [ ] **S69 — Template payments module** · `templates/fullstack/server/payments/**.tmpl` (+ web pages):
  subscriptions/one-time via the relay, end-user cancellation page, fake relay in the product's own tests.
- [ ] **S70 — Apple client for the API** · `apple_template.py`, `app_template.py`: Swift client from
  swift-openapi-generator against staging; StoreKit 2 when digital goods are sold (3.1.1).
- [ ] **W36 — Apple in-app purchases on the server** · `forge_web/apple/iap.py`, `asc_standin` extended:
  App Store Server Notifications v2 (JWS chain checked with `cryptography`), renewals/refunds update the app's
  entitlements. Connects the existing Apple path (W22) to the hosted backend.

## Phase G – weitere Plattformen

- [ ] **S71 / W37 — Android** · `src/forge/android_template.py` (Kotlin/Compose, Play Billing);
  `forge_hostworker/android.py` (Gradle build in a Linux container), `forge_web/stores/play.py` (Play Developer
  API, service-account JWT RS256), `tests/play_standin.py`. Release only after the review gate. Note: new
  personal Play accounts need a 14-day closed test with 12 testers; an organisation account (D-U-N-S) avoids it.
- [ ] **S72 / W38 — Windows** · `src/forge/windows_template.py` (.NET 10 WinUI 3, MSIX);
  `packages/winworker/` (Mac-worker protocol on a Windows machine), `forge_web/stores/msstore.py` (Partner
  Center submission API, Entra ID), `tests/msstore_standin.py`. The Store signs MSIX; direct download later with
  Azure Trusted Signing.

## Was nur der Nutzer liefern kann (der Agent fragt erst, wenn der Schritt es braucht)

- **Server:** mindestens ein Linux-Host für die Apps (getrennt von Forge Web) mit gVisor; später ein
  Windows-Rechner/VM für W38; Off-Site-Speicher für Backups.
- **Domains:** eigene Apps-Domain mit Wildcard-DNS (+ Eintrag in die Public Suffix List).
- **Konten:** Stripe (mit Connect und Steuer-Registrierungen), Mail-Anbieter mit eigener Absender-Domain,
  Google Play (Organisation empfohlen), Microsoft Partner Center + Entra-ID-App, Apple (vorhanden geplant).
- **Rechtstexte (Anwalt/Steuerberater):** AGB, Datenschutz, Impressum, AV-Vertrag, AUP, Widerrufsbelehrung,
  DSA-Kontaktstelle; Kleinunternehmer/OSS, E-Rechnung (B2B ab 2027/28).
- **Entscheidungen:** die sechs Grundentscheidungen oben bestätigen oder ändern.

## Verifikation

- Je Schritt: Tests zuerst; Kern-Gate `uv run ruff check . && uv run mypy src && uv run pytest -q`;
  Forge-Web-Gate `uv run ruff check . && uv run mypy packages && uv run pytest -q` + UI `npm run typecheck &&
  npm test && npm run build`; der Verify-Befehl der Karte.
- Vorlagen: CI-Job `fullstack` baut und testet ein frisch erzeugtes Produkt (Server-Tests auf echtem Postgres,
  Web-Build, OpenAPI-Abgleich).
- Hosting: Ende-zu-Ende-Test mit Attrappen-Host (fake `docker`) in jedem Gate; ein CI-Job mit echtem Docker
  (+ runsc) deployt eine erzeugte App, ruft `/healthz` über Caddy und rollt einen absichtlich kaputten
  Release zurück.
- Abrechnung: Stripe-Attrappe prüft Signaturen, Idempotenz, Reihenfolge; ein Live-Test (Stripe-Testmodus,
  `pytest -m live`) nur mit Schlüsseln als GitHub-Secrets.
- Nach jedem Push die CI lesen und bis grün nachbessern.

## Reihenfolge und Nutzen

1. Phase 8 (S63–S68) → Forge baut komplette, prüfbare Produkte. 2. Phase D (W24–W30) → sie gehen nach zwei
Freigaben live. 3. Phase E (W31–W34) → Fremde können zahlen (erst wenn die Rechtstexte da sind). 4. Phase F →
Apps verdienen selbst Geld. 5. Phase G → Android, dann Windows. Jede Phase ist für sich nutzbar.
