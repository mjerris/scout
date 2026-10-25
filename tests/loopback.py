"""A ThreadingHTTPServer for the tests' local fakes, without the reverse DNS lookup."""

from __future__ import annotations

import socketserver
from http.server import ThreadingHTTPServer


class LoopbackHTTPServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the reverse DNS lookup in server_bind: HTTPServer calls
    socket.getfqdn(host) after binding and before listening, and on GitHub's macOS runners
    getfqdn("127.0.0.1") takes 35 s. The name is only used for SERVER_NAME in CGI, which
    nothing here reads."""

    request_queue_size = 128  # the default 5 refuses connections when a client sends a dozen at once

    def server_bind(self) -> None:
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name, self.server_port = str(host), int(port)
