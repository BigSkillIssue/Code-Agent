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
the peer may be untrusted; the details go to the local log.
The methods and channel kinds are listed with the daemon (step W03) and the worker (step W04).
