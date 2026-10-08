# Forge Web wire protocol (version 1)

The server talks to each project's sandbox daemon over **one byte stream** (a `docker exec -i … forge-sandbox
attach` pipe, or a local socket in local mode). Many logical **channels** share that stream.
Code: `forge_sandbox/frames.py`, `protocol.py`, `mux.py`, `rpc.py`.

## Frames

```
+----------------+----------------+--------+------------------------+
| length (u32)   | channel (u32)  | type u8| payload (length bytes) |
+----------------+----------------+--------+------------------------+
```

Big endian; the header is 9 bytes; `length` ≤ 65 536. A larger length, an unknown type or a stream that ends
inside a frame is a protocol error and closes the connection.

| Type | Name | Payload |
| --- | --- | --- |
| 1 | DATA | raw bytes of a byte-stream channel (PTY, TCP forward, file transfer) |
| 2 | MESSAGE | the last (or only) part of one UTF-8 JSON object |
| 3 | MESSAGE_MORE | a part of a JSON object; more parts follow |
| 4 | OPEN | JSON `{"kind": str, "args": {...}}` — open the channel with this id |
| 5 | OPEN_OK | empty — the channel is open |
| 6 | OPEN_FAIL | JSON `{"code": str, "message": str}` |
| 7 | CLOSE | empty — the sender sends nothing more on this channel |
| 8 | CREDIT | u32 — the receiver may now send that many more payload bytes |

## Channels

- Channel **0** is the control channel; it exists from the start.
- The **server** opens odd ids (1, 3, 5, …), the **daemon** even ids (2, 4, …). Opening an id of the wrong
  parity, or one in use, is a protocol error. A side may refuse more than 256 open channels
  (`too_many_channels`).
- A channel is gone once both sides sent CLOSE. Frames that arrive for an unknown channel are ignored (they
  may still be in flight after a close).

## Flow control

Each side may send at most **WINDOW** (1 MiB) payload bytes per channel before the receiver grants more with
CREDIT frames. The receiver grants what its application has consumed — when that reaches half the window, or
whenever nothing is left to read. A peer that sends more than it was granted breaks the protocol and is cut
off. So a channel nobody reads (a stalled terminal, a flooding process) only blocks its own sender.

Write order: OPEN, OPEN_OK, OPEN_FAIL, CREDIT and every frame of the control channel go first; DATA, MESSAGE
and CLOSE of the other channels follow in the order they were sent (CLOSE stays behind its channel's data).

A JSON message is at most 1 MiB (the window); larger content (big files, archives) uses a byte-stream channel.

## Control channel

The first message each side sends is `hello`:

```json
{"type": "hello", "protocol": 1, "role": "daemon", "version": "0.1.0", "info": {"workspace": "/workspace"}}
```

A different `protocol` number fails the handshake (`ProtocolMismatch`); the server then recreates the
container from the current image (the project's volumes are kept).

After that the control channel carries calls in both directions:

```json
{"type": "request", "id": 7, "method": "fs.read", "params": {"path": "src/app.py"}}
{"type": "response", "id": 7, "ok": true, "result": {"text": "..."}}
{"type": "response", "id": 8, "ok": false, "error": {"code": "not_found", "message": "no such file"}}
{"type": "notify", "method": "procs.exited", "params": {"id": "p1", "code": 0}}
```

A handler failure that is not an expected error is answered with code `internal` and no details, because
the peer may be untrusted; the details go to the local log. Parameters are validated by the models in
`forge_sandbox/methods.py`; a bad one is answered with `bad_params` naming the field.

## Daemon methods (server → daemon)

| Method | Parameters | Result |
| --- | --- | --- |
| `fs.list` / `fs.stat` | `path` | entries `{name, type, size, mtime}` / one entry |
| `fs.read` | `path`, `limit` (≤ 900 000), `offset` | `{path, size, mtime, offset, truncated, binary, text \| base64}` (parts after the first are always base64) |
| `fs.write` | `path`, `text` or `base64`, `create_dirs`, `expected_mtime` | `{path, size, mtime}`; `conflict` if changed |
| `fs.write_part` | `path`, `upload` (16 hex), `base64`, `last`, `create_dirs`, `abort` | `{path, received, done: false}`, then `{path, size, mtime, done: true}`; parts go to a hidden file that the last part moves into place |
| `fs.mkdir` / `fs.rename` / `fs.delete` | `path` / `src`, `dst` / `path`, `recursive` | `{path}` |
| `procs.start` | `argv` or `command`, `cwd`, `env`, `name` | program info (`id`, `pid`, `running`, …) |
| `procs.list` / `procs.output` / `procs.stop` | — / `id`, `since`, `limit` / `id` | info / `{lines, from, next, …}` / info |
| `pty.create` / `pty.list` / `pty.resize` / `pty.close` | `cols`, `rows`, `cwd`, `argv` / — / `id`, `cols`, `rows` / `id` | terminal info |
| `forward.listen` | `target`, `port` (0 = any) | `{target, port}` on 127.0.0.1 inside the sandbox |
| `ports.list` | — | listening ports `{port, address}` (without the daemon's own) |
| `git.status` / `git.diff` | — / `path`, `staged`, `limit` | `{repo, branch, upstream, ahead, behind, files}` / `{diff, truncated}` |
| `git.stage` / `git.unstage` / `git.discard` | `paths` | `{ok}` — discard resets tracked files to HEAD and deletes new ones |
| `git.commit` | `message`, `name`, `email` | `{commit}`; `nothing_staged` |
| `git.branches` / `git.switch` | — / `branch`, `create` | `{current, branches}` / `{current}` |
| `git.log` | `limit` | `{commits: [{commit, author, email, time, subject}]}` |
| `git.remote` / `git.set_remote` | — / `url` | `{url}` |
| `git.bundle_out` | `branch` | `{bundle, head}` — packs the branch into `.git/forge-transfer/push.bundle` for a git job |
| `git.bundle_in` | `branch` | `{merged, head \| reason}` — takes `.git/forge-transfer/fetch.bundle` as `origin/<branch>` and fast-forwards a checked-out branch |
| `git.files` | `query`, `limit` | `{files, total}` — files not ignored by git, best matches first (name, then path, then letters in order) |
| `chat.open` | `chat_id`, `options`, `env` | chat info `{chat_id, state, seq, session_id, pending}` |
| `chat.send` | `chat_id`, `text` (a prompt, or a `/command`) | chat info; `busy` while a turn runs |
| `chat.answer` | `chat_id`, `request_id`, `answer` | `{accepted}` — only the first answer is accepted |
| `chat.cancel` / `chat.close` / `chat.list` | `chat_id` / `chat_id` / — | chat info / chat info / all chats |
| `daemon.info` | — | the hello info |

Notification (daemon → server): `procs.exited {id, exit_code}`.

## Channel kinds

| Kind | Opened by | Arguments | Carries |
| --- | --- | --- | --- |
| `chat` | server | `chat_id`, `after_seq` | messages: `hello` (chat info + `oldest`), `gap` (items before `oldest` are gone), then `{"type": "item", "seq", "item"}` in order |
| `pty` | server | `id` | DATA both ways (output / keystrokes); a `{"type": "resize", "cols", "rows"}` message |
| `connect` | server | `port` | DATA to and from 127.0.0.1:`port` inside the sandbox (live preview) |
| `forward` | daemon | `target` | DATA to and from a server target (LLM gateway, egress proxy); unknown targets are refused |

A sandbox may open nothing but `forward` channels.

## Chat items

Every item a chat produces gets the next `seq` (1, 2, 3, …) and stays in the daemon's buffer (the last
20 000), so a server that comes back asks for everything after the last `seq` it stored.

| `type` | From | Fields |
| --- | --- | --- |
| `user` | daemon | `text` — a message the user sent |
| `ready` | worker | `session_id`, `turns`, `commands` (`name`, `usage`, `help`, `custom`) — the Forge session is open; the slash commands it understands |
| `status` | worker | `state`: `running` or `idle` |
| `event` | worker | `event` — a Forge event (`kind` = `model_delta`, `tool_started`, …) |
| `request` | worker | `id`, `kind` (`approval` \| `question`), `payload` (`call` + `reason`, or `questions`) |
| `request_resolved` | daemon | `id`, and `answer` or `cancelled: true` |
| `command_result` | worker | `command`, `text` |
| `turn` | worker | `prompt`, `ok`, `summary`, `report`, `files_changed`, `usage`, `seconds`, maybe `cancelled`/`error` |
| `error` | worker | `message` |
| `worker_exited` | daemon | `exit_code` |
| `oversized` | daemon | an item too large for one message (`original_type`, `event_kind`) |

Answers: approvals `{"allow": bool, "remember": bool, "feedback": str}`; questions
`{"answers": [{"question_index": 0, "values": ["…"]}]}` or `{"dismissed": true}`. An answer that does not
fit counts as "no" (approvals) or "dismissed" (questions).

Worker ↔ daemon (inside the sandbox) is one JSON object per line on the worker's stdin/stdout:
`start {options}`, `prompt {text}`, `answer {id, answer}`, `cancel`, `shutdown` in; the items above out.

Git actions from the web UI run with `core.hooksPath=/dev/null`: repository hooks never run for them.

Pushing and pulling never happen in the sandbox: the server runs a git job (a throwaway container, or a
child process in local mode) that only exchanges bundle files with the project, has the user's token on
stdin and runs git with no system or global config, no hooks, https only and no redirects.
