"""The one HTTP server class Dourmouse binds ports with (finding #088).

``http.server.ThreadingHTTPServer`` sets ``allow_reuse_address``, which is
right on POSIX (a restart can rebind a port still in TIME_WAIT, and two
listeners are still refused) but wrong on Windows: there SO_REUSEADDR lets a
second process bind a port that is already listening, so two servers share
it silently and requests land on either. A stale instance can then shadow a
fresh one on the same port. On Windows this class binds with
SO_EXCLUSIVEADDRUSE instead, so a port in use is a real bind error.
"""

from __future__ import annotations

import socket
import sys
from http.server import ThreadingHTTPServer


class DourmouseHTTPServer(ThreadingHTTPServer):
    if sys.platform == "win32":
        allow_reuse_address = False

        def server_bind(self) -> None:
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)  # type: ignore[attr-defined]
            super().server_bind()
