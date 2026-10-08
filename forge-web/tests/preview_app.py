"""A small web app the preview tests run inside a project's sandbox: `python preview_app.py PORT`.

It reports what reached it (host, origin, cookies) and tries what a hostile app would try:
cookies for a parent domain, a cookie named like Forge's session, absolute redirects.
"""

import sys

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8000


async def echo(request: Request) -> Response:
    body = await request.body()
    seen = {
        "host": request.headers.get("host"),
        "origin": request.headers.get("origin"),
        "referer": request.headers.get("referer"),
        "cookie": request.headers.get("cookie", ""),
        "forwarded": request.headers.get("x-forwarded-for"),
        "method": request.method,
        "path": request.url.path,
        "query": request.url.query,
        "body": body.decode(),
    }
    response = JSONResponse(seen)
    response.headers.append("set-cookie", "app=1; Path=/; Domain=localhost")
    response.headers.append("set-cookie", "forge_session=stolen; Path=/")
    response.headers.append("set-cookie", "plain=2; Path=/; HttpOnly")
    return response


async def moved(_request: Request) -> Response:
    return RedirectResponse(f"http://localhost:{PORT}/echo?from=moved", status_code=302)


async def big(_request: Request) -> Response:
    async def chunks():  # type: ignore[no-untyped-def]
        for _ in range(64):
            yield b"x" * 16384

    return StreamingResponse(chunks(), media_type="application/octet-stream")


async def socket(websocket: WebSocket) -> None:
    offered = websocket.scope.get("subprotocols") or []
    await websocket.accept(subprotocol=offered[0] if offered else None)
    headers = websocket.headers
    await websocket.send_json(
        {"origin": headers.get("origin"), "host": headers.get("host"),
         "cookie": headers.get("cookie", "")}
    )  # fmt: skip
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            return
        if message.get("text") is not None:
            await websocket.send_text("echo:" + message["text"])
        elif message.get("bytes") is not None:
            await websocket.send_bytes(message["bytes"][::-1])


app = Starlette(
    routes=[
        Route("/echo", echo, methods=["GET", "POST"]),
        Route("/moved", moved),
        Route("/big", big),
        WebSocketRoute("/ws", socket),
    ]
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
