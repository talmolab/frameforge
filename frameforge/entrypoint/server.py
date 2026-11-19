"""``ff serve``: the node's management server, a stdlib HTTP front over Node.

Routes come from the verb registry: GET for reads with typed query args, POST with
the token header for writes, server-sent events for streams. Setup is mounted at
its own paths. Runs as root under ff-serve.service so systemd verbs work, and reads
the token from secrets.env on every request so a rotation needs no restart.
"""

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .client import to_json
from .handlers import secret
from .node import Node
from .verbs import TOKEN_HEADER, VERBS

DEFAULT_PORT = 8080
_SETUP_MODES = ("desk", "rack")

logger = logging.getLogger("frameforge.serve")


class Handler(BaseHTTPRequestHandler):
    node: Node

    def do_GET(self) -> None:
        url = urlparse(self.path)
        parts = url.path.strip("/").split("/")
        query = {key: values[0] for key, values in parse_qs(url.query).items()}

        if parts[:1] == ["setup"] and len(parts) == 3 and parts[2] == "questions":
            return self._setup_questions(parts[1])
        spec = self._verb(parts)
        if spec is None:
            return
        if spec.mutates:
            return self._json(405, {"error": "POST required"})

        kwargs = {}
        for arg in spec.args:
            if arg.flag:
                kwargs[arg.name] = query.get(arg.name, "false").lower() in ("true", "1")
            elif arg.name in query:
                try:
                    kwargs[arg.name] = arg.type(query[arg.name])
                except ValueError:
                    return self._json(400, {"error": f"{arg.name} must be {arg.type.__name__}"})
        outcome = getattr(self.node, spec.name)(**kwargs)
        if spec.streams:
            return self._stream(outcome)
        self._json(200, to_json(outcome))

    def do_POST(self) -> None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if self.headers.get(TOKEN_HEADER) != secret("FF_TOKEN"):
            return self._json(401, {"error": "missing or wrong token"})
        body = self._body()
        if body is None:
            return

        if parts[:1] == ["setup"] and len(parts) == 2:
            if parts[1] not in _SETUP_MODES:
                return self._json(400, {"error": f"unknown setup mode {parts[1]!r}"})
            return self._json(200, to_json(self.node.setup(parts[1], yes=True, answers=body)))
        spec = self._verb(parts)
        if spec is None:
            return
        if not spec.mutates:
            return self._json(405, {"error": "GET required"})
        allowed = {arg.name for arg in spec.args}
        unknown = set(body) - allowed
        if unknown:
            return self._json(400, {"error": f"unknown arguments: {sorted(unknown)}"})
        self._json(200, to_json(getattr(self.node, spec.name)(**body)))

    def _verb(self, parts: list[str]):
        if len(parts) == 2 and parts[0] == "api" and parts[1] in VERBS:
            return VERBS[parts[1]]
        self._json(404, {"error": "no such endpoint"})
        return None

    def _setup_questions(self, mode: str) -> None:
        if mode not in _SETUP_MODES:
            return self._json(400, {"error": f"unknown setup mode {mode!r}"})
        self._json(200, self.node.setup_questions(mode))

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except ValueError:
            self._json(400, {"error": "body must be JSON"})
            return None
        if not isinstance(body, dict):
            self._json(400, {"error": "body must be a JSON object"})
            return None
        return body

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _stream(self, lines) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            for line in lines:
                self.wfile.write(f"data: {line}\n\n".encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, format: str, *args) -> None:
        if not self.path.startswith("/api/logs"):
            logger.info("%s %s", self.address_string(), format % args)


def serve(port: int = DEFAULT_PORT, bind: str = "0.0.0.0") -> None:
    Handler.node = Node()
    server = ThreadingHTTPServer((bind, port), Handler)
    server.daemon_threads = True
    logger.info("ff serve listening on %s:%d", bind, port)
    server.serve_forever()
