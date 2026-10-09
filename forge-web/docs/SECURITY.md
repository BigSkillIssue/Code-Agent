# Forge Web security

Who may do what, where the boundaries are, what was checked, and the risks that are accepted on purpose.
Read this before changing sign-in, the gateway, containers, previews or the files API.

## Threat model

Forge Web is built for **people you do not trust**: anyone who can sign up may be hostile. They have a
terminal and an AI agent inside their project, so they control everything in their container,
including the sandbox daemon itself. They must not be able to:

- reach the host, other containers, the server's internal services, the local network or cloud metadata;
- read or change projects, chats, files or terminals of people who did not add them;
- get the server's LLM keys, other people's keys, git tokens or the vault's master key;
- spend more than their limits on the server's keys;
- touch files on the host (outside a server folder an admin opened for them on purpose);
- attack other people's browsers through a live preview, or the main app through a file or chat;
- take the server down with oversized or endless data from their container or browser.

The server operator and admins are trusted. The machine's Docker daemon is trusted.

## Boundaries

```
Browser ──(main origin: sessions, CSRF, origin checks)──▶ Server ──(docker exec pipe)──▶ Container
Browser ──(preview domain: own cookie, never the session)──▶ Preview proxy ──(connect channel)──▶ port
Container ──(forward channel)──▶ Gateway (run token)   Container ──(forward channel)──▶ Egress proxy
```

**The container is the only boundary between users.** Everything that comes out of it is hostile:

- Frames are length-checked (64 KiB) and credit-limited per channel; the control channel goes first, so
  a flooding terminal never stalls approvals or other chats (`docs/PROTOCOL.md`).
- Messages are validated (pydantic, `forge.events.parse_event`) and capped; tool output is cut before it
  is stored; a worker's own usage numbers are never believed (the gateway meters the upstream answers).
- Paths, names and numbers from a container are never used on the host. The server never opens project
  files on the host: files live in Docker volumes and every file operation runs in the daemon, which
  refuses paths that leave `/workspace` (also through symlinks).

### Containers

One container per project, made by `containers/docker.py`:

- `--network none`: no network at all. The only ways out are channels over the `docker exec` pipe: the
  gateway (models), the egress proxy (package registries) and nothing else.
- `--cap-drop ALL` plus the few capabilities the daemon needs to start programs as the `forge` user
  (uid 1000), `no-new-privileges`, read-only root file system, `tmpfs` for `/tmp`, `--pids-limit`,
  `--memory`, `--cpus`, `nofile` limit, `--pull never` (a missing image is an error, never a download).
- gVisor (`runsc`) is used when it is installed (`forge-web doctor` says whether it is).
- Disk use is limited per project (`quotas.project_disk_mb`); idle containers stop after
  `sandbox.idle_minutes`. Volumes stay until the project is deleted.
- Git jobs that need a token (clone, push, pull) run in a throwaway container on the project's volume as
  uid 1000 with `core.hooksPath=/dev/null`, `core.fsmonitor=` and `protocol.file.allow=never`; the token
  comes in through a credential helper on stdin and is never written to `.git/config`. The agent itself
  cannot push; people push with the button in the Changes panel.
- Forge's own configuration inside the container is written by the server. Projects stay "untrusted" for
  Forge, so hooks and MCP servers from a cloned repository never run.

**The Docker socket is root on the host.** Whoever can talk to it owns the machine, and the server needs
it. Run Forge Web on a machine (or VM) of its own, as its own service user; rootless Docker or a socket
proxy that only allows the calls in `containers/docker.py` lowers the damage of a server bug further.
`compose.yaml` mounts the socket into the server container for this reason only.

### Keys and tokens

- The server's LLM keys, people's own keys and GitHub tokens are encrypted at rest with the vault
  (`vault.py`, Fernet; the master key is a file in the data folder, readable by the service user
  only and never in the database, so a stolen database backup reveals no secret). None of them ever enters a container.
- A chat run gets a short-lived **run token** bound to the user, chat and run, sent to the worker in its
  start message (not its environment). It works only while its chat runs. The gateway accepts it only
  for the model paths it knows (messages, chat completions, responses, generateContent), only for
  allowed models, swaps in the real key header, reserves the cost before the call and settles it from
  the upstream's answer (also for streams that break off). Monthly limits apply per user and server-wide.
- The egress proxy only connects to the hosts in `egress.allow` (package registries and code hosts by
  default; `CONNECT` for HTTPS, absolute URLs for plain HTTP). It resolves the name itself and connects
  to that address only if it is public, so DNS tricks cannot point an allowed name at loopback, private,
  link-local or metadata addresses.

### Browser

- Sessions are random tokens stored hashed in the database, in an `HttpOnly`, `Secure`, `SameSite=Lax`
  `__Host-` cookie on HTTPS. Every changing request needs the CSRF header that matches the session;
  WebSockets check the `Origin` header.
- Open sockets are checked again every 30 seconds (`live_access.py`): a terminal closes when its user
  loses the editor role, and `/api/ws` closes when the session ends or stops delivering chats the user
  may no longer read.
