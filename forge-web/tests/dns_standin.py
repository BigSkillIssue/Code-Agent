"""DNS for the domain checks (W30): a DNS-over-HTTPS resolver (the JSON form Cloudflare and
Google answer) on localhost, which knows only the TXT records a test gives it."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, urlsplit


class DohStandIn:
    """Answers `GET /dns-query?name=...&type=TXT` from `records` (name -> its TXT texts)."""

    def __init__(self) -> None:
        self.records: dict[str, list[str]] = {}
        self.asked: list[str] = []
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                query = parse_qs(urlsplit(self.path).query)
                name = query.get("name", [""])[0].rstrip(".").lower()
                stand_in.asked.append(name)
                texts = stand_in.records.get(name, [])
                body: dict[str, Any] = {
                    "Status": 0 if texts else 3,
                    "Answer": [
                        {"name": name, "type": 16, "TTL": 60, "data": f'"{text}"'} for text in texts
                    ],
                }
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/dns-json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/dns-query"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> "DohStandIn":
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
