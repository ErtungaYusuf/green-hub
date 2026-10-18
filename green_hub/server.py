"""A small local web server for the hub: one page, a JSON API.

It listens on 127.0.0.1 only. Every request must name this machine in Host (a
DNS-rebinding page names its own), and every request that changes something must
carry the page's token in X-UI-Token, which a page from another site can neither read
nor send without a CORS preflight that is never granted.
"""

import json
import os
import secrets
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from green.node import names_this_machine


class UIError(Exception):
    pass


def node_error(e: Exception) -> str:
    """An error as a person should read it: with the node's own explanation."""
    if isinstance(e, urllib.error.HTTPError):
        try:
            return json.loads(e.read()).get("error", str(e))
        except (ValueError, OSError):
            return str(e)
    if isinstance(e, (urllib.error.URLError, ConnectionError, TimeoutError)):
        return "the node does not answer (is it running?)"
    return str(e)


class LocalUI:
    """Serve `page` at / and dispatch /api/<name> to methods: get_<name>(query) for GET,
    post_<name>(body) for POST (which need the token)."""

    def __init__(self, page_file: str, port: int, title: str):
        self.token = secrets.token_hex(16)
        self.title = title
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), page_file), encoding="utf-8") as f:
            self.page = f.read().replace("{{TOKEN}}", self.token).encode()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        threading.Thread(target=self.server.serve_forever, daemon=True, name="ui-http").start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        ui = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 60

            def log_message(self, *args):
                pass

            def _send(self, code: int, body, ctype: str = "application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; "
                                 "style-src 'unsafe-inline'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(data)

            def _route(self):
                if not names_this_machine(self.headers.get("Host")):
                    self._drain()
                    return None, self._send(403, {"error": "use http://127.0.0.1"})
                u = urlparse(self.path)
                return u, None

            def _drain(self):
                raw = self.headers.get("Content-Length", "0")
                if raw.isdigit() and int(raw) <= 1 << 20:
                    self.rfile.read(int(raw))
                self.close_connection = True

            def do_GET(self):
                u, done = self._route()
                if u is None:
                    return done
                if u.path == "/":
                    return self._send(200, ui.page, "text/html; charset=utf-8")
                fn = getattr(ui, "get_" + u.path[len("/api/"):], None) if u.path.startswith("/api/") else None
                if fn is None:
                    return self._send(404, {"error": "not found"})
                return self._call(fn, {k: v[0] for k, v in parse_qs(u.query).items()})

            def do_POST(self):
                u, done = self._route()
                if u is None:
                    return done
                if self.headers.get("X-UI-Token") != ui.token:
                    self._drain()
                    return self._send(403, {"error": "missing or wrong X-UI-Token (open the page at its own address)"})
                fn = getattr(ui, "post_" + u.path[len("/api/"):], None) if u.path.startswith("/api/") else None
                raw = self.headers.get("Content-Length", "0")
                if not raw.isdigit() or int(raw) > 64 * 1024:
                    self._drain()
                    return self._send(413, {"error": "too large"})
                try:
                    body = json.loads(self.rfile.read(int(raw)) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "bad JSON"})
                if fn is None or not isinstance(body, dict):
                    return self._send(404, {"error": "not found"})
                return self._call(fn, body)

            def _call(self, fn, arg):
                try:
                    return self._send(200, fn(arg))
                except UIError as e:
                    return self._send(400, {"error": str(e)})
                except (KeyError, TypeError, ValueError) as e:
                    return self._send(400, {"error": f"bad request: {e}"})
                except urllib.error.HTTPError as e:
                    return self._send(400, {"error": node_error(e)})       # the node said no, and why
                except (OSError, urllib.error.URLError) as e:
                    return self._send(502, {"error": node_error(e)})

        return Handler