- Chat Markdown is rendered without raw HTML; file contents and tool output are text, never markup.
- **Previews run on a separate domain** (`p<port>-<project>.<preview domain>`, never a subdomain of the
  app). Opening one hands out a one-time ticket (60 seconds) that becomes a `__Host-forge_preview` cookie
  (`Secure`, `HttpOnly`, `SameSite=None`, `Partitioned`) for that host only; the session cookie never
  reaches a preview. The proxy strips Forge's cookies from requests, rewrites `Host` and `Origin` to
  `localhost:<port>`, refuses requests from other sites (other previews included) unless they are
  top-level `GET` navigations, and checks membership again every 30 seconds. Caddy asks `/api/preview/allowed-host` before it gets a
  certificate, so only names of existing projects and ports get one.

### Accounts

- Passwords are hashed with argon2id (a few at a time, so a login flood cannot use up the memory);
  wrong email and wrong password take the same time and give the same answer.
- Sign-up modes: invite (default), approval by an admin, or open (optionally for some email domains).
  Open sign-up without working mail leaves new accounts waiting for an admin, because nobody can prove
  their address. Local isolation refuses strangers altogether.
- An email link confirms the address but never signs in: the account may have been made by someone else.
- Google and GitHub sign-ins join an existing account by email only when that account has a confirmed
  address and no password; otherwise its owner links the provider in their settings.
- A new password ends the person's other sessions and every reset link still open.
- `auth.passwords = false` turns off password sign-in, resets and "forgot password" entirely.
- Two-factor sign-in (TOTP, RFC 6238) with ten one-time recovery codes; codes are claimed with a
  compare-and-set, so a code works once. Turning it on ends the person's other sessions; an admin who
  turns it off for someone ends all of their sessions. `auth.admin_two_factor` makes it compulsory for
  admins.
- Rate limits (per IP, per email, per user): sign-in, sign-up, reset mails, provider sign-ins,
  two-factor codes, and adding project members by email (each try tells whether an address has an
  account).
- Sign-ins (also failed ones), changes of rights, members, keys, git tokens and server settings are in
  the audit log.

## What was checked

Each step had its own security tests; `tests/test_access.py` calls every API route as a signed-out
browser, an outsider, a viewer and a non-admin, and fails when a new route has no rule. W17 added two reviews of the whole code:

1. **Sign-in and accounts** (sessions, CSRF, OAuth, resets, invites, two-factor, admin rights).
   Fixed: accounts made with someone else's address could be taken over by the address's owner signing
   in with Google or GitHub, and the other way round; the email link started a session; open sign-up
   without mail let unconfirmed addresses in; `auth.passwords = false` still allowed password sign-in
   and resets; reset links outlived a password change; turning on two-factor left other sessions open;
   two-factor codes were limited per user only; open terminals and chat sockets outlived a removal from
   the project; "forgot password" answered faster for unknown addresses; adding members by email could
   be used to probe addresses without limit and added accounts whose address was never confirmed.
2. **Sandbox and proxies** (container arguments, protocol, files, git, ZIP, terminals, preview proxy,
   gateway, egress). Found sound: the container arguments, frame and credit limits, the files API
   (downloads are attachments with `CSP: sandbox` and `nosniff`), git URL and branch checks, the
   terminal and chat sockets, the preview proxy's host parsing, cookie and header rules, the gateway's
   path and header allow lists, and the egress address checks. Open findings (fixed in W17c):
   - a stream that breaks off after Anthropic's first event is charged almost no output;
   - the gateway believes the sandbox when it says a chat still runs, so a run token can outlive its
     run (a chat in someone else's project runs in their sandbox);
   - live chat state, protocol queues, replays to slow browsers and stored chat events have no limit
     in bytes, so a hostile sandbox can use up the server's memory or disk;
   - the disk quota relies on numbers from the sandbox, and volumes have no size limit;
   - with an upstream `HTTPS_PROXY`, the egress proxy hands over names it would refuse itself;
   - presets without a key (Ollama, LM Studio, vLLM) reach the server's own `localhost`;
   - a server folder path with a comma could add options to Docker's `--mount`;
   - git jobs run on Docker's default network;
   - `/api/preview/allowed-host` accepts any port, so one project can use up certificate limits.

## Accepted risks

- **Two admins demoting each other at the same moment** can leave the server without an active admin.
  `forge-web user add --admin` on the server's command line can always make a new one.
- **Guessing two-factor codes slowly**: 10 tries per 15 minutes per account and 30 per IP make a guess
  succeed only after months, and every failed try is in the audit log.
- **Safari and partitioned cookies**: some Safari versions block cookies in frames of another site even
  when they are partitioned; there the preview panel shows "This preview is private", and "open in a
  new tab" works.
- **Let's Encrypt limits**: every preview host needs its own certificate unless you use a wildcard
  certificate (DNS challenge). Many projects with many ports can hit the weekly limit; large servers
  should use a wildcard certificate for the preview domain.
- **What people do with their own container** (mining, attacking allowed hosts, pushing their code
  anywhere the egress list allows) is limited by CPU, memory, time and the egress list, not prevented.
- **A project's configuration is ignored by Forge** (untrusted): repository hooks and MCP servers do not
  run. People who want them configure them in their own Forge settings.

## Reporting

Please report security problems privately to the server's operator, or for Forge Web itself as a
GitHub security advisory on the repository, not as a public issue.
