"""VIES for the VAT ID checks (W34): the EU's REST check (`POST .../check-vat-number` with the
country code and the number) on localhost, which knows only the numbers a test gives it."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any


class ViesStandIn:
    """Answers whether a VAT ID is registered, with the name and address it was given."""

    def __init__(self) -> None:
        self.known: dict[str, tuple[str, str]] = {}  # "DE123456789" -> (name, address)
        self.down = False
        self.asked: list[str] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                size = int(self.headers.get("Content-Length", "0"))
                asked = json.loads(self.rfile.read(size) or b"{}")
                number = f"{asked.get('countryCode', '')}{asked.get('vatNumber', '')}"
                stand_in.asked.append(number)
                if stand_in.down:
                    self.answer(503, {"errorWrappers": [{"error": "MS_UNAVAILABLE"}]})
                    return
                name, address = stand_in.known.get(number, ("---", "---"))
                self.answer(200, {"countryCode": asked.get("countryCode"),
                                  "vatNumber": asked.get("vatNumber"),
                                  "valid": number in stand_in.known,
                                  "name": name, "address": address})  # fmt: skip

            def answer(self, status: int, body: dict[str, Any]) -> None:
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/check-vat-number"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "ViesStandIn":
        self.thread.start()
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.server.shutdown()
        self.server.server_close()
