"""Binding the socket, and proving what it bound.

This app renders the corpus, the journal and -- from 029 -- endpoints that spend
the user's Claude Code credentials. The bind address is therefore a security
property rather than a default, and the plan states it as an invariant: *the server
binds `127.0.0.1`, asserted against the actually-bound socket.*

"Asserted against the actually-bound socket" is the whole point of this module.
Passing `host="127.0.0.1"` to `uvicorn.run` asserts nothing: it is an argument, and
arguments are what change. So `serve` creates the listening socket itself and the
check happens twice around the one operation that can surprise you:

1. **Before binding**, the requested host is resolved and refused if the address it
   resolves to is not loopback -- so a routable address never gets bound at all,
   not even for the microsecond before something notices.
2. **After binding**, `getsockname()` is read back off the socket and checked
   again. That is the address the kernel actually gave, which is the only address
   that means anything: `""`, `0`, `0.0.0.0` and a hostname that resolves
   differently on someone else's box all answer this question honestly.

Only then is the socket handed to uvicorn. A LAN bind behind a token is decision
**D1** and is not implemented; until it is decided, asking for one is an error that
names the file where the decision lives.
"""

from __future__ import annotations

import errno
import ipaddress
import socket
from dataclasses import dataclass
from typing import Any

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
BACKLOG = 128

DECISION = "issues/decisions/D1-portability.md"


class BindRefused(Exception):
    """The address asked for is not one this server is allowed to serve on."""


@dataclass(frozen=True)
class Bound:
    """A listening socket and the address the kernel gave it."""

    socket: socket.socket
    host: str
    port: int

    @property
    def url(self) -> str:
        host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{host}:{self.port}"

    def close(self) -> None:
        self.socket.close()


def is_loopback(host: str) -> bool:
    """True for `127.0.0.0/8` and `::1`. Never true for `0.0.0.0` or `::`.

    Note `0.0.0.0` is *not* loopback even though connecting to it from this box
    works: it means "every interface", which includes the one the LAN is on.
    """
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _refuse(address: str, *, resolved_from: str | None = None) -> BindRefused:
    origin = f"{resolved_from} resolves to {address}" if resolved_from else address
    return BindRefused(
        f"refusing to serve on {origin}: it is not a loopback address. "
        f"This server holds your corpus, your attempt history and (from 029) your "
        f"model credentials, so it binds {DEFAULT_HOST} only. "
        f"Serving on a LAN is decision D1 -- see {DECISION}."
    )


def resolve(host: str, port: int) -> tuple[int, tuple[Any, ...]]:
    """`(family, sockaddr)` for `host`, refusing a non-loopback result."""
    if not host:
        raise _refuse("0.0.0.0", resolved_from='""')
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise BindRefused(f"cannot resolve host {host!r}: {error}") from error
    if not infos:
        raise BindRefused(f"cannot resolve host {host!r}")

    family, _type, _proto, _canon, sockaddr = infos[0]
    address = str(sockaddr[0])
    if not is_loopback(address):
        raise _refuse(address, resolved_from=None if address == host else host)
    return family, sockaddr


def bind(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> Bound:
    """Bind a listening socket, and check the address the kernel actually gave.

    Raises `BindRefused` without leaving a socket open, on either check.
    """
    family, sockaddr = resolve(host, port)

    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(sockaddr)
        bound_host, bound_port = sock.getsockname()[:2]
        bound_host = str(bound_host)
        if not is_loopback(bound_host):
            # Unreachable through `bind()` above, which is the point: this is the
            # check that still holds if the code above is changed by someone who
            # did not read the docstring.
            raise _refuse(bound_host)
        sock.listen(BACKLOG)
    except BaseException:
        sock.close()
        raise
    return Bound(socket=sock, host=bound_host, port=int(bound_port))


def bind_or_explain(host: str, port: int) -> Bound:
    """`bind`, turning "address already in use" into a sentence, not a traceback."""
    try:
        return bind(host, port)
    except OSError as error:
        if error.errno == errno.EADDRINUSE:
            raise BindRefused(
                f"{host}:{port} is already in use -- another `examkb serve` is "
                f"probably running. Use --port to pick another one."
            ) from error
        raise BindRefused(f"cannot bind {host}:{port}: {error}") from error


def run(bound: Bound, *, app: Any = None, log_level: str = "info") -> None:
    """Serve `app` on an already-bound socket until interrupted.

    uvicorn is imported here rather than at module scope so that the bind check --
    the part with the security property on it -- can be imported and tested without
    pulling in a web server.
    """
    import uvicorn

    from examkb.web.app import create_app

    config = uvicorn.Config(
        app if app is not None else create_app(),
        log_level=log_level,
        access_log=False,  # One user, one browser: the access log is noise.
        date_header=False,
        server_header=False,
    )
    uvicorn.Server(config).run(sockets=[bound.socket])
