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
- What the server keeps for a container is limited in bytes, not only in count: live chat state
  (16 streaming replies and 16 tool outputs, 100 000 characters each, ids of at most 64 characters),
  open requests (50), frames that need no credit (a peer that stops reading is cut off), requests
  handled at once (the rest waits unread in the peer's window), rows waiting for the database, and
  stored history (one item at most 512 KiB, a project at most `quotas.chat_log_mb`). Browsers that
  fall behind by 32 MiB are disconnected and catch up from their last item; replays wait for the
  browser instead of piling up.

### Macs (Apple builds)

A Mac worker runs projects' code (build scripts, tests, the app itself) and is a boundary of its own:

- Each project builds in a macOS VM of its own (Tart), cloned from a prepared image and deleted after
  10 minutes without a job; projects never share a VM, and nothing secret is in the image. With
  `softnet` (the default) a VM reaches the internet but not the Mac's own network. Direct mode builds
  on the Mac itself and is meant only for CI (GitHub's Macs cannot run VMs) and for projects the
  Mac's owner trusts; `docs/EINRICHTUNG.md` (section 9) says so too.
- The image gets Forge's own wheels by file name: PyPI has an unrelated package called "forge" with
  higher version numbers, which a name-based install would pick.
- The worker connects out; it signs in with a token admins create (shown once, stored as a SHA-256
  hash) and sees only the jobs it took, while they run. Mac routes refuse browser sessions.
- The server never unpacks a project or an archive: the sandbox sends a tar.gz (size-limited), the
  Mac's VM unpacks it with tar's `data` filter (no paths or links that leave the project), and what
  comes back is checked: results are validated and size-limited, screenshots must be PNGs of at most
  12 MB, archives are size-limited and kept under their job id; a path the Mac reports is never used.
- Who may build is the server's decision (`apple.enabled`, `apple.allowed`, a grant per user, Mac
  minutes per month), checked for every job with the chat's run token, which works only while its
  chat runs.
- A user's App Store Connect team key is encrypted with the vault (like model keys) and never shown
  again; only the server signs with it (short-lived ES256 JWTs). It never goes to a sandbox, a Mac
  or a VM: signing certificates and profiles are made on the server, and only what one export needs
  goes to a fresh VM (W22b).
- Guideline reviews, screenshots and Forge's approval question come from the project's sandbox and are
  shown as they came (review links only to apple.com pages). An approval is only the user's own
  answer "Ready for Apple" to that question, recorded by the server once Forge took it (a second answer
  to the same question is refused), with who gave it, when, the project's commit and whether anything
  was uncommitted, and written to the audit log. Opening the approval page approves nothing. The App
  Store step (W22) works from that commit and asks the user again before anything goes to Apple.

### Containers

One container per project, made by `containers/docker.py`:

- `--network none`: no network at all. The only ways out are channels over the `docker exec` pipe: the
  gateway (models), the egress proxy (package registries) and nothing else.
- `--cap-drop ALL` plus the few capabilities the daemon needs to start programs as the `forge` user
  (uid 1000), `no-new-privileges`, read-only root file system, `tmpfs` for `/tmp`, `--pids-limit`,
  `--memory`, `--cpus`, `nofile` limit, `--pull never` (a missing image is an error, never a download).
- gVisor (`runsc`) is used when it is installed (`forge-web doctor` says whether it is).
- Disk use per project (`quotas.project_disk_mb`) is measured by Docker itself, outside the sandbox,
  every `quotas.disk_check_minutes`; a project over its quota has its container stopped. Uploads and
  messages are refused when the larger of that number and the sandbox's own is over the quota.
  Between two measurements a project can write more, so Docker's data belongs on a partition of its
  own (or on XFS with project quotas), where a full disk never stops the server.
- Idle containers stop after `sandbox.idle_minutes`. Volumes stay until the project is deleted.
- Git jobs that need a token (clone, push, pull) run in a throwaway container on the project's volume as
  uid 1000 with `core.hooksPath=/dev/null`, `core.fsmonitor=` and `protocol.file.allow=never`; the token
  comes in through a credential helper on stdin and is never written to `.git/config`. The agent itself
  cannot push; people push with the button in the Changes panel. Git jobs use Docker's default network
  (only the fixed script runs, against the one checked host): on cloud servers, block that network from
  the metadata address and the host's own services (`EINRICHTUNG.md`, section 9).
- A server folder opened as a project (admins only, under `sandbox.folder_roots`) may not have a
  comma or quote in its path, so it cannot add options to Docker's `--mount`.
- Forge's own configuration inside the container is written by the server. Projects stay "untrusted" for
  Forge, so hooks and MCP servers from a cloned repository never run.

**The Docker socket is root on the host.** Whoever can talk to it owns the machine, and the server needs
it. Run Forge Web on a machine (or VM) of its own, as its own service user; rootless Docker or a socket
proxy that only allows the calls in `containers/docker.py` lowers the damage of a server bug further.
`compose.yaml` mounts the socket into the server container for this reason only.

### Keys and tokens

- The server's LLM keys, people's own keys and GitHub tokens are encrypted at rest with the vault
  (`vault.py`, Fernet; the master key is a file in the data folder, readable by the service user
  only and never in the database, so a stolen database backup reveals no secret). None of them ever
  enters a container.
- A chat gets a **run token** bound to the user and the chat, sent to the worker in its start message
  (not its environment). It works only while a run that a person started here (a message or an
  answer) goes on, and at most `gateway.run_minutes` after the last one: the sandbox's own word that a
  chat runs is not enough. The gateway accepts it only for the model paths it knows (messages, chat
  completions, responses, generateContent), only for allowed models, swaps in the real key header,
  reserves the cost before the call and settles it from the upstream's final usage report. A stream
  that breaks off before that report is charged the larger of what was reported so far and what the
  text that came through (tool-call arguments and thinking included) amounts to. Monthly limits apply
  per user and server-wide.
- Local model servers (Ollama, LM Studio, vLLM) need no key and their presets point at `localhost`,
  which from the gateway is the server itself: they are offered only when an admin names their
  address in `gateway.upstreams`.
- The egress proxy only connects to the hosts in `egress.allow` (package registries and code hosts by
  default; `CONNECT` for HTTPS, absolute URLs for plain HTTP). It resolves the name itself and connects
  to that address only if it is public, so DNS tricks cannot point an allowed name at loopback, private,
  link-local or metadata addresses. Behind an `HTTPS_PROXY` of its own, the server hands that proxy only
  names it cannot resolve itself, never one that resolves to a private address.

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
  top-level `GET` navigations, and checks membership again every 30 seconds.
- Caddy asks `/api/preview/allowed-host` before it gets a certificate. The answer is yes only for a
  preview of an existing project that a member opened in the last ten minutes, and for at most 20
  ports per project a day, so nobody can make the server use up the certificate authority's limits.

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
browser, an outsider, a viewer and a non-admin, and fails when a new route has no rule. W17 added
two reviews of the whole code:

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
   path and header allow lists, and the egress address checks. Fixed (W17c):
   - a stream broken off after Anthropic's first event was charged one output token, and the estimate
     for broken-off streams ignored tool-call arguments and thinking;
   - the gateway believed the sandbox when it said a chat still ran, so a run token could outlive
     its run;
   - live chat state, frames that need no credit, requests handled at once, replays to slow browsers,
     rows waiting for the database and stored chat history had no limit in bytes;
   - the disk quota relied on the sandbox's own numbers;
   - with an upstream `HTTPS_PROXY`, the egress proxy handed over names that resolve to private
     addresses;
   - local model server presets reached the server's own `localhost`;
   - a server folder path with a comma could add options to Docker's `--mount`;
   - `/api/preview/allowed-host` said yes for any port of an existing project.

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
- **A chat in someone else's project runs in their sandbox.** Its owners can read it and, while it
  runs (at most `gateway.run_minutes` after the last message or answer), use the model access it
  pays for. Add people only to projects of people they trust, and stop a chat to end its run.
- **Disk use between two measurements**: a project can write more than its quota for up to
  `quotas.disk_check_minutes` before its container stops (see Containers).
- **Git jobs on Docker's default network**: a hole in git itself would reach what that network
  reaches; block the metadata address and host services there (see Containers).

## Reporting

Please report security problems privately to the server's operator, or for Forge Web itself as a
GitHub security advisory on the repository, not as a public issue.
